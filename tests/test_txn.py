"""Tests for command transactions + idempotency + privileged isolation
(spec 01 §Command transactions and idempotency, §Failure; acceptance #4, #9)."""

from __future__ import annotations

import json

import pytest

from _helpers import register_conn
from csr_bench.db import in_memory, init_schema, insert_run, new_run_id
from csr_bench.enums import LedgerCategory, RunStatus
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.ledger import Ledger, LedgerEntry
from csr_bench.rng import Rng
from csr_bench.txn import (
    PRIVILEGED_NAMESPACE,
    PUBLIC_NAMESPACE,
    Services,
    command,
    is_reserved,
    privileged,
    privileged_request_id,
)


def _audit_recorder(conn, run_id):
    def rec(**kw):
        conn.execute(
            "INSERT INTO _rng_draw_audit(run_id, stream_name, entity_type, entity_id, "
            "scope, event_kind, draw_index, outcome_id, simulated_ts_ms) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            (
                run_id,
                kw["stream_name"],
                kw["entity_type"],
                kw["entity_id"],
                kw["scope"],
                kw["event_kind"],
                kw["draw_index"],
                kw.get("outcome_id"),
            ),
        )

    return rec


class FakeInvocations:
    def __init__(self, conn, run_id):
        self.conn = conn
        self.run_id = run_id

    def active(self):
        row = self.conn.execute(
            "SELECT * FROM _invocations WHERE run_id=? AND status='active' "
            "ORDER BY sequence DESC LIMIT 1",
            (self.run_id,),
        ).fetchone()
        return row


def _services(*, active_invocation=False, status=RunStatus.ACTIVE, with_ledger=True):
    conn = in_memory()
    register_conn(conn)
    init_schema(conn)
    rid = new_run_id()
    conn.execute("BEGIN")
    insert_run(
        conn,
        run_id=rid,
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        config_json="{}",
        master_seed=42,
        start_at_ms=0,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        current_time_ms=0,
        status=status,
    )
    conn.execute("COMMIT")
    rng = Rng(42, "1.0.0", audit_recorder=_audit_recorder(conn, rid))
    ledger = Ledger(conn, rid) if with_ledger else None
    inv = FakeInvocations(conn, rid)
    if active_invocation:
        conn.execute(
            "INSERT INTO _invocations(run_id, type, ts_ms, sequence, status, attempt_count) "
            "VALUES (?, 'call', 0, 1, 'active', 1)",
            (rid,),
        )
    sv = Services(conn=conn, run_id=rid, rng=rng, ledger=ledger, invocations=inv)
    return conn, rid, sv, ledger, rng


def _audit_count(conn, rid):
    return conn.execute("SELECT COUNT(*) c FROM _rng_draw_audit WHERE run_id=?", (rid,)).fetchone()[
        "c"
    ]


# ---------------------------------------------------------------------------
# Nnamespace / privileged isolation (#9)
# ---------------------------------------------------------------------------


def test_command_rejects_reserved_request_id_with_uniform_error():
    conn, rid, sv, _, _ = _services(active_invocation=True)
    with pytest.raises(EngineError) as ei:
        command(sv, privileged_request_id("apply_event", 7), lambda ctx: {})
    assert ei.value.code is ErrorCode.UNAUTHORIZED
    assert ei.value.message == "unauthorized"


def test_is_reserved_recognises_namespace():
    assert is_reserved("__control__:apply_event:5")
    assert not is_reserved("jobs.book.5")
    assert is_reserved(privileged_request_id("settle", "day_3"))


def test_privileged_requires_reserved_id():
    conn, rid, sv, _, _ = _services()
    with pytest.raises(EngineError):
        privileged(sv, "not_reserved", lambda ctx: {})


def test_privileged_op_unreachable_via_command_probe():
    conn, rid, sv, _, _ = _services(active_invocation=True)
    # The controller's reserved id used through the public command path -> uniform
    # auth error, even though the privileged operation itself would succeed.
    ridp = privileged_request_id("settle", "day_1")
    with pytest.raises(EngineError) as ei:
        command(sv, ridp, lambda ctx: {"ok": True})
    assert ei.value.code is ErrorCode.UNAUTHORIZED


