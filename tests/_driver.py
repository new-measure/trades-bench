"""Scripted determinism driver (docs/CSR-001.md decision 6; tests only).

A tiny, fully deterministic "day" played through the **real** machinery: events
are popped in ``(scheduled_at, priority, event_id)`` order via
:class:`csr_bench.events.Events` with toy handlers, and a recorded script of fake
agent commands runs through :func:`csr_bench.txn.command` /
:func:`csr_bench.txn.privileged`, posting to :class:`csr_bench.ledger.Ledger` and
pulling both keyed draws and sequential-stream draws (``weather_process``,
``labor_economy``) so ``_rng_state`` genuinely matters at a resume boundary.

The script is :data:`SCRIPT`: :data:`N_COMMITS` numbered steps, each of which is
exactly **one committed transaction** — i.e. one resume boundary for DET-02.
Every step derives what it needs from committed rows (the active invocation, the
next pending event, the next invocation sequence), never from in-process state,
which is what lets a fresh process pick the script up mid-way.

Every timestamp is simulated (built with :func:`csr_bench.clock.phoenix_instant`);
wall clock never enters the database.

Also runnable as a subprocess entry point for the fresh-process proofs::

    python tests/_driver.py --db RUN.db --create --first 1 --last 12

which prints one canonical JSON line: the dump hash, the observed draws, and the
side notes the DET-04/05 test inspects.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from pathlib import Path

from csr_bench import SPEC_VERSION
from csr_bench._dump import dump_hash
from csr_bench.clock import phoenix_instant
from csr_bench.config import load_run_config, probability_threshold
from csr_bench.db import (
    begin,
    commit,
    connect,
    init_schema,
    insert_run,
    set_current_time,
    set_run_status,
)
from csr_bench.enums import (
    EntityType,
    EventKind,
    InvocationType,
    KeyedStreamName,
    LedgerCategory,
    RunStatus,
    SequentialStreamName,
)
from csr_bench.events import Events
from csr_bench.invocations import Invocations
from csr_bench.ledger import Ledger, LedgerEntry
from csr_bench.rng import Rng, chance
from csr_bench.txn import Services, TxnContext, command, privileged, privileged_request_id

# ---------------------------------------------------------------------------
# Fixed inputs — the script is a pure function of these
# ---------------------------------------------------------------------------

MASTER_SEED = 20261001
#: A fixed run id: the driver's databases are compared across processes, and a
#: UUID would differ per process. (:mod:`csr_bench._dump` also normalizes it, so
#: the proofs hold either way.)
RUN_ID = "contract-test-run"
TIMEZONE = "America/Phoenix"
FIRST_SERVICE_DATE = "2026-10-01"
LAST_SERVICE_DATE = "2027-09-30"

#: The single simulated service day the script plays.
DAY = (2026, 10, 1)
#: Scope string for every keyed draw in the script (the Phoenix service date).
SCOPE = FIRST_SERVICE_DATE

ENTITY = EntityType.CONTRACT_TEST_ENTITY.value
ENTITY_ALT = EntityType.CONTRACT_TEST_ENTITY_ALT.value

OPENING_CASH_CENTS = 5_000_000
BOOK_PRICE_CENTS = 24_500
TRAVEL_COST_CENTS = 3_000
RESCHEDULE_FEE_CENTS = 1_750
SETTLE_DEBIT_CENTS = 12_000
SETTLE_CREDIT_CENTS = 41_000
PAYROLL_BASE_CENTS = 180_000
DAILY_REVENUE_CENTS = 196_500
#: Far beyond any balance the script reaches — the variant-B overspend probe.
OVERSPEND_CENTS = 10_000_000_000

BOOK_ENTITY_ID = 11
TRAVEL_ENTITY_ID = 12
RESCHEDULE_ENTITY_ID = 13

HALF_THRESHOLD = probability_threshold(Decimal("0.5"))
CASCADE_THRESHOLD = probability_threshold(Decimal("0.7"))

MINUTE_MS = 60_000


def _ts(hour: int, minute: int) -> int:
    return phoenix_instant(DAY[0], DAY[1], DAY[2], hour, minute)


T_OPEN = _ts(6, 45)
T_BOOKED_JOB = _ts(14, 0)
T_CALL = _ts(12, 0)
T_RESCHEDULED = _ts(15, 30)
T_SETTLE = _ts(19, 0)

#: Seeded world events, deliberately inserted out of chronological order and with
#: a ``(8:00, priority 20)`` tie so the pop order exercises all three ordering
#: keys: ``(scheduled_at, priority, event_id)``.
SEED_EVENTS: tuple[tuple[EventKind, int, int, int, int], ...] = (
    (EventKind.CONTRACT_TEST_EVENT, 11, 0, 10, 3),
    (EventKind.CONTRACT_TEST_EVENT, 8, 0, 20, 1),
    (EventKind.CONTRACT_TEST_SETTLE_DEBIT, 8, 0, 10, 2),
    (EventKind.CONTRACT_TEST_EVENT, 8, 0, 20, 4),
    (EventKind.CONTRACT_TEST_SETTLE_CREDIT, 9, 30, 5, 5),
)


class DriverError(RuntimeError):
    """The script could not proceed — a driver bug, never an expected outcome."""


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------


@dataclass
class RunContext:
    path: str
    conn: sqlite3.Connection
    run_id: str
    rng: Rng
    ledger: Ledger
    events: Events
    invocations: Invocations
    services: Services
    variant: str = "A"
    #: Draw outcomes, in consumption order. Must be identical between variants.
    observations: list[tuple[str, str]] = field(default_factory=list)
    #: Side channel for the variant-B probes (stored responses, retries,
    #: rejection codes) that the test inspects but that carry no draw.
    notes: list[tuple[str, str]] = field(default_factory=list)
    steps_run: list[str] = field(default_factory=list)
    #: Committed public request ids, for the idempotent-retry probe.
    public_request_ids: list[str] = field(default_factory=list)

    def __enter__(self) -> RunContext:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self.conn.in_transaction:
            self.conn.rollback()
        self.conn.close()


def _observe(run: RunContext, label: str, value: object) -> None:
    run.observations.append((label, str(value)))


def _audit_recorder(conn: sqlite3.Connection, run_id: str) -> Callable[..., None]:
    """Persist each consumed draw key with the run's *simulated* timestamp."""

    def rec(**kw: object) -> None:
        row = conn.execute("SELECT current_time_ms FROM run WHERE run_id=?", (run_id,)).fetchone()
        conn.execute(
            "INSERT INTO _rng_draw_audit(run_id, stream_name, entity_type, entity_id, "
            "scope, event_kind, draw_index, outcome_id, simulated_ts_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                kw["stream_name"],
                kw["entity_type"],
                kw["entity_id"],
                kw["scope"],
                kw["event_kind"],
                kw["draw_index"],
                kw.get("outcome_id"),
                int(row["current_time_ms"]),
            ),
        )

    return rec


