"""Tests for the append-only integer-cent ledger + cash balance + bankruptcy
(spec 02 §4.7, spec 06 §9.1; CSR-002 decision 9)."""

from __future__ import annotations

import sqlite3

import pytest

from csr_bench import clock
from csr_bench.db import begin, commit, in_memory, insert_run, new_run_id, rollback
from csr_bench.enums import LedgerCategory, RunStatus
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.ledger import Ledger, LedgerEntry
from csr_bench.migrations import migrate

TS = clock.phoenix_instant(2026, 10, 1, 6, 45)


@pytest.fixture
def ledger():
    conn = in_memory()
    migrate(conn)
    begin(conn)
    insert_run(
        conn,
        run_id=new_run_id(),
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        master_seed=1,
        start_at_ms=0,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        status=RunStatus.ACTIVE,
    )
    commit(conn)
    yield conn, Ledger(conn)
    conn.close()


def _stored_rows(conn):
    return conn.execute(
        "SELECT occurred_at, service_date, entry_type, amount_cents, "
        "balance_after_cents, job_id, invoice_id, payment_id, technician_id, "
        "hiring_request_id, training_reservation_id, description, created_at, origin "
        "FROM _ledger_entries ORDER BY id"
    ).fetchall()


def test_cash_balance_starts_zero_then_opening_adjustment(ledger):
    conn, lg = ledger
    assert lg.cash_balance() == 0
    assert lg.entry_count() == 0
    begin(conn)
    lg.post([LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 100000, TS)])
    commit(conn)
    assert lg.cash_balance() == 100000
    assert lg.entry_count() == 1


def test_post_computes_running_balances_in_caller_order(ledger):
    conn, lg = ledger
    begin(conn)
    lg.post(
        [
            LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 100000, TS),
            LedgerEntry(LedgerCategory.REVENUE, 5000, TS),
            LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1500, TS),
        ]
    )
    commit(conn)
    rows = _stored_rows(conn)
    assert [r["amount_cents"] for r in rows] == [100000, 5000, -1500]
    # balance_after_cents is the exact running total in caller order
    assert [r["balance_after_cents"] for r in rows] == [100000, 105000, 103500]
    # cash is the ledger: the sum equals the last row's running balance
    assert lg.cash_balance() == 103500
    assert lg.cash_balance_money().cents == 103500
    assert lg.entry_count() == 3


def test_post_batch_with_negative_amount_mid_list(ledger):
    conn, lg = ledger
    begin(conn)
    lg.post(
        [
            LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 50000, TS),
            LedgerEntry(LedgerCategory.MATERIAL_COST, -8000, TS),
            LedgerEntry(LedgerCategory.REVENUE, 20000, TS),
        ]
    )
    commit(conn)
    rows = _stored_rows(conn)
    assert [r["balance_after_cents"] for r in rows] == [50000, 42000, 62000]
    assert lg.cash_balance() == 62000


def test_post_running_balance_continues_across_batches(ledger):
    conn, lg = ledger
    begin(conn)
    lg.post([LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 100000, TS)])
    commit(conn)
    begin(conn)
    lg.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1500, TS)])
    commit(conn)
    assert lg.cash_balance() == 98500
    rows = _stored_rows(conn)
    assert [r["balance_after_cents"] for r in rows] == [100000, 98500]


def test_service_date_derived_in_phoenix_time_when_omitted(ledger):
    conn, lg = ledger
    # 2026-10-01 23:00 Phoenix == 2026-10-02 06:00 UTC: the Phoenix-local date
    # is the grouping key, never the UTC one (spec 01 §Canonical primitive types).
    late_ts = clock.phoenix_instant(2026, 10, 1, 23, 0)
    begin(conn)
    lg.post([LedgerEntry(LedgerCategory.REVENUE, 1000, late_ts)])
    commit(conn)
    assert _stored_rows(conn)[0]["service_date"] == "2026-10-01"


