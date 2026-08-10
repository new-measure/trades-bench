"""Hypothesis property test: money integrity (spec 01 §Money and bankruptcy;
docs/CSR-001.md decision 4; acceptance #7, ACC-01/03/04 shape).

Generates arbitrary sequences mixing revenue posts, discretionary spends of
varied amounts (including deliberate overspends), and unavoidable privileged
settlements that may push cash negative. Every step runs through the **real**
:func:`csr_bench.txn.command` / :func:`csr_bench.txn.privileged` machinery, and
every discretionary spend calls the real :meth:`csr_bench.ledger.Ledger.require_cash`
guard before any write or draw — nothing here re-implements the guard as a test
double.

Request ids are ``f"step:{step}"`` / ``privileged_request_id("settle", step)``,
i.e. a pure function of the step's position in the drawn sequence: hypothesis
supplies the randomness that picks the sequence, but the engine itself never
sees anything but a deterministic list of actions.

After each step the test independently predicts, from a hand-tracked
:class:`~csr_bench.money.Money` running total, what the engine should have
done — success/failure and the resulting balance — and checks the real
response and ledger state against that prediction. The final assertions are
the spec properties:

  (a) ``cash_balance()`` == opening adjustment + sum of all committed ledger
      rows, always;
  (b) every overspend attempt failed atomically with ``INSUFFICIENT_CASH`` —
      no ledger row, no draw-audit row, no partial write from the failed
      command;
  (c) if any unavoidable settlement pushed cash below zero, the run is
      ``bankrupt`` and every later transaction was refused with
      ``invalid_run_status``; if cash never went negative, the run is still
      ``active``.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from _helpers import audit_count, make_services
from csr_bench.enums import EntityType, EventKind, KeyedStreamName, LedgerCategory, RunStatus
from csr_bench.ledger import LedgerEntry
from csr_bench.money import Money
from csr_bench.txn import command, privileged, privileged_request_id

#: Deliberately small relative to the spend range below, so most sequences
#: contain at least one genuine overspend.
OPENING_CASH_CENTS = 15_000

ENTITY = EntityType.CONTRACT_TEST_ENTITY.value
SCOPE = "2026-10-01"
EVENT_KIND = EventKind.CONTRACT_TEST_EVENT.value

#: (kind, amount_cents). "settle" amounts may be negative (unavoidable debit,
#: e.g. payroll) or positive (e.g. daily revenue) — either can be drawn.
_actions = st.one_of(
    # revenue is strictly positive under the spec-02 sign rules (a zero-cent
    # credit is neither sign), so the strategy never draws 0 for it.
    st.tuples(st.just("revenue"), st.integers(min_value=1, max_value=10_000)),
    st.tuples(st.just("spend"), st.integers(min_value=1, max_value=60_000)),
    # A zero settle has no legal entry type under the spec-02 sign rules
    # (revenue is strictly positive, operating_overhead strictly negative), so
    # the strategy never draws it.
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
        # validation stage: cash sufficiency, before any write or draw
        # (spec 01 §Command transactions validation order).
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
            # (c) halted: every later transaction refused, no trace.
            assert body["ok"] is False
            assert body["error"]["code"] == "invalid_run_status"
            assert ledger.entry_count() == pre_rows
            assert audit_count(conn) == pre_audit
            continue

        if kind == "spend" and amount > int(expected):
            # (b) overspend: atomic rejection, before any write or draw.
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
        assert ledger.cash_balance_money() == expected  # (a) after each step
        assert ledger.cash_balance() == int(expected)
        if kind == "spend":
            # A successful spend draws exactly once (_spend_fn) — the audit
            # count must actually increase, so the pre==post checks on the
            # rejection paths above are not vacuously true.
            assert audit_count(conn) == pre_audit + 1

        if int(expected) < 0:
            model_bankrupt = True

    # (a) final reconciliation against the independently-tracked total.
    assert ledger.cash_balance() == int(expected)
    assert ledger.cash_balance_money() == expected

    # (c) terminal status matches the model.
    row = conn.execute("SELECT status FROM _run").fetchone()
    expected_status = RunStatus.BANKRUPT if model_bankrupt else RunStatus.ACTIVE
    assert row["status"] == expected_status.value
