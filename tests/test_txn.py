from __future__ import annotations

import json

import pytest

from _helpers import register_conn
from csr_bench.db import in_memory, insert_run, new_run_id
from csr_bench.enums import LedgerCategory, RunStatus
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.ledger import Ledger, LedgerEntry
from csr_bench.migrations import migrate
from csr_bench.rng import Rng, sqlite_audit_recorder
from csr_bench.txn import (
    PRIVILEGED_NAMESPACE,
    PUBLIC_NAMESPACE,
    Services,
    command,
    is_reserved,
    privileged,
    privileged_request_id,
)


class FakeInvocations:
    def __init__(self, conn):
        self.conn = conn

    def active(self):
        row = self.conn.execute(
            "SELECT * FROM _invocations WHERE status='active' "
            "ORDER BY sequence DESC LIMIT 1",
        ).fetchone()
        return row


def _services(*, active_invocation=False, status=RunStatus.ACTIVE, with_ledger=True):
    conn = in_memory()
    register_conn(conn)
    migrate(conn)
    rid = new_run_id()
    conn.execute("BEGIN")
    insert_run(
        conn,
        run_id=rid,
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        master_seed=42,
        start_at_ms=0,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        status=status,
    )
    conn.execute("COMMIT")
    rng = Rng(42, "1.0.0", audit_recorder=sqlite_audit_recorder(conn), conn=conn)
    ledger = Ledger(conn) if with_ledger else None
    inv = FakeInvocations(conn)
    if active_invocation:
        conn.execute(
            "INSERT INTO _invocations(type, ts_ms, sequence, status, attempt_count) "
            "VALUES ('call', 0, 1, 'active', 1)"
        )
    sv = Services(conn=conn, rng=rng, ledger=ledger, invocations=inv)
    return conn, rid, sv, ledger, rng


def _audit_count(conn):
    return conn.execute("SELECT COUNT(*) c FROM _random_audit").fetchone()["c"]


def test_command_rejects_reserved_request_id_with_uniform_error():
    conn, rid, sv, _, _ = _services(active_invocation=True)
    with pytest.raises(EngineError) as ei:
        command(
            sv, privileged_request_id("apply_event", 7), lambda ctx: {},
            operation="test.op", args={},
        )
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
    ridp = privileged_request_id("settle", "day_1")
    with pytest.raises(EngineError) as ei:
        command(sv, ridp, lambda ctx: {"ok": True}, operation="test.op", args={})
    assert ei.value.code is ErrorCode.UNAUTHORIZED


def test_command_fails_when_run_not_active_and_fn_never_runs():
    calls = {"n": 0}

    def fn(ctx):
        calls["n"] += 1
        return {}

    conn, rid, sv, _, _ = _services(status=RunStatus.INITIALIZING, active_invocation=True)
    r1 = command(sv, "req.1", fn, operation="test.op", args={})
    r2 = command(sv, "req.1", fn, operation="test.op", args={})
    assert calls["n"] == 0
    assert r1 == r2
    assert json.loads(r1)["error"]["code"] == "invalid_run_status"


def test_command_fails_without_active_invocation():
    conn, rid, sv, _, rng = _services(active_invocation=False)
    drew = {"n": 0}

    def fn(ctx):
        drew["n"] += 1
        ctx.rng.draw("acceptance", "customer", 1, "2026-10-01", "k", 0)
        return {}

    r = command(sv, "req.2", fn, operation="test.op", args={})
    assert json.loads(r)["error"]["code"] == "invocation_conflict"
    assert drew["n"] == 0
    assert _audit_count(conn) == 0


def test_successful_command_idempotent_byte_for_byte_no_duplicates():
    ledger_calls = {"n": 0}

    def fn(ctx):
        ledger_calls["n"] += 1
        ctx.rng.draw(
            "acceptance", "customer", 7, "2026-10-01", "k", 0, outcome_id="call_42"
        )
        ctx.services.ledger.post(
            [
                LedgerEntry(
                    LedgerCategory.REVENUE,
                    5000,
                    0,
                )
            ]
        )
        return {"booked": True, "entity_id": 7}

    conn, rid, sv, ledger, _ = _services(active_invocation=True)
    r1 = command(sv, "jobs.book.5", fn, operation="test.op", args={})
    r2 = command(sv, "jobs.book.5", fn, operation="test.op", args={})
    assert r1 == r2
    assert json.loads(r1) == {"ok": True, "result": {"booked": True, "entity_id": 7}}
    assert ledger_calls["n"] == 1
    assert ledger.entry_count() == 1
    assert _audit_count(conn) == 1