def test_explicit_service_date_is_kept_not_rederived(ledger):
    conn, lg = ledger
    # A warm-start opening row: occurred at the fixture's pin instant, dated
    # before the run clock starts — the explicit date must survive untouched
    # (decision 9), even though derivation would produce a different value.
    warm_ts = clock.phoenix_instant(2026, 9, 30, 23, 59) + 59 * 1000
    begin(conn)
    lg.post(
        [
            LedgerEntry(
                LedgerCategory.OPENING_BALANCE_ADJUSTMENT,
                500000,
                warm_ts,
                service_date="2026-09-30",
                origin="warm_start",
            )
        ]
    )
    commit(conn)
    row = _stored_rows(conn)[0]
    assert row["service_date"] == "2026-09-30"
    assert row["occurred_at"] == warm_ts
    assert row["origin"] == "warm_start"


def test_origin_defaults_to_run_and_warm_start_accepted(ledger):
    conn, lg = ledger
    begin(conn)
    lg.post(
        [
            LedgerEntry(LedgerCategory.REVENUE, 1000, TS),
            LedgerEntry(LedgerCategory.RECRUITING_COST, -500, TS, origin="warm_start"),
        ]
    )
    commit(conn)
    assert [r["origin"] for r in _stored_rows(conn)] == ["run", "warm_start"]


def test_unknown_origin_rejected(ledger):
    with pytest.raises(EngineError):
        LedgerEntry(LedgerCategory.REVENUE, 100, TS, origin="bogus")  # type: ignore[arg-type]


def test_post_round_trips_all_six_source_links_and_created_at(ledger):
    conn, lg = ledger
    # Minimal parent rows so the six link FKs resolve through the real schema
    # (the domain tables' own modules land in later chunks).
    begin(conn)
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at) "
        "VALUES (1, 'Test', 'residential', 'active', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, "
        "postal_code, latitude, longitude, active, created_at, modified_at, "
        "effective_zone_id) "
        "VALUES (1, 1, 'Home', '1 Main St', '', 'Phoenix', 'AZ', '85001', "
        "33.5, -112.1, 1, ?, ?, 1)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
        "caller_name, caller_phone, requested_service_name, urgent, created_at) "
        "VALUES (1, ?, '2026-10-01', 'new_job', 'booked', 'A', '555', 'HVAC service', 0, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO _jobs (id, job_number, call_id, customer_id, location_id, job_type_id, "
        "requested_service_name, status, priority, no_charge, summary, created_at, modified_at) "
        "VALUES (1, 'J0001', 1, 1, 1, 1, 'HVAC service', 'scheduled', 'normal', 0, 's', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO _invoices (id, invoice_number, job_id, customer_id, location_id, "
        "invoice_date, issued_at, status, no_charge, subtotal_cents, total_cents, "
        "material_cost_cents, material_percent_basis_points, created_at) "
        "VALUES (1, 'INV0001', 1, 1, 1, '2026-10-01', ?, 'paid', 0, 0, 0, 0, 0, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO _payments (id, payment_number, invoice_id, customer_id, paid_at, "
        "amount_cents, payment_type, status, created_at) "
        "VALUES (1, 'PAY0001', 1, 1, ?, 0, 'immediate', 'posted', ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO _technicians (id, name, street, unit, city, state, postal_code, "
        "home_latitude, home_longitude, joined_on, hourly_wage_cents, employment_status, "
        "availability_status, created_at, modified_at) "
        "VALUES (1, 'T', '1 St', '', 'Phoenix', 'AZ', '85001', 0, 0, '2026-01-01', 3000, "
        "'active', 'available', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO _hiring_requests (id, requested_at, requested_skill_codes_json, "
        "minimum_experience_years, status, recruiting_cost_cents, "
        "committed_hourly_wage_cents, committed_start_date, created_at, modified_at) "
        "VALUES (1, ?, '[\"plumbing\"]', 1, 'completed', 1000, 3000, '2026-09-01', ?, ?)",
        (TS, TS, TS),
    )
    conn.execute(
        "INSERT INTO _training_reservations (id, technician_id, skill_code, scheduled_at, "
        "total_hours, locked_hourly_rate_cents, prepaid_cost_cents, status, "
        "completed_hours, created_at, modified_at) "
        "VALUES (1, 1, 'plumbing', ?, 4, 3000, 12000, 'completed', 4, ?, ?)",
        (TS, TS, TS),
    )
    lg.post(
        [
            LedgerEntry(
                LedgerCategory.REVENUE,
                10000,
                TS,
                description="paid completion",
                job_id=1,
                invoice_id=1,
                payment_id=1,
                technician_id=1,
                hiring_request_id=1,
                training_reservation_id=1,
            )
        ]
    )
    commit(conn)
    row = _stored_rows(conn)[0]
    assert row["entry_type"] == "revenue"
    assert row["amount_cents"] == 10000
    assert row["balance_after_cents"] == 10000
    assert row["description"] == "paid completion"
    assert row["job_id"] == 1
    assert row["invoice_id"] == 1
    assert row["payment_id"] == 1
    assert row["technician_id"] == 1
    assert row["hiring_request_id"] == 1
    assert row["training_reservation_id"] == 1
    assert row["created_at"] == TS  # created_at == occurred_at (append-only)
    assert row["occurred_at"] == TS
    assert row["service_date"] == "2026-10-01"
    assert row["origin"] == "run"