def create_run(path: str | Path, *, master_seed: int = MASTER_SEED) -> None:
    """Initialize a fresh run database and seed the world (one committed txn each)."""
    conn = connect(path)
    try:
        init_schema(conn)
        cfg = load_run_config()
        begin(conn)
        insert_run(
            conn,
            run_id=RUN_ID,
            spec_version=SPEC_VERSION,
            config_version=cfg.config_version,
            config_hash=cfg.config_hash,
            config_json=cfg.canonical_bytes.decode("utf-8"),
            master_seed=master_seed,
            start_at_ms=T_OPEN,
            first_service_date=FIRST_SERVICE_DATE,
            last_service_date=LAST_SERVICE_DATE,
            timezone=TIMEZONE,
            current_time_ms=T_OPEN,
            status=RunStatus.INITIALIZING,
        )
        set_run_status(conn, RUN_ID, RunStatus.ACTIVE)
        commit(conn)
    finally:
        conn.close()
    with open_run(path, master_seed=master_seed) as run:
        _seed_world(run)


def open_run(path: str | Path, *, master_seed: int = MASTER_SEED, variant: str = "A") -> RunContext:
    """Wire the real services over an existing run database.

    Nothing in-memory is carried over from a previous process: sequential stream
    counters are adopted from committed ``_rng_state`` rows by :class:`Rng`.
    """
    conn = connect(path)
    rng = Rng(
        master_seed,
        SPEC_VERSION,
        audit_recorder=_audit_recorder(conn, RUN_ID),
        conn=conn,
        run_id=RUN_ID,
    )
    ledger = Ledger(conn, RUN_ID)
    events = Events(conn, RUN_ID, rng)
    invocations = Invocations(conn, RUN_ID)
    services = Services(
        conn=conn, run_id=RUN_ID, rng=rng, ledger=ledger, invocations=invocations, events=events
    )
    return RunContext(
        path=str(path),
        conn=conn,
        run_id=RUN_ID,
        rng=rng,
        ledger=ledger,
        events=events,
        invocations=invocations,
        services=services,
        variant=variant,
    )


