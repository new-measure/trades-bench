from __future__ import annotations

import json

from _helpers import make_services
from csr_bench import clock
from csr_bench.enums import EventKind, InvocationType, LedgerCategory
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.ledger import LedgerEntry
from csr_bench.txn import command


def test_open_and_sequence_and_active():
    conn, rid, sv, ledger, events, rng = make_services()
    i1 = sv.invocations.open(InvocationType.OPENING, 100, services=sv)
    i2 = None
    row = sv.invocations.active()
    assert row["invocation_id"] == i1
    assert row["sequence"] == 1
    assert row["status"] == "active"
    sv.invocations.complete(i1, services=sv)
    assert sv.invocations.active() is None
    i2 = sv.invocations.open(InvocationType.OPENING, 110, services=sv)
    assert i2 == i1 + 1
    sv.invocations.complete(i2, services=sv)


def test_open_second_invocation_fails_exclusivity():
    conn, rid, sv, ledger, events, rng = make_services()
    i1 = sv.invocations.open(InvocationType.CALL, 100, services=sv)
    r = sv.invocations.open(InvocationType.OPENING, 110, services=sv)
    assert json.loads(r)["error"]["code"] == "invocation_conflict"
    n = conn.execute("SELECT COUNT(*) c FROM _invocations").fetchone()["c"]
    assert n == 1
    assert sv.invocations.active()["invocation_id"] == i1


def test_complete_terminal_and_idempotent_violations():
    conn, rid, sv, ledger, events, rng = make_services()
    i = sv.invocations.open(InvocationType.OPENING, 100, services=sv)
    r = sv.invocations.complete(i, services=sv)
    assert json.loads(r)["ok"] is True
    r2 = sv.invocations.complete(i, services=sv)
    assert json.loads(r2)["error"]["code"] == "state_illegal"


def test_invocation_freezes_current_time_for_commands():
    conn, rid, sv, ledger, events, rng = make_services()
    ts = 5_000
    iv = sv.invocations.open(InvocationType.OPENING, ts, services=sv)
    frozen = {"v": None}

    def fn(ctx):
        frozen["v"] = ctx.now_ms
        return {"inv": iv}

    command(sv, "noop.read.1", fn)
    assert frozen["v"] == ts
    assert clock.now(conn) == ts
    sv.invocations.complete(iv, services=sv)


def test_world_event_cannot_commit_while_invocation_active():
    conn, rid, sv, ledger, events, rng = make_services()
    conn.execute("BEGIN")
    rng.enter_txn()
    eid = events.schedule(EventKind.BLOCK_LOCK, 50)
    rng.exit_txn()
    conn.execute("COMMIT")
    sv.invocations.open(InvocationType.OPENING, 60, services=sv)

    r = events.apply(eid, lambda ctx, e: None, services=sv)
    assert json.loads(r)["error"]["code"] == "invocation_conflict"
    assert events.status(eid).value == "pending"
    sv.invocations.complete(sv.invocations.active()["invocation_id"], services=sv)
    r2 = events.apply(eid, lambda ctx, e: None, services=sv)
    assert json.loads(r2)["ok"] is True
    assert events.status(eid).value == "applied"


def test_record_attempt_and_three_attempt_terminal():
    conn, rid, sv, ledger, events, rng = make_services()
    i = sv.invocations.open(InvocationType.CALL, 100, services=sv)
    for expect in (1, 2, 3):
        r = sv.invocations.record_attempt(i, services=sv)
        assert json.loads(r)["result"]["attempt_count"] == expect
    r = sv.invocations.mark_call_unanswered(i, services=sv)
    result = json.loads(r)["result"]
    assert result["status"] == "completed"
    assert result["outcome"] == "unanswered"
    assert sv.invocations.active() is None


def test_mark_unanswered_before_three_attempts_rejected():
    conn, rid, sv, ledger, events, rng = make_services()
    i = sv.invocations.open(InvocationType.CALL, 100, services=sv)
    sv.invocations.record_attempt(i, services=sv)
    r = sv.invocations.mark_call_unanswered(i, services=sv)
    assert json.loads(r)["error"]["code"] == "state_illegal"
    sv.invocations.record_attempt(i, services=sv)
    sv.invocations.record_attempt(i, services=sv)
    r2 = sv.invocations.mark_call_unanswered(i, services=sv)
    assert json.loads(r2)["ok"] is True
    assert sv.invocations.active() is None


def test_mark_unanswered_requires_call_invocation():
    conn, rid, sv, ledger, events, rng = make_services()
    i = sv.invocations.open(InvocationType.OPENING, 100, services=sv)
    r = sv.invocations.mark_call_unanswered(i, services=sv)
    assert json.loads(r)["error"]["code"] == "state_illegal"
    sv.invocations.complete(i, services=sv)


def _closing_restricted_command(services, request_id, target_ts):

    def fn(ctx):
        inv = ctx.invocation
        if inv is not None and inv["type"] == "closing" and target_ts <= inv["ts_ms"]:
            raise EngineError(ErrorCode.CLOSING_RESTRICTED, "closing is future-facing only")
        ctx.services.ledger.post([LedgerEntry(LedgerCategory.REVENUE, 1, target_ts)])
        return {"target_ts": target_ts}

    return command(services, request_id, fn)


def test_closing_invocation_rejects_finished_day_mutation_and_allows_future():
    conn, rid, sv, ledger, events, rng = make_services()
    closing_ts = 10_000
    sv.invocations.open(InvocationType.CLOSING, closing_ts, services=sv)

    r_bad = _closing_restricted_command(sv, "schedule.yesterday", closing_ts - 1)
    assert json.loads(r_bad)["error"]["code"] == "closing_restricted"
    assert ledger.entry_count() == 0

    r_ok = _closing_restricted_command(sv, "schedule.tomorrow", closing_ts + 1)
    assert json.loads(r_ok)["ok"] is True
    assert ledger.entry_count() == 1
    sv.invocations.complete(sv.invocations.active()["invocation_id"], services=sv)


def test_non_closing_invocation_allows_current_time_mutation():
    conn, rid, sv, ledger, events, rng = make_services()
    sv.invocations.open(InvocationType.CALL, 5_000, services=sv)
    r = _closing_restricted_command(sv, "job.book", 5_000)
    assert json.loads(r)["ok"] is True
    sv.invocations.complete(sv.invocations.active()["invocation_id"], services=sv)