# ---------------------------------------------------------------------------
# Validation ordering — fn never runs before engine checks pass
# ---------------------------------------------------------------------------


def test_command_fails_when_run_not_active_and_fn_never_runs():
    calls = {"n": 0}

    def fn(ctx):
        calls["n"] += 1
        return {}

    conn, rid, sv, _, _ = _services(status=RunStatus.INITIALIZING, active_invocation=True)
    r1 = command(sv, "req.1", fn)
    r2 = command(sv, "req.1", fn)
    assert calls["n"] == 0  # validation failed before fn; never ran either call
    assert r1 == r2
    assert json.loads(r1)["error"]["code"] == "invalid_run_status"


def test_command_fails_without_active_invocation():
    conn, rid, sv, _, rng = _services(active_invocation=False)
    drew = {"n": 0}

    def fn(ctx):
        drew["n"] += 1
        ctx.rng.draw("acceptance", "contract_test_entity", 1, "2026-10-01", "k", 0)
        return {}

    r = command(sv, "req.2", fn)
    assert json.loads(r)["error"]["code"] == "invocation_conflict"
    assert drew["n"] == 0
    assert _audit_count(conn, rid) == 0  # no draw persisted


# ---------------------------------------------------------------------------
# Idempotency (#4) — replayed request_id returns stored response, no dups
# ---------------------------------------------------------------------------


def test_successful_command_idempotent_byte_for_byte_no_duplicates():
    ledger_calls = {"n": 0}

    def fn(ctx):
        ledger_calls["n"] += 1
        ctx.rng.draw(
            "acceptance", "contract_test_entity", 7, "2026-10-01", "k", 0, outcome_id="call_42"
        )
        ctx.services.ledger.post(
            [
                LedgerEntry(
                    5000,
                    LedgerCategory.CONTRACT_TEST_CREDIT,
                    0,
                    entity_type="contract_test_entity",
                    entity_id=7,
                )
            ]
        )
        return {"booked": True, "entity_id": 7}

    conn, rid, sv, ledger, _ = _services(active_invocation=True)
    r1 = command(sv, "jobs.book.5", fn)
    r2 = command(sv, "jobs.book.5", fn)
    assert r1 == r2
    assert json.loads(r1) == {"ok": True, "result": {"booked": True, "entity_id": 7}}
    assert ledger_calls["n"] == 1  # fn ran exactly once
    assert ledger.entry_count() == 1  # no duplicate ledger row
    assert _audit_count(conn, rid) == 1  # no duplicate draw audit row


def test_failed_command_is_idempotent_and_rolls_back_draws_and_writes():
    runs = {"n": 0}

    def fn(ctx):
        runs["n"] += 1
        ctx.rng.draw("acceptance", "contract_test_entity", 7, "2026-10-01", "k", 0)
        ctx.services.ledger.post([LedgerEntry(1, LedgerCategory.CONTRACT_TEST_DEBIT, 0)])
        raise EngineError(ErrorCode.INSUFFICIENT_CASH, "not enough")

    conn, rid, sv, ledger, _ = _services(active_invocation=True)
    r1 = command(sv, "jobs.hire.5", fn)
    r2 = command(sv, "jobs.hire.5", fn)
    assert r1 == r2
    assert json.loads(r1)["error"]["code"] == "insufficient_cash"
    assert runs["n"] == 1  # not re-run on retry
    assert ledger.entry_count() == 0  # rolled back
    assert _audit_count(conn, rid) == 0  # draw rolled back with its txn


def test_failed_validation_does_not_consume_a_draw_or_write():
    def fn(ctx):
        ctx.rng.draw("acceptance", "A", 1, "s", "k", 0)
        ctx.services.ledger.post([LedgerEntry(1, LedgerCategory.CONTRACT_TEST_DEBIT, 0)])
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, "no such job")

    conn, rid, sv, ledger, _ = _services(active_invocation=True)
    r = command(sv, "jobs.x", fn)
    assert json.loads(r)["error"]["code"] == "entity_not_found"
    assert ledger.entry_count() == 0
    assert _audit_count(conn, rid) == 0


