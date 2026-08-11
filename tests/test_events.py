from __future__ import annotations

import json

import pytest

from _helpers import audit_count, make_services
from csr_bench.db import begin, commit
from csr_bench.enums import EventKind, EventStatus, LedgerCategory
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.ledger import LedgerEntry
from csr_bench.txn import privileged, privileged_request_id


def test_schedule_requires_open_txn():
    conn, rid, sv, _, events, _ = make_services()
    with pytest.raises(EngineError) as ei:
        events.schedule(EventKind.CONTRACT_TEST_EVENT, 100, 0)
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN


def test_schedule_inside_txn_inserts_pending_event_with_monotonic_id():
    conn, rid, sv, _, events, rng = make_services()
    begin(conn)
    rng.enter_txn()
    e1 = events.schedule(EventKind.CONTRACT_TEST_EVENT, 100, 5)
    e2 = events.schedule(EventKind.CONTRACT_TEST_EVENT, 90, 0)
    rng.exit_txn()
    commit(conn)
    assert e1 == 1 and e2 == 2
    assert events.status(e1) is EventStatus.PENDING
    order = [r["event_id"] for r in events.pending_in_order()]
    assert order == [e2, e1]


def test_next_pending_orders_by_scheduled_at_priority_event_id():
    conn, rid, sv, _, events, rng = make_services()
    begin(conn)
    rng.enter_txn()
    a = events.schedule(EventKind.CONTRACT_TEST_EVENT, 100, 5)
    b = events.schedule(EventKind.CONTRACT_TEST_EVENT, 100, 1)
    c = events.schedule(EventKind.CONTRACT_TEST_EVENT, 100, 1)
    rng.exit_txn()
    commit(conn)
    seq = [r["event_id"] for r in events.pending_in_order()]
    assert seq == [b, c, a]
    assert events.next_pending()["event_id"] == b


def test_payload_canonical_sorted_on_storage():
    conn, rid, sv, _, events, rng = make_services()
    begin(conn)
    rng.enter_txn()
    eid = events.schedule(
        EventKind.CONTRACT_TEST_EVENT, 10, 0, payload={"b": 1, "a": 2, "z": [3, 2, 1]}
    )
    rng.exit_txn()
    commit(conn)
    row = events.get(eid)
    assert json.loads(row["payload"]) == {
        "a": 2,
        "b": 1,
        "z": [3, 2, 1],
    }


def test_cancel_pending_event_then_cannot_reapply_as_pending():
    conn, rid, sv, _, events, rng = make_services()
    begin(conn)
    rng.enter_txn()
    eid = events.schedule(EventKind.CONTRACT_TEST_EVENT, 10, 0)
    rng.exit_txn()
    commit(conn)
    begin(conn)
    rng.enter_txn()
    events.cancel(eid)
    rng.exit_txn()
    commit(conn)
    assert events.status(eid) is EventStatus.CANCELLED
    begin(conn)
    rng.enter_txn()
    with pytest.raises(EngineError):
        events.cancel(eid)
    rng.exit_txn()
    commit(conn)


def test_apply_atomic_with_draws_ledger_nested_events_and_marks_applied():
    conn, rid, sv, ledger, events, rng = make_services()
    begin(conn)
    rng.enter_txn()
    eid = events.schedule(EventKind.CONTRACT_TEST_EVENT, 50, 0)
    rng.exit_txn()
    commit(conn)

    calls = {"n": 0}

    def handler(ctx, event):
        calls["n"] += 1
        ctx.rng.draw(
            "acceptance",
            "contract_test_entity",
            event["entity_id"] or 1,
            "2026-10-01",
            "contract_test_event",
            0,
            outcome_id="ev",
        )
        ctx.services.ledger.post(
            [
                LedgerEntry(
                    LedgerCategory.REVENUE,
                    700,
                    50,
                )
            ]
        )
        ctx.services.events.schedule(EventKind.CONTRACT_TEST_EVENT, 200, 0)

    r = events.apply(eid, handler, services=sv)
    assert json.loads(r)["ok"] is True
    assert events.status(eid) is EventStatus.APPLIED
    assert events.applied_count() == 1
    assert ledger.entry_count() == 1
    assert audit_count(conn) == 1
    assert len(events.pending_in_order()) == 1