# ---------------------------------------------------------------------------
# Shared transaction helpers
# ---------------------------------------------------------------------------


def _require_ok(what: str, response: bytes) -> dict:
    body = json.loads(response)
    if not body.get("ok"):
        raise DriverError(f"{what}: {body}")
    return body


def _advance_to(ctx: TxnContext, ts_ms: int) -> int:
    """Move simulated time to ``ts_ms``, never backwards (spec 01 invariant #3)."""
    now = max(int(ctx.now_ms), int(ts_ms))
    set_current_time(ctx.conn, ctx.run_id, now)
    ctx.now_ms = now
    return now


def _payload(event: sqlite3.Row) -> dict:
    raw = event["payload"]
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# Toy event handlers
# ---------------------------------------------------------------------------


def _handle_tick(run: RunContext, ctx: TxnContext, event: sqlite3.Row) -> dict:
    """A world tick: advance the weather process, price the tick, maybe cascade."""
    entity_id = int(event["entity_id"])
    kind = str(event["kind"])
    now = _advance_to(ctx, int(event["scheduled_at_ms"]))

    # Sequential: the weather process evolves with world time, never with how
    # many actions the agent took (spec 09 DET-05).
    u = ctx.rng.stream(SequentialStreamName.WEATHER_PROCESS.value).next()
    temp_index = u % 100
    _observe(run, f"weather_process/tick/{event['event_id']}", u)

    # Keyed: order-independent forecast noise for this entity.
    f = ctx.rng.draw(
        KeyedStreamName.WEATHER_FORECAST.value,
        ENTITY,
        entity_id,
        SCOPE,
        kind,
        0,
        outcome_id=f"temp_index={temp_index}",
    )
    _observe(run, f"weather_forecast/{entity_id}", f)

    ctx.services.ledger.post(
        [
            LedgerEntry(
                -10 * (temp_index + 1),
                LedgerCategory.CONTRACT_TEST_DEBIT,
                now,
                entity_type=ENTITY,
                entity_id=entity_id,
            )
        ]
    )

    depth = int(_payload(event).get("depth", 0))
    cascaded = None
    if depth < 1 and chance(f, CASCADE_THRESHOLD):
        cascaded = ctx.services.events.schedule(
            EventKind.CONTRACT_TEST_EVENT,
            int(event["scheduled_at_ms"]) + 30 * MINUTE_MS,
            15,
            entity_type=ENTITY,
            entity_id=entity_id,
            payload={"depth": depth + 1, "source": "cascade"},
        )
    return {"temp_index": temp_index, "cascaded_event_id": cascaded}


def _handle_settlement(
    amount_cents: int, category: LedgerCategory
) -> Callable[[RunContext, TxnContext, sqlite3.Row], dict]:
    """An unavoidable settlement effect: a fixed amount, no draw."""

    def handler(run: RunContext, ctx: TxnContext, event: sqlite3.Row) -> dict:
        now = _advance_to(ctx, int(event["scheduled_at_ms"]))
        ctx.services.ledger.post(
            [
                LedgerEntry(
                    amount_cents,
                    category,
                    now,
                    entity_type=ENTITY,
                    entity_id=int(event["entity_id"]),
                )
            ]
        )
        return {"amount_cents": amount_cents}

    return handler