def test_invocation_conflict_error_is_not_memoized():
    """Pins the :data:`NON_CACHEABLE_ERRORS` rule (txn.py:62) directly: a
    transient world-state precondition (an invocation not being active right
    now) must be re-evaluated on every attempt, not cached forever."""
    conn, rid, sv, _, _ = _services(active_invocation=False)
    calls = {"n": 0}

    def fn(ctx):
        calls["n"] += 1
        return {}

    r1 = command(sv, "req.conflict", fn)
    r2 = command(sv, "req.conflict", fn)
    assert json.loads(r1)["error"]["code"] == "invocation_conflict"
    assert json.loads(r2)["error"]["code"] == "invocation_conflict"
    assert calls["n"] == 0  # engine-level check rejects before fn ever runs
    rows = conn.execute(
        "SELECT * FROM _command_responses WHERE run_id=? AND request_id=?",
        (rid, "req.conflict"),
    ).fetchall()
    assert rows == []  # never memoized under this request id


# ---------------------------------------------------------------------------
# Unexpected (non-EngineError) failures (spec 01 §Failure and transaction
# semantics, specs/01-run-contracts.md:222; docs/CSR-001.md chunk-6 review)
# ---------------------------------------------------------------------------


def test_unexpected_exception_returns_structured_error_and_rolls_back():
    calls = {"n": 0}

    def fn(ctx):
        calls["n"] += 1
        ctx.rng.draw("acceptance", "contract_test_entity", 9, "2026-10-01", "k", 0)
        ctx.services.ledger.post([LedgerEntry(1, LedgerCategory.CONTRACT_TEST_DEBIT, 0)])
        raise RuntimeError("adapter bug")

    conn, rid, sv, ledger, _ = _services(active_invocation=True)
    r = command(sv, "req.boom", fn)
    body = json.loads(r)
    assert body["ok"] is False
    assert body["error"]["code"] == "unexpected_failure"
    assert calls["n"] == 1
    assert conn.in_transaction is False  # rolled back, not left open
    assert ledger.entry_count() == 0  # write rolled back with the txn
    assert _audit_count(conn, rid) == 0  # draw rolled back with the txn


def test_unexpected_exception_is_not_memoized_and_retry_reruns_fn():
    calls = {"n": 0}

    def fn(ctx):
        calls["n"] += 1
        raise TypeError("boom")

    conn, rid, sv, _, _ = _services(active_invocation=True)
    r1 = command(sv, "req.boom2", fn)
    r2 = command(sv, "req.boom2", fn)
    assert calls["n"] == 2  # not memoized: fn ran again on the retry
    assert json.loads(r1)["error"]["code"] == "unexpected_failure"
    assert json.loads(r2)["error"]["code"] == "unexpected_failure"
    rows = conn.execute(
        "SELECT * FROM _command_responses WHERE run_id=? AND request_id=?",
        (rid, "req.boom2"),
    ).fetchall()
    assert rows == []  # no stored response row for this request id


def test_command_succeeds_after_a_prior_unexpected_failure_on_same_connection():
    """A crash mid-transaction must not leave the connection stuck 'inside' a
    transaction — the very next command on the same connection must succeed."""

    def boom(ctx):
        raise RuntimeError("adapter bug")

    conn, rid, sv, _, _ = _services(active_invocation=True)
    command(sv, "req.boom3", boom)
    assert conn.in_transaction is False

    r = command(sv, "req.ok", lambda ctx: {"done": True})
    assert json.loads(r) == {"ok": True, "result": {"done": True}}