def test_rolled_back_ledger_leaves_no_trace(ledger):
    conn, lg = ledger
    begin(conn)
    lg.post([LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 100000, TS)])
    rollback(conn)
    assert lg.cash_balance() == 0
    assert lg.entry_count() == 0


def test_post_empty_batch_is_noop(ledger):
    conn, lg = ledger
    begin(conn)
    lg.post([])
    commit(conn)
    assert lg.cash_balance() == 0
    assert lg.entry_count() == 0


def test_post_outside_open_transaction_raises_guard_error(ledger):
    """Same guard pattern as its siblings (``Events.schedule``, events.py:70;
    ``Rng.draw``, rng.py:383): the run connection is autocommit, so a post()
    outside a transaction would otherwise commit immediately with no rollback
    able to undo it."""
    conn, lg = ledger
    assert not conn.in_transaction
    with pytest.raises(EngineError) as ei:
        lg.post([LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 100000, TS)])
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN
    assert lg.cash_balance() == 0
    assert lg.entry_count() == 0


def test_require_cash_sufficient_and_insufficient(ledger):
    conn, lg = ledger
    begin(conn)
    lg.post([LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 100000, TS)])
    commit(conn)
    lg.require_cash(100000)  # exact fit passes
    with pytest.raises(EngineError) as ei:
        lg.require_cash(100001)
    assert ei.value.code is ErrorCode.INSUFFICIENT_CASH


def test_require_cash_rejects_bool_and_non_int_amount(ledger):
    _, lg = ledger
    with pytest.raises(EngineError):
        lg.require_cash(True)  # type: ignore[arg-type]
    with pytest.raises(EngineError):
        lg.require_cash(1.5)  # type: ignore[arg-type]


def test_bankruptcy_check_flips_status_when_negative(ledger):
    conn, lg = ledger
    begin(conn)
    lg.post([LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 1000, TS)])
    commit(conn)
    # an unavoidable settlement pushes cash negative
    begin(conn)
    lg.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1500, TS)])
    became = lg.bankruptcy_check()
    commit(conn)
    assert became is True
    row = conn.execute("SELECT status FROM _run").fetchone()
    assert row["status"] == "bankrupt"


def test_bankruptcy_check_noop_when_non_negative(ledger):
    conn, lg = ledger
    begin(conn)
    lg.post([LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 100000, TS)])
    lg.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -500, TS)])
    assert lg.bankruptcy_check() is False
    commit(conn)
    assert lg.cash_balance() == 99500
    row = conn.execute("SELECT status FROM _run").fetchone()
    assert row["status"] == "active"


