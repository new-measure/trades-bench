from __future__ import annotations

import json

from _helpers import make_services
from trades_bench import clock
from trades_bench.enums import EventKind, InvocationType, LedgerCategory
from trades_bench.ledger import LedgerEntry
from trades_bench.txn import command


def test_open_and_sequence_and_active():
    conn, rid, sv, ledger, events, rng = make_services()
    i1 = sv.invocations.open(InvocationType.OPENING, 100, request_id="req.open.1", services=sv)
    i2 = None
    row = sv.invocations.active()
    assert row["invocation_id"] == i1
    assert row["sequence"] == 1
    assert row["status"] == "active"
    sv.invocations.complete(i1, request_id="req.complete.1", services=sv)
    assert sv.invocations.active() is None
    i2 = sv.invocations.open(InvocationType.OPENING, 110, request_id="req.open.2", services=sv)
    assert i2 == i1 + 1
    sv.invocations.complete(i2, request_id="req.complete.2", services=sv)


def test_open_second_invocation_fails_exclusivity():
    conn, rid, sv, ledger, events, rng = make_services()
    i1 = sv.invocations.open(InvocationType.CALL, 100, request_id="req.open.1", services=sv)
    r = sv.invocations.open(InvocationType.OPENING, 110, request_id="req.open.2", services=sv)
    body = json.loads(r)
    assert body["error"]["code"] == "invocation_conflict"
    assert body["error"]["details"]["reason"] == "invocation_already_active"
    n = conn.execute("SELECT COUNT(*) c FROM _invocations").fetchone()["c"]
    assert n == 1
    assert sv.invocations.active()["invocation_id"] == i1


def test_complete_terminal_and_idempotent_violations():
    conn, rid, sv, ledger, events, rng = make_services()
    i = sv.invocations.open(InvocationType.OPENING, 100, request_id="req.open.1", services=sv)
    r = sv.invocations.complete(i, request_id="req.complete.1", services=sv)
    assert json.loads(r)["ok"] is True
    r2 = sv.invocations.complete(i, request_id="req.complete.2", services=sv)
    assert json.loads(r2)["error"]["code"] == "state_illegal"


def test_invocation_freezes_current_time_for_commands():
    conn, rid, sv, ledger, events, rng = make_services()
    ts = 5_000
    iv = sv.invocations.open(InvocationType.OPENING, ts, request_id="req.open.1", services=sv)
    frozen = {"v": None}

    def fn(ctx):
        frozen["v"] = ctx.now_ms
        return {"inv": iv}

    command(sv, "noop.read.1", fn, operation="test.op", args={})
    assert frozen["v"] == ts
    assert clock.now(conn) == ts
    sv.invocations.complete(iv, request_id="req.complete.1", services=sv)


def test_world_event_cannot_commit_while_invocation_active():
    conn, rid, sv, ledger, events, rng = make_services()
    conn.execute("BEGIN")
    rng.enter_txn()
    eid = events.schedule(EventKind.BLOCK_LOCK, 50)
    rng.exit_txn()
    conn.execute("COMMIT")
    sv.invocations.open(InvocationType.OPENING, 60, request_id="req.open.1", services=sv)

    r = events.apply(eid, lambda ctx, e: None, services=sv)
    body = json.loads(r)
    assert body["error"]["code"] == "invocation_conflict"
    assert events.status(eid).value == "pending"
    sv.invocations.complete(
        sv.invocations.active()["invocation_id"], request_id="req.complete.1", services=sv
    )
    r2 = events.apply(eid, lambda ctx, e: None, services=sv)
    assert json.loads(r2)["ok"] is True
    assert events.status(eid).value == "applied"


_CLOSING_DATE = "2026-10-01"
_NEXT_DATE = "2026-10-02"


def _closing_instant():
    return clock.phoenix_instant(2026, 10, 1, 19, 0)


def _gated_command(services, request_id, service_date, block_start):

    def fn(ctx):
        ctx.services.ledger.post([LedgerEntry(LedgerCategory.REVENUE, 1, ctx.now_ms)])
        return {"service_date": service_date, "block_start": block_start}

    return command(
        services,
        request_id,
        fn,
        operation="jobs.book",
        args={"service_date": service_date, "block_start": block_start},
    )


def test_closing_invocation_rejects_finished_day_mutation_and_allows_future():
    conn, rid, sv, ledger, events, rng = make_services()
    closing_ts = _closing_instant()
    sv.invocations.open(
        InvocationType.CLOSING, closing_ts, request_id="req.open.1", services=sv
    )

    r_bad = _gated_command(sv, "book.today", _CLOSING_DATE, "17:00")
    assert json.loads(r_bad)["error"]["code"] == "closing_restricted"
    assert ledger.entry_count() == 0

    r_ok = _gated_command(sv, "book.tomorrow", _NEXT_DATE, "09:00")
    assert json.loads(r_ok)["ok"] is True
    assert ledger.entry_count() == 1
    sv.invocations.complete(
        sv.invocations.active()["invocation_id"], request_id="req.complete.1", services=sv
    )


def test_non_closing_invocation_allows_current_time_mutation():
    conn, rid, sv, ledger, events, rng = make_services()
    sv.invocations.open(
        InvocationType.CALL, _closing_instant(), request_id="req.open.1", services=sv
    )
    r = _gated_command(sv, "book.now", _CLOSING_DATE, "17:00")
    assert json.loads(r)["ok"] is True
    sv.invocations.complete(
        sv.invocations.active()["invocation_id"], request_id="req.complete.1", services=sv
    )