HANDLERS: dict[EventKind, Callable[[RunContext, TxnContext, sqlite3.Row], dict]] = {
    EventKind.CONTRACT_TEST_EVENT: _handle_tick,
    EventKind.CONTRACT_TEST_SETTLE_DEBIT: _handle_settlement(
        -SETTLE_DEBIT_CENTS, LedgerCategory.CONTRACT_TEST_DEBIT
    ),
    EventKind.CONTRACT_TEST_SETTLE_CREDIT: _handle_settlement(
        SETTLE_CREDIT_CENTS, LedgerCategory.CONTRACT_TEST_CREDIT
    ),
}


# ---------------------------------------------------------------------------
# Fake agent commands (recorded script)
# ---------------------------------------------------------------------------


def _cmd_book(run: RunContext, ctx: TxnContext) -> dict:
    # Discretionary immediate cost: cash-guarded before any write or draw
    # (docs/CSR-001.md decision 4).
    ctx.services.ledger.require_cash(BOOK_PRICE_CENTS)
    u = ctx.rng.draw(
        KeyedStreamName.ACCEPTANCE.value,
        ENTITY,
        BOOK_ENTITY_ID,
        SCOPE,
        EventKind.CONTRACT_TEST_EVENT.value,
        0,
        outcome_id="book",
    )
    _observe(run, f"acceptance/{BOOK_ENTITY_ID}", u)
    accepted = chance(u, HALF_THRESHOLD)
    ctx.services.ledger.post(
        [
            LedgerEntry(
                -BOOK_PRICE_CENTS,
                LedgerCategory.CONTRACT_TEST_DEBIT,
                ctx.now_ms,
                entity_type=ENTITY,
                entity_id=BOOK_ENTITY_ID,
            )
        ]
    )
    ctx.services.events.schedule(
        EventKind.CONTRACT_TEST_EVENT,
        T_BOOKED_JOB,
        15,
        entity_type=ENTITY,
        entity_id=BOOK_ENTITY_ID,
        payload={"depth": 0, "source": "book"},
    )
    return {"accepted": accepted, "entity_id": BOOK_ENTITY_ID}


def _cmd_travel(run: RunContext, ctx: TxnContext) -> dict:
    minutes = ctx.rng.triangular(
        KeyedStreamName.TRAVEL_NOISE.value,
        ENTITY,
        TRAVEL_ENTITY_ID,
        SCOPE,
        EventKind.CONTRACT_TEST_EVENT.value,
    )
    _observe(run, f"travel_noise/{TRAVEL_ENTITY_ID}", minutes)
    ctx.services.ledger.post(
        [
            LedgerEntry(
                -TRAVEL_COST_CENTS,
                LedgerCategory.CONTRACT_TEST_DEBIT,
                ctx.now_ms,
                entity_type=ENTITY,
                entity_id=TRAVEL_ENTITY_ID,
            )
        ]
    )
    return {"timing_variation_minutes": minutes}


def _cmd_reschedule(run: RunContext, ctx: TxnContext) -> dict:
    ctx.services.ledger.require_cash(RESCHEDULE_FEE_CENTS)
    slot = ctx.rng.choice(
        KeyedStreamName.DEMAND.value,
        ENTITY_ALT,
        RESCHEDULE_ENTITY_ID,
        SCOPE,
        EventKind.CONTRACT_TEST_EVENT.value,
        5,
        draw_index=2,
    )
    _observe(run, f"demand_slot/{RESCHEDULE_ENTITY_ID}", slot)
    ctx.services.ledger.post(
        [
            LedgerEntry(
                -RESCHEDULE_FEE_CENTS,
                LedgerCategory.CONTRACT_TEST_DEBIT,
                ctx.now_ms,
                entity_type=ENTITY_ALT,
                entity_id=RESCHEDULE_ENTITY_ID,
            )
        ]
    )
    ctx.services.events.schedule(
        EventKind.CONTRACT_TEST_EVENT,
        T_RESCHEDULED + slot * 15 * MINUTE_MS,
        15,
        entity_type=ENTITY_ALT,
        entity_id=RESCHEDULE_ENTITY_ID,
        payload={"depth": 1, "source": "reschedule"},
    )
    return {"slot": slot, "entity_id": RESCHEDULE_ENTITY_ID}