def test_failed_command_is_idempotent_and_rolls_back_draws_and_writes():
    runs = {"n": 0}

    def fn(ctx):
        runs["n"] += 1
        ctx.rng.draw("acceptance", "customer", 7, "2026-10-01", "k", 0)
        ctx.services.ledger.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1, 0)])
        raise EngineError(ErrorCode.INSUFFICIENT_CASH, "not enough")

    conn, rid, sv, ledger, _ = _services(active_invocation=True)
    r1 = command(sv, "jobs.hire.5", fn, operation="test.op", args={})
    r2 = command(sv, "jobs.hire.5", fn, operation="test.op", args={})
    assert r1 == r2
    assert json.loads(r1)["error"]["code"] == "insufficient_cash"
    assert runs["n"] == 1
    assert ledger.entry_count() == 0
    assert _audit_count(conn) == 0


def test_failed_validation_does_not_consume_a_draw_or_write():
    def fn(ctx):
        ctx.rng.draw("acceptance", "A", 1, "s", "k", 0)
        ctx.services.ledger.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1, 0)])
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, "no such job")

    conn, rid, sv, ledger, _ = _services(active_invocation=True)
    r = command(sv, "jobs.x", fn, operation="test.op", args={})
    assert json.loads(r)["error"]["code"] == "entity_not_found"
    assert ledger.entry_count() == 0
    assert _audit_count(conn) == 0


def test_invocation_conflict_error_is_not_memoized():
    conn, rid, sv, _, _ = _services(active_invocation=False)
    calls = {"n": 0}

    def fn(ctx):
        calls["n"] += 1
        return {}

    r1 = command(sv, "req.conflict", fn, operation="test.op", args={})
    r2 = command(sv, "req.conflict", fn, operation="test.op", args={})
    assert json.loads(r1)["error"]["code"] == "invocation_conflict"
    assert json.loads(r2)["error"]["code"] == "invocation_conflict"
    assert calls["n"] == 0
    rows = conn.execute(
        "SELECT * FROM _idempotency WHERE request_id=?",
        ("req.conflict",),
    ).fetchall()
    assert rows == []


def test_unexpected_exception_returns_structured_error_and_rolls_back():
    calls = {"n": 0}

    def fn(ctx):
        calls["n"] += 1
        ctx.rng.draw("acceptance", "customer", 9, "2026-10-01", "k", 0)
        ctx.services.ledger.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1, 0)])
        raise RuntimeError("adapter bug")

    conn, rid, sv, ledger, _ = _services(active_invocation=True)
    r = command(sv, "req.boom", fn, operation="test.op", args={})
    body = json.loads(r)
    assert body["ok"] is False
    assert body["error"]["code"] == "unexpected_failure"
    assert calls["n"] == 1
    assert conn.in_transaction is False
    assert ledger.entry_count() == 0
    assert _audit_count(conn) == 0


def test_unexpected_exception_is_not_memoized_and_retry_reruns_fn():
    calls = {"n": 0}

    def fn(ctx):
        calls["n"] += 1
        raise TypeError("boom")

    conn, rid, sv, _, _ = _services(active_invocation=True)
    r1 = command(sv, "req.boom2", fn, operation="test.op", args={})
    r2 = command(sv, "req.boom2", fn, operation="test.op", args={})
    assert calls["n"] == 2
    assert json.loads(r1)["error"]["code"] == "unexpected_failure"
    assert json.loads(r2)["error"]["code"] == "unexpected_failure"
    rows = conn.execute(
        "SELECT * FROM _idempotency WHERE request_id=?",
        ("req.boom2",),
    ).fetchall()
    assert rows == []


def test_command_succeeds_after_a_prior_unexpected_failure_on_same_connection():

    def boom(ctx):
        raise RuntimeError("adapter bug")

    conn, rid, sv, _, _ = _services(active_invocation=True)
    command(sv, "req.boom3", boom, operation="test.op", args={})
    assert conn.in_transaction is False

    r = command(sv, "req.ok", lambda ctx: {"done": True}, operation="test.op", args={})
    assert json.loads(r) == {"ok": True, "result": {"done": True}}


def test_non_serializable_payload_rolls_back_and_recovers_connection():

    def fn(ctx):
        ctx.services.ledger.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -5000, 0)])
        return {"tags": {"a", "b"}}

    conn, rid, sv, ledger, _ = _services(active_invocation=True)
    r = command(sv, "req.badpayload", fn, operation="test.op", args={})
    body = json.loads(r)
    assert body["ok"] is False
    assert body["error"]["code"] == "unexpected_failure"
    assert conn.in_transaction is False
    assert ledger.entry_count() == 0

    r2 = command(sv, "req.ok2", lambda ctx: {"done": True}, operation="test.op", args={})
    assert json.loads(r2) == {"ok": True, "result": {"done": True}}