def _idempotency_rows(conn, request_id):
    return conn.execute(
        "SELECT * FROM _idempotency WHERE request_id LIKE ?",
        (f"%{request_id}%",),
    ).fetchall()


def test_open_retried_with_same_request_id_replays_byte_identical():
    conn, rid, sv, ledger, events, rng = make_services()
    r1, replayed1 = sv.invocations.open_with_replay(
        InvocationType.OPENING, 100, request_id="req.open.dup", services=sv
    )
    r2, replayed2 = sv.invocations.open_with_replay(
        InvocationType.OPENING, 100, request_id="req.open.dup", services=sv
    )
    assert replayed1 is False
    assert replayed2 is True
    assert r1 == r2
    n = conn.execute("SELECT COUNT(*) c FROM _invocations").fetchone()["c"]
    assert n == 1


def test_open_retried_with_same_request_id_different_args_conflicts():
    conn, rid, sv, ledger, events, rng = make_services()
    sv.invocations.open(InvocationType.OPENING, 100, request_id="req.open.fp", services=sv)
    r2 = sv.invocations.open(InvocationType.CALL, 100, request_id="req.open.fp", services=sv)
    assert json.loads(r2)["error"]["code"] == "request_id_conflict"
    n = conn.execute("SELECT COUNT(*) c FROM _invocations").fetchone()["c"]
    assert n == 1


def _scheduled(conn, events, rng) -> int:
    conn.execute("BEGIN")
    rng.enter_txn()
    event_id = events.schedule(EventKind.OPENING_INVOCATION, 100)
    rng.exit_txn()
    conn.execute("COMMIT")
    return event_id


def test_open_stores_the_origin_event_id_and_defaults_it_to_null():
    conn, rid, sv, ledger, events, rng = make_services()
    event_id = _scheduled(conn, events, rng)
    iv = sv.invocations.open(
        InvocationType.OPENING, 100, request_id="req.open.origin",
        origin_event_id=event_id, services=sv,
    )
    assert sv.invocations.get(iv)["origin_event_id"] == event_id
    sv.invocations.complete(iv, request_id="req.complete.origin", services=sv)
    bare = sv.invocations.open(
        InvocationType.OPENING, 110, request_id="req.open.bare", services=sv
    )
    assert sv.invocations.get(bare)["origin_event_id"] is None


def test_open_retried_with_a_different_origin_event_conflicts():
    conn, rid, sv, ledger, events, rng = make_services()
    first = _scheduled(conn, events, rng)
    second = _scheduled(conn, events, rng)
    sv.invocations.open(
        InvocationType.OPENING, 100, request_id="req.open.origin.fp",
        origin_event_id=first, services=sv,
    )
    replayed = sv.invocations.open(
        InvocationType.OPENING, 100, request_id="req.open.origin.fp",
        origin_event_id=second, services=sv,
    )
    assert json.loads(replayed)["error"]["code"] == "request_id_conflict"
    assert conn.execute("SELECT COUNT(*) c FROM _invocations").fetchone()["c"] == 1


def test_complete_retried_with_same_request_id_replays_byte_identical():
    conn, rid, sv, ledger, events, rng = make_services()
    iv = sv.invocations.open(InvocationType.OPENING, 100, request_id="req.open.1", services=sv)
    r1, replayed1 = sv.invocations.complete_with_replay(
        iv, request_id="req.complete.dup", services=sv
    )
    r2, replayed2 = sv.invocations.complete_with_replay(
        iv, request_id="req.complete.dup", services=sv
    )
    assert replayed1 is False
    assert replayed2 is True
    assert r1 == r2
    assert json.loads(r1)["ok"] is True


def test_complete_retried_with_same_request_id_different_invocation_conflicts():
    conn, rid, sv, ledger, events, rng = make_services()
    i1 = sv.invocations.open(InvocationType.OPENING, 100, request_id="req.open.1", services=sv)
    sv.invocations.complete(i1, request_id="req.complete.fp", services=sv)
    i2 = sv.invocations.open(InvocationType.OPENING, 110, request_id="req.open.2", services=sv)
    r2 = sv.invocations.complete(i2, request_id="req.complete.fp", services=sv)
    assert json.loads(r2)["error"]["code"] == "request_id_conflict"


def test_c2_stale_ts_open_fails_cleanly_and_does_not_poison_the_next_attempt():
    conn, rid, sv, ledger, events, rng = make_services()
    i1 = sv.invocations.open(InvocationType.OPENING, 100, request_id="req.open.1", services=sv)
    sv.invocations.complete(i1, request_id="req.complete.1", services=sv)
    assert clock.now(conn) == 100

    r_stale = sv.invocations.open(
        InvocationType.OPENING, 50, request_id="req.open.stale", services=sv
    )
    body = json.loads(r_stale)
    assert body["ok"] is False
    assert body["error"]["code"] == "state_illegal"

    rows = _idempotency_rows(conn, "req.open.stale")
    assert rows == []
    n = conn.execute("SELECT COUNT(*) c FROM _invocations").fetchone()["c"]
    assert n == 1

    i2 = sv.invocations.open(InvocationType.OPENING, 110, request_id="req.open.recover", services=sv)
    assert isinstance(i2, int)
    row = sv.invocations.active()
    assert row["invocation_id"] == i2
    sv.invocations.complete(i2, request_id="req.complete.2", services=sv)