def test_re_apply_is_idempotent_never_reapplies():
    conn, rid, sv, ledger, events, rng = make_services()
    begin(conn)
    rng.enter_txn()
    make_schedule(events)
    rng.exit_txn()
    commit(conn)
    eid = 1
    calls = {"n": 0}

    def handler(ctx, event):
        calls["n"] += 1
        ctx.services.ledger.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1, 0)])

    r1 = events.apply(eid, handler, services=sv)
    r2 = events.apply(eid, handler, services=sv)
    assert r1 == r2
    assert calls["n"] == 1
    assert ledger.entry_count() == 1
    assert events.applied_count() == 1


def make_schedule(events):
    events.schedule(EventKind.CONTRACT_TEST_EVENT, 50, 0)


def test_apply_rejects_when_invocation_active_and_leaves_no_trace():
    conn, rid, sv, ledger, events, rng = make_services(active_invocation=True)
    begin(conn)
    rng.enter_txn()
    eid = events.schedule(EventKind.CONTRACT_TEST_EVENT, 50, 0)
    rng.exit_txn()
    commit(conn)
    drew = {"n": 0}

    def handler(ctx, event):
        drew["n"] += 1
        ctx.rng.draw("acceptance", "A", 1, "s", "k", 0)
        ctx.services.ledger.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1, 0)])

    r = events.apply(eid, handler, services=sv)
    assert json.loads(r)["error"]["code"] == "invocation_conflict"
    assert drew["n"] == 0
    assert events.status(eid) is EventStatus.PENDING
    assert ledger.entry_count() == 0
    assert audit_count(conn) == 0


def test_apply_rolls_back_on_handler_failure_and_is_idempotent():
    conn, rid, sv, ledger, events, rng = make_services()
    begin(conn)
    rng.enter_txn()
    make_schedule(events)
    rng.exit_txn()
    commit(conn)
    eid = 1
    runs = {"n": 0}

    def handler(ctx, event):
        runs["n"] += 1
        ctx.rng.draw("acceptance", "A", 1, "s", "k", 0)
        ctx.services.ledger.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1, 0)])
        raise EngineError(ErrorCode.STATE_ILLEGAL, "boom")

    r1 = events.apply(eid, handler, services=sv)
    r2 = events.apply(eid, handler, services=sv)
    assert r1 == r2
    assert json.loads(r1)["error"]["code"] == "state_illegal"
    assert runs["n"] == 1
    assert events.status(eid) is EventStatus.PENDING
    assert ledger.entry_count() == 0
    assert audit_count(conn) == 0


def test_apply_event_bankruptcy_via_unavoidable_settle_event():
    conn, rid, sv, ledger, events, rng = make_services(active_invocation=False)
    begin(conn)
    rng.enter_txn()
    ledger.post([LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, 1000, 0)])
    rng.exit_txn()
    commit(conn)
    begin(conn)
    rng.enter_txn()
    eid = events.schedule(EventKind.CONTRACT_TEST_SETTLE_DEBIT, 5, 0)
    rng.exit_txn()
    commit(conn)

    def handler(ctx, event):
        ctx.services.ledger.post([LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -1500, 5)])

    events.apply(eid, handler, services=sv)
    assert ledger.cash_balance() == -500
    row = conn.execute("SELECT status FROM _run").fetchone()
    assert row["status"] == "bankrupt"


def test_cancel_via_privileged_id_stable():
    ridp = privileged_request_id("apply_event", 99)
    assert ridp == "__control__:apply_event:99"
    assert privileged.__name__ == "privileged"