def _cmd_overspend(run: RunContext, ctx: TxnContext) -> dict:
    """The variant-B failed validation: rejected before any write or draw."""
    ctx.services.ledger.require_cash(OVERSPEND_CENTS)
    raise DriverError("overspend probe unexpectedly passed the cash guard")


def _replay_trap(ctx: TxnContext) -> dict:
    """Body for an idempotent retry: reaching it means the response was re-run."""
    raise DriverError("idempotent retry re-executed the command body")


# ---------------------------------------------------------------------------
# Script steps — one committed transaction each
# ---------------------------------------------------------------------------


def _run_command(run: RunContext, name: str, fn: Callable[[TxnContext], dict]) -> None:
    request_id = f"script:{name}"
    response = command(run.services, request_id, fn)
    _require_ok(name, response)
    run.public_request_ids.append(request_id)
    run.notes.append((f"response:{request_id}", response.hex()))


def _step_open_opening(run: RunContext, name: str) -> None:
    result = run.invocations.open(
        InvocationType.OPENING, T_OPEN, prompt_version="contract-test", services=run.services
    )
    if not isinstance(result, int):
        raise DriverError(f"{name}: {json.loads(result)}")


def _step_open_call(run: RunContext, name: str) -> None:
    result = run.invocations.open(
        InvocationType.CALL, T_CALL, prompt_version="contract-test", services=run.services
    )
    if not isinstance(result, int):
        raise DriverError(f"{name}: {json.loads(result)}")


def _step_complete_invocation(run: RunContext, name: str) -> None:
    active = run.invocations.active()
    if active is None:
        raise DriverError(f"{name}: no active invocation to complete")
    _require_ok(name, run.invocations.complete(int(active["invocation_id"]), services=run.services))


def _step_apply_next_event(run: RunContext, name: str) -> None:
    event = run.events.next_pending()
    if event is None:
        raise DriverError(f"{name}: no pending event — the script would be vacuous")
    handler = HANDLERS[EventKind(event["kind"])]
    _require_ok(
        name,
        run.events.apply(int(event["event_id"]), partial(handler, run), services=run.services),
    )


def _step_settle_day(run: RunContext, name: str) -> None:
    def fn(ctx: TxnContext) -> dict:
        now = _advance_to(ctx, T_SETTLE)
        wage = ctx.rng.stream(SequentialStreamName.LABOR_ECONOMY.value).next()
        weather = ctx.rng.stream(SequentialStreamName.WEATHER_PROCESS.value).next()
        _observe(run, "labor_economy/settle", wage)
        _observe(run, "weather_process/settle", weather)
        drift = wage % 500
        ctx.services.ledger.post(
            [
                LedgerEntry(-(PAYROLL_BASE_CENTS + drift), LedgerCategory.CONTRACT_TEST_DEBIT, now),
                LedgerEntry(DAILY_REVENUE_CENTS, LedgerCategory.CONTRACT_TEST_CREDIT, now),
            ]
        )
        return {"payroll_drift_cents": drift}

    _require_ok(name, privileged(run.services, privileged_request_id("settle", name), fn))


def _step_cmd_book(run: RunContext, name: str) -> None:
    _run_command(run, name, partial(_cmd_book, run))


def _step_cmd_travel(run: RunContext, name: str) -> None:
    _run_command(run, name, partial(_cmd_travel, run))


def _step_cmd_reschedule(run: RunContext, name: str) -> None:
    _run_command(run, name, partial(_cmd_reschedule, run))


