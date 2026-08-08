"""Tests for the append-only integer-cent ledger + cash balance + bankruptcy
(spec 01 §Money and bankruptcy; acceptance #7)."""

from __future__ import annotations

import pytest

from csr_bench.db import begin, commit, in_memory, init_schema, insert_run, new_run_id, rollback
from csr_bench.enums import LedgerCategory, RunStatus
from csr_bench.ledger import Ledger, LedgerEntry


@pytest.fixture
def ledger():
    conn = in_memory()
    init_schema(conn)
    rid = new_run_id()
    insert_run(
        conn,
        run_id=rid,
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        config_json="{}",
        master_seed=1,
        start_at_ms=0,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        current_time_ms=0,
        status=RunStatus.ACTIVE,
    )
    yield conn, Ledger(conn, rid), rid
    conn.close()


def test_cash_balance_starts_zero_then_opening_adjustment(ledger):
    conn, lg, rid = ledger
    assert lg.cash_balance() == 0
    begin(conn)
    lg.post([LedgerEntry(100000, LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 0)])
    commit(conn)
    assert lg.cash_balance() == 100000
    assert lg.entry_count() == 1


def test_cash_balance_is_sum_of_committed_entries(ledger):
    conn, lg, rid = ledger
    begin(conn)
    lg.post(
        [
            LedgerEntry(100000, LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 0),
            LedgerEntry(5000, LedgerCategory.CONTRACT_TEST_CREDIT, 10),
            LedgerEntry(-1500, LedgerCategory.CONTRACT_TEST_DEBIT, 20),
        ]
    )
    commit(conn)
    assert lg.cash_balance() == 100000 + 5000 - 1500
    assert lg.cash_balance_money().cents == 100000 + 5000 - 1500


def test_rolled_back_ledger_leaves_no_trace(ledger):
    conn, lg, rid = ledger
    begin(conn)
    lg.post([LedgerEntry(100000, LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 0)])
    rollback(conn)
    assert lg.cash_balance() == 0
    assert lg.entry_count() == 0


def test_reject_float_amount_and_non_enum_category(ledger):
    _, lg, rid = ledger
    from csr_bench.errors import EngineError

    with pytest.raises(EngineError):
        LedgerEntry(100.5, LedgerCategory.CONTRACT_TEST_DEBIT, 0)  # type: ignore[arg-type]
    with pytest.raises(EngineError):
        LedgerEntry(100, "not_a_category", 0)  # type: ignore[arg-type]


def test_post_outside_open_transaction_raises_guard_error(ledger):
    """Same guard pattern as its siblings (``Events.schedule``, events.py:70;
    ``Rng.draw``, rng.py:383): the run connection is autocommit, so a post()
    outside a transaction would otherwise commit immediately with no rollback
    able to undo it."""
    conn, lg, rid = ledger
    from csr_bench.errors import EngineError, ErrorCode

    assert not conn.in_transaction
    with pytest.raises(EngineError) as ei:
        lg.post([LedgerEntry(100000, LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 0)])
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN
    assert lg.cash_balance() == 0
    assert lg.entry_count() == 0


def test_require_cash_rejects_bool_and_non_int_amount(ledger):
    _, lg, rid = ledger
    from csr_bench.errors import EngineError

    with pytest.raises(EngineError):
        lg.require_cash(True)  # type: ignore[arg-type]
    with pytest.raises(EngineError):
        lg.require_cash(1.5)  # type: ignore[arg-type]


def test_bankruptcy_check_flips_status_when_negative(ledger):
    conn, lg, rid = ledger
    begin(conn)
    lg.post([LedgerEntry(1000, LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 0)])
    commit(conn)
    # an unavoidable settlement pushes cash negative
    begin(conn)
    lg.post([LedgerEntry(-1500, LedgerCategory.CONTRACT_TEST_DEBIT, 1)])
    became = lg.bankruptcy_check()
    commit(conn)
    assert became is True
    row = conn.execute("SELECT status FROM run WHERE run_id=?", (rid,)).fetchone()
    assert row["status"] == "bankrupt"


def test_bankruptcy_check_noop_when_non_negative(ledger):
    conn, lg, rid = ledger
    begin(conn)
    lg.post([LedgerEntry(100000, LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 0)])
    lg.post([LedgerEntry(-500, LedgerCategory.CONTRACT_TEST_DEBIT, 1)])
    assert lg.bankruptcy_check() is False
    commit(conn)
    assert lg.cash_balance() == 99500
    row = conn.execute("SELECT status FROM run WHERE run_id=?", (rid,)).fetchone()
    assert row["status"] == "active"


def test_bankruptcy_check_idempotent_and_terminal_safe(ledger):
    conn, lg, rid = ledger
    begin(conn)
    lg.post([LedgerEntry(-1, LedgerCategory.CONTRACT_TEST_DEBIT, 0)])
    assert lg.bankruptcy_check() is True
    assert lg.bankruptcy_check() is False  # already bankrupt (no longer ACTIVE)
    commit(conn)
    row = conn.execute("SELECT status FROM run WHERE run_id=?", (rid,)).fetchone()
    assert row["status"] == "bankrupt"