def test_bankruptcy_check_idempotent_and_terminal_safe(ledger):
    conn, lg = ledger
    begin(conn)
    lg.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1, TS)])
    assert lg.bankruptcy_check() is True
    assert lg.bankruptcy_check() is False  # already bankrupt (no longer ACTIVE)
    commit(conn)
    row = conn.execute("SELECT status FROM _run").fetchone()
    assert row["status"] == "bankrupt"


def test_bankruptcy_check_noop_when_run_already_terminal(ledger):
    conn, lg = ledger
    begin(conn)
    conn.execute("UPDATE _run SET status = 'completed'")
    lg.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1500, TS)])
    assert lg.bankruptcy_check() is False
    commit(conn)
    row = conn.execute("SELECT status FROM _run").fetchone()
    assert row["status"] == "completed"


def test_bankruptcy_check_outside_open_transaction_raises_guard_error(ledger):
    """Same autocommit reasoning as ``post``: outside a transaction a status
    flip would commit immediately and could never be rolled back with the
    financial effect that caused it."""
    conn, lg = ledger
    assert not conn.in_transaction
    with pytest.raises(EngineError) as ei:
        lg.bankruptcy_check()
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN
    row = conn.execute("SELECT status FROM _run").fetchone()
    assert row["status"] == "active"


def test_ledger_entries_are_append_only_at_storage_layer(ledger):
    conn, lg = ledger
    begin(conn)
    lg.post([LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 1000, TS)])
    commit(conn)
    begin(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE _ledger_entries SET amount_cents = 9999")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM _ledger_entries")
    rollback(conn)
    assert lg.cash_balance() == 1000
    assert lg.entry_count() == 1


def test_reject_unknown_entry_type(ledger):
    with pytest.raises(EngineError):
        LedgerEntry("not_a_category", 100, TS)  # type: ignore[arg-type]


#: The eight members with a fixed sign; ``opening_balance_adjustment`` may be
#: either sign (spec 06 §9.1 / spec 02 §4.7).
_FIXED_SIGN_CATEGORIES = [
    c for c in LedgerCategory if c is not LedgerCategory.OPENING_BALANCE_ADJUSTMENT
]


@pytest.mark.parametrize("category", _FIXED_SIGN_CATEGORIES)
def test_sign_rule_rejects_wrong_sign_amount(ledger, category):
    wrong_sign = -1 if category is LedgerCategory.REVENUE else 1
    with pytest.raises(EngineError):
        LedgerEntry(category, wrong_sign, TS)


@pytest.mark.parametrize("category", _FIXED_SIGN_CATEGORIES)
def test_sign_rule_rejects_zero_for_fixed_sign_members(ledger, category):
    # zero is neither positive nor negative, so it violates a fixed-sign member
    with pytest.raises(EngineError):
        LedgerEntry(category, 0, TS)


@pytest.mark.parametrize("category", _FIXED_SIGN_CATEGORIES)
def test_sign_rule_accepts_correct_sign_amount(ledger, category):
    right_sign = 1 if category is LedgerCategory.REVENUE else -1
    assert LedgerEntry(category, right_sign, TS).amount_cents == right_sign


def test_opening_balance_adjustment_accepts_either_sign_and_zero(ledger):
    for amount in (1000, -1000, 0):
        assert LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, amount, TS).amount_cents == amount


def test_reject_float_and_bool_amount_and_bad_field_types(ledger):
    with pytest.raises(EngineError):
        LedgerEntry(LedgerCategory.REVENUE, 100.5, TS)  # type: ignore[arg-type]
    with pytest.raises(EngineError):
        LedgerEntry(LedgerCategory.REVENUE, True, TS)  # type: ignore[arg-type]
    with pytest.raises(EngineError):
        LedgerEntry(LedgerCategory.REVENUE, 100, True)  # type: ignore[arg-type]
    with pytest.raises(EngineError):
        LedgerEntry(LedgerCategory.REVENUE, 100, TS, description=123)  # type: ignore[arg-type]
