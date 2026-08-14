from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from _helpers import audit_count, make_services
from csr_bench.enums import EntityType, EventKind, KeyedStreamName, LedgerCategory, RunStatus
from csr_bench.ledger import LedgerEntry
from csr_bench.money import Money
from csr_bench.txn import command, privileged, privileged_request_id

OPENING_CASH_CENTS = 15_000

ENTITY = EntityType.CUSTOMER.value
SCOPE = "2026-10-01"
EVENT_KIND = EventKind.DEMAND_GENERATION.value

_actions = st.one_of(
    st.tuples(st.just("revenue"), st.integers(min_value=1, max_value=10_000)),
    st.tuples(st.just("spend"), st.integers(min_value=1, max_value=60_000)),
    st.tuples(
        st.just("settle"), st.integers(min_value=-30_000, max_value=10_000).filter(lambda n: n != 0)
    ),
)


def _revenue_fn(amount_cents: int):
    def fn(ctx):
        ctx.services.ledger.post(
            [LedgerEntry(LedgerCategory.REVENUE, amount_cents, ctx.now_ms)]
        )
        return {"amount_cents": amount_cents}

    return fn


def _spend_fn(amount_cents: int, step: int):
    def fn(ctx):
        ctx.services.ledger.require_cash(amount_cents)
        ctx.rng.draw(
            KeyedStreamName.ACCEPTANCE.value,
            ENTITY,
            1,
            SCOPE,
            EVENT_KIND,
            step,
            outcome_id=f"spend:{step}",
        )
        ctx.services.ledger.post(
            [LedgerEntry(LedgerCategory.OPERATING_OVERHEAD, -amount_cents, ctx.now_ms)]
        )
        return {"amount_cents": amount_cents}

    return fn


def _settle_fn(delta_cents: int):
    def fn(ctx):
        category = (
            LedgerCategory.REVENUE
            if delta_cents >= 0
            else LedgerCategory.OPERATING_OVERHEAD
        )
        ctx.services.ledger.post([LedgerEntry(category, delta_cents, ctx.now_ms)])
        return {"delta_cents": delta_cents}

    return fn


def _open_balance_fn(ctx):
    ctx.services.ledger.post(
        [LedgerEntry(LedgerCategory.OPENING_BALANCE_ADJUSTMENT, OPENING_CASH_CENTS, 0)]
    )
    return {"opened": True}


@settings(deadline=None, max_examples=60)
@given(st.lists(_actions, max_size=12))
def test_money_integrity_over_arbitrary_committed_sequences(actions):
    conn, rid, services, ledger, _events, rng = make_services(active_invocation=True)

    opened = json.loads(
        privileged(services, privileged_request_id("open_bal", "x"), _open_balance_fn)
    )
    assert opened["ok"] is True

    expected = Money(OPENING_CASH_CENTS)
    expected_rows = 1
    model_bankrupt = False

    for step, (kind, amount) in enumerate(actions):
        pre_rows = ledger.entry_count()
        pre_audit = audit_count(conn)

        if kind == "revenue":
            resp = command(services, f"step:{step}", _revenue_fn(amount))
        elif kind == "spend":
            resp = command(services, f"step:{step}", _spend_fn(amount, step))
        else:
            resp = privileged(services, privileged_request_id("settle", step), _settle_fn(amount))
        body = json.loads(resp)

        if model_bankrupt:
            assert body["ok"] is False
            assert body["error"]["code"] == "invalid_run_status"
            assert ledger.entry_count() == pre_rows
            assert audit_count(conn) == pre_audit
            continue

        if kind == "spend" and amount > int(expected):
            assert body["ok"] is False
            assert body["error"]["code"] == "insufficient_cash"
            assert ledger.entry_count() == pre_rows
            assert audit_count(conn) == pre_audit
            continue

        assert body["ok"] is True, body
        expected_rows += 1
        signed = amount if kind != "spend" else -amount
        expected = expected + Money(signed)

        assert ledger.entry_count() == expected_rows
        assert ledger.cash_balance_money() == expected
        assert ledger.cash_balance() == int(expected)
        if kind == "spend":
            assert audit_count(conn) == pre_audit + 1

        if int(expected) < 0:
            model_bankrupt = True

    assert ledger.cash_balance() == int(expected)
    assert ledger.cash_balance_money() == expected

    row = conn.execute("SELECT status FROM _run").fetchone()
    expected_status = RunStatus.BANKRUPT if model_bankrupt else RunStatus.ACTIVE
    assert row["status"] == expected_status.value