#: The recorded script. Index (1-based) is the commit point number; every entry
#: commits exactly one transaction, so boundary ``i`` means "steps 1..i applied".
SCRIPT: tuple[tuple[str, Callable[[RunContext, str], None]], ...] = (
    ("01_open_opening_invocation", _step_open_opening),
    ("02_cmd_book", _step_cmd_book),
    ("03_cmd_travel", _step_cmd_travel),
    ("04_complete_opening_invocation", _step_complete_invocation),
    ("05_apply_event", _step_apply_next_event),
    ("06_apply_event", _step_apply_next_event),
    ("07_open_call_invocation", _step_open_call),
    ("08_cmd_reschedule", _step_cmd_reschedule),
    ("09_complete_call_invocation", _step_complete_invocation),
    ("10_apply_event", _step_apply_next_event),
    ("11_apply_event", _step_apply_next_event),
    ("12_settle_day", _step_settle_day),
)

N_COMMITS = len(SCRIPT)


def _seed_world(run: RunContext) -> None:
    def fn(ctx: TxnContext) -> dict:
        ctx.services.ledger.post(
            [LedgerEntry(OPENING_CASH_CENTS, LedgerCategory.OPENING_BALANCE_ADJUSTMENT, T_OPEN)]
        )
        for kind, hour, minute, priority, entity_id in SEED_EVENTS:
            ctx.services.events.schedule(
                kind,
                _ts(hour, minute),
                priority,
                entity_type=ENTITY,
                entity_id=entity_id,
                payload={"depth": 0, "source": "seed"},
            )
        return {"seeded_events": len(SEED_EVENTS)}

    _require_ok("seed_world", privileged(run.services, privileged_request_id("seed_world", 1), fn))


# ---------------------------------------------------------------------------
# Variant B — noise that must not move a single draw (DET-04 / DET-05)
# ---------------------------------------------------------------------------


def _interleave(run: RunContext, after: str) -> None:
    if run.variant != "B":
        return
    # (a) extra read-only queries — no write, no draw.
    run.conn.execute("SELECT COUNT(*) FROM _events WHERE run_id=?", (run.run_id,)).fetchone()
    run.conn.execute(
        "SELECT COUNT(*) FROM _rng_draw_audit WHERE run_id=?", (run.run_id,)
    ).fetchone()
    run.ledger.cash_balance()
    run.events.pending_in_order()

    # (b) idempotent retries of already-committed request ids — the stored
    # response comes back without the body running (``_replay_trap``).
    for request_id in run.public_request_ids:
        run.notes.append(
            (f"retry:{request_id}", command(run.services, request_id, _replay_trap).hex())
        )

    # (c) a failed validation: a discretionary overspend, refused before any
    # write or draw. Only reachable while an invocation is active.
    if run.invocations.active() is not None:
        request_id = f"probe:overspend:{after}"
        body = json.loads(command(run.services, request_id, partial(_cmd_overspend, run)))
        run.notes.append((f"rejected:{request_id}", body.get("error", {}).get("code", "")))


def run_steps(run: RunContext, first: int = 1, last: int = N_COMMITS) -> None:
    """Play commit points ``first..last`` (1-based, inclusive) of :data:`SCRIPT`."""
    for index, (name, step) in enumerate(SCRIPT, start=1):
        if not (first <= index <= last):
            continue
        step(run, name)
        run.steps_run.append(name)
        _interleave(run, name)


def report(run: RunContext) -> dict:
    return {
        "hash": dump_hash(run.conn, run.run_id),
        "observations": [list(o) for o in run.observations],
        "notes": [list(n) for n in run.notes],
        "steps": list(run.steps_run),
    }


# ---------------------------------------------------------------------------
# Subprocess entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CSR-001 scripted determinism driver")
    parser.add_argument("--db", required=True)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--first", type=int, default=1)
    parser.add_argument("--last", type=int, default=N_COMMITS)
    parser.add_argument("--variant", choices=("A", "B"), default="A")
    parser.add_argument("--seed", type=int, default=MASTER_SEED)
    args = parser.parse_args(argv)

    if args.create:
        create_run(args.db, master_seed=args.seed)
    with open_run(args.db, master_seed=args.seed, variant=args.variant) as run:
        run_steps(run, args.first, args.last)
        payload = report(run)
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