def test_non_serializable_payload_rolls_back_and_recovers_connection():
    """The success tail (``ok_response``, ``_store_response``, the bankruptcy
    hook, ``commit``) runs *after* ``fn`` returns and must fail through the
    same rollback/UNEXPECTED_FAILURE funnel as a bug inside ``fn`` itself —
    not leave the transaction open with ``fn``'s writes pending (spec 01
    §Failure and transaction semantics, specs/01-run-contracts.md:222;
    docs/CSR-001.md chunk-6 review)."""

    def fn(ctx):
        ctx.services.ledger.post([LedgerEntry(-5000, LedgerCategory.CONTRACT_TEST_DEBIT, 0)])
        return {"tags": {"a", "b"}}  # a set is not JSON serializable

    conn, rid, sv, ledger, _ = _services(active_invocation=True)
    r = command(sv, "req.badpayload", fn)
    body = json.loads(r)
    assert body["ok"] is False
    assert body["error"]["code"] == "unexpected_failure"
    assert conn.in_transaction is False  # not left open mid-transaction
    assert ledger.entry_count() == 0  # the pending ledger row rolled back

    # the very next command on the same connection must still succeed.
    r2 = command(sv, "req.ok2", lambda ctx: {"done": True})
    assert json.loads(r2) == {"ok": True, "result": {"done": True}}


# ---------------------------------------------------------------------------
# Bankruptcy after a committed financial effect
# ---------------------------------------------------------------------------


def test_privileged_settlement_pushing_negative_flips_bankrupt_atomically():
    conn, rid, sv, ledger, _ = _services(active_invocation=False)
    ring = {"n": 0}

    def fn(ctx):
        ring["n"] += 1
        ctx.services.ledger.post(
            [
                LedgerEntry(1000, LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 0),
                LedgerEntry(-1500, LedgerCategory.CONTRACT_TEST_DEBIT, 1),
            ]
        )
        return {"settled": True}

    r = privileged(sv, privileged_request_id("settle", "day_1"), fn)
    assert json.loads(r)["ok"] is True
    assert ring["n"] == 1
    row = conn.execute("SELECT status FROM run WHERE run_id=?", (rid,)).fetchone()
    assert row["status"] == "bankrupt"
    # ledger entries committed (settle is unavoidable), balance negative
    assert ledger.cash_balance() == -500


def test_privileged_settlement_idempotent_via_reserved_id():
    n = {"n": 0}

    def fn(ctx):
        n["n"] += 1
        ctx.services.ledger.post([LedgerEntry(100, LedgerCategory.CONTRACT_TEST_CREDIT, 0)])
        return {}

    conn, rid, sv, ledger, _ = _services(active_invocation=False)
    ridp = privileged_request_id("settle", "day_2")
    r1 = privileged(sv, ridp, fn)
    r2 = privileged(sv, ridp, fn)
    assert r1 == r2 and json.loads(r1)["ok"] is True
    assert n["n"] == 1
    assert ledger.entry_count() == 1  # no duplicate settlement


def test_privileged_id_isolated_from_public_namespace_table():
    conn, rid, sv, _, _ = _services(active_invocation=True)
    # same request_id string used in both namespaces must not collide
    ridp = privileged_request_id("note", "x")  # __control__:note:x
    # public command rejects reserved id outright (uniform auth)
    with pytest.raises(EngineError):
        command(sv, ridp, lambda ctx: None)  # type: ignore[arg-type]
    # but the privileged path can store under that id without colliding with public
    r = privileged(sv, ridp, lambda ctx: {"note": True})
    assert json.loads(r)["ok"] is True
    rows = conn.execute(
        "SELECT namespace FROM _command_responses WHERE request_id=?", (ridp,)
    ).fetchall()
    assert {x["namespace"] for x in rows} == {PRIVILEGED_NAMESPACE}


# ---------------------------------------------------------------------------
# Counters / response record namespaces
# ---------------------------------------------------------------------------


def test_no_response_record_outside_run_active_commands():
    conn, rid, sv, _, _ = _services(status=RunStatus.INITIALIZING, active_invocation=True)
    command(sv, "r.x", lambda ctx: None)  # type: ignore[arg-type]
    rows = conn.execute("SELECT * FROM _command_responses WHERE run_id=?", (rid,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["namespace"] == PUBLIC_NAMESPACE