def test_privileged_settlement_pushing_negative_flips_bankrupt_atomically():
    conn, rid, sv, ledger, _ = _services(active_invocation=False)
    ring = {"n": 0}

    def fn(ctx):
        ring["n"] += 1
        ctx.services.ledger.post(
            [
                LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 1000, 0),
                LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1500, 1),
            ]
        )
        return {"settled": True}

    r = privileged(sv, privileged_request_id("settle", "day_1"), fn)
    assert json.loads(r)["ok"] is True
    assert ring["n"] == 1
    row = conn.execute("SELECT status FROM _run").fetchone()
    assert row["status"] == "bankrupt"
    assert ledger.cash_balance() == -500


def test_privileged_settlement_idempotent_via_reserved_id():
    n = {"n": 0}

    def fn(ctx):
        n["n"] += 1
        ctx.services.ledger.post([LedgerEntry(LedgerCategory.REVENUE, 100, 0)])
        return {}

    conn, rid, sv, ledger, _ = _services(active_invocation=False)
    ridp = privileged_request_id("settle", "day_2")
    r1 = privileged(sv, ridp, fn)
    r2 = privileged(sv, ridp, fn)
    assert r1 == r2 and json.loads(r1)["ok"] is True
    assert n["n"] == 1
    assert ledger.entry_count() == 1


def test_privileged_id_isolated_from_public_namespace_table():
    conn, rid, sv, _, _ = _services(active_invocation=True)
    ridp = privileged_request_id("note", "x")
    with pytest.raises(EngineError):
        command(sv, ridp, lambda ctx: None, operation="test.op", args={})  # type: ignore[arg-type]
    r = privileged(sv, ridp, lambda ctx: {"note": True})
    assert json.loads(r)["ok"] is True
    rows = conn.execute(
        "SELECT namespace FROM _idempotency WHERE request_id=?", (ridp,)
    ).fetchall()
    assert {x["namespace"] for x in rows} == {PRIVILEGED_NAMESPACE}


def test_no_response_record_outside_run_active_commands():
    conn, rid, sv, _, _ = _services(status=RunStatus.INITIALIZING, active_invocation=True)
    command(sv, "r.x", lambda ctx: None, operation="test.op", args={})  # type: ignore[arg-type]
    rows = conn.execute("SELECT * FROM _idempotency").fetchall()
    assert len(rows) == 1
    assert rows[0]["namespace"] == PUBLIC_NAMESPACE


def test_same_request_id_different_args_conflicts_without_running_fn():
    runs = {"n": 0}

    def fn(ctx):
        runs["n"] += 1
        ctx.rng.draw("acceptance", "customer", 1, "2026-10-01", "k", 0)
        return {"call_id": 1}

    conn, rid, sv, _, _ = _services(active_invocation=True)
    r1 = command(sv, "req.fp", fn, operation="calls.reject", args={"call_id": 1})
    assert json.loads(r1)["ok"] is True
    assert runs["n"] == 1
    assert _audit_count(conn) == 1

    r2 = command(sv, "req.fp", fn, operation="calls.reject", args={"call_id": 2})
    assert json.loads(r2)["error"]["code"] == "request_id_conflict"
    assert runs["n"] == 1
    assert _audit_count(conn) == 1

    rows = conn.execute(
        "SELECT response FROM _idempotency WHERE request_id=?", ("req.fp",)
    ).fetchall()
    assert len(rows) == 1
    assert bytes(rows[0]["response"]) == r1

    r3 = command(sv, "req.fp", fn, operation="calls.reject", args={"call_id": 1})
    assert r3 == r1
    assert runs["n"] == 1


def test_same_request_id_different_operation_conflicts():
    conn, rid, sv, _, _ = _services(active_invocation=True)
    r1 = command(sv, "req.op", lambda ctx: {"x": 1}, operation="test.a", args={"id": 5})
    assert json.loads(r1)["ok"] is True
    r2 = command(sv, "req.op", lambda ctx: {"x": 1}, operation="test.b", args={"id": 5})
    assert json.loads(r2)["error"]["code"] == "request_id_conflict"


def test_conflict_after_cached_error_preserves_original_error_replay():
    def fn(ctx):
        raise EngineError(ErrorCode.INSUFFICIENT_CASH, "not enough")

    conn, rid, sv, _, _ = _services(active_invocation=True)
    r1 = command(sv, "req.errfp", fn, operation="test.op", args={"id": 1})
    assert json.loads(r1)["error"]["code"] == "insufficient_cash"

    r2 = command(sv, "req.errfp", fn, operation="test.op", args={"id": 2})
    assert json.loads(r2)["error"]["code"] == "request_id_conflict"

    r3 = command(sv, "req.errfp", fn, operation="test.op", args={"id": 1})
    assert r3 == r1
