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

from csr_bench import SPEC_VERSION, clock
from csr_bench._dump import dump_hash
from csr_bench.clock import phoenix_instant
from csr_bench.config import load_run_config, probability_threshold
from csr_bench.db import (
    begin,
    commit,
    connect,
    insert_run,
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
from csr_bench.migrations import migrate
from csr_bench.rng import Rng, chance, sqlite_audit_recorder
from csr_bench.txn import Services, TxnContext, command, privileged, privileged_request_id

MASTER_SEED = 20261001
RUN_ID = "contract-test-run"
TIMEZONE = "America/Phoenix"
FIRST_SERVICE_DATE = "2026-10-01"
LAST_SERVICE_DATE = "2027-09-30"

DAY = (2026, 10, 1)
SCOPE = FIRST_SERVICE_DATE

ENTITY = EntityType.APPOINTMENT.value
ENTITY_ALT = EntityType.TECHNICIAN.value

OPENING_CASH_CENTS = 5_000_000
BOOK_PRICE_CENTS = 24_500
TRAVEL_COST_CENTS = 3_000
RESCHEDULE_FEE_CENTS = 1_750
SETTLE_DEBIT_CENTS = 12_000
SETTLE_CREDIT_CENTS = 41_000
PAYROLL_BASE_CENTS = 180_000
DAILY_REVENUE_CENTS = 196_500
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

SEED_EVENTS: tuple[tuple[EventKind, int, int, int], ...] = (
    (EventKind.DEMAND_GENERATION, 11, 0, 3),
    (EventKind.DEMAND_GENERATION, 8, 0, 1),
    (EventKind.DAILY_SETTLEMENT, 8, 0, 2),
    (EventKind.DEMAND_GENERATION, 8, 0, 4),
    (EventKind.WORK_COMPLETED, 9, 30, 5),
)


class DriverError(RuntimeError):
    pass


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
    observations: list[tuple[str, str]] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)
    steps_run: list[str] = field(default_factory=list)
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


def create_run(path: str | Path, *, master_seed: int = MASTER_SEED) -> None:
    conn = connect(path)
    try:
        migrate(conn)
        cfg = load_run_config()
        begin(conn)
        insert_run(
            conn,
            run_id=RUN_ID,
            spec_version=SPEC_VERSION,
            config_version=cfg.config_version,
            config_hash=cfg.config_hash,
            master_seed=master_seed,
            start_at_ms=T_OPEN,
            first_service_date=FIRST_SERVICE_DATE,
            last_service_date=LAST_SERVICE_DATE,
            timezone=TIMEZONE,
            status=RunStatus.INITIALIZING,
        )
        set_run_status(conn, RUN_ID, RunStatus.ACTIVE)
        commit(conn)
    finally:
        conn.close()
    with open_run(path, master_seed=master_seed) as run:
        _seed_world(run)


def open_run(path: str | Path, *, master_seed: int = MASTER_SEED, variant: str = "A") -> RunContext:
    conn = connect(path)
    rng = Rng(
        master_seed,
        SPEC_VERSION,
        audit_recorder=sqlite_audit_recorder(conn),
        conn=conn,
    )
    ledger = Ledger(conn)
    events = Events(conn, rng)
    invocations = Invocations(conn)
    services = Services(conn=conn, rng=rng, ledger=ledger, invocations=invocations, events=events)
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


def _require_ok(what: str, response: bytes) -> dict:
    body = json.loads(response)
    if not body.get("ok"):
        raise DriverError(f"{what}: {body}")
    return body


def _advance_to(ctx: TxnContext, ts_ms: int) -> int:
    now = max(int(ctx.now_ms), int(ts_ms))
    clock.advance(ctx.conn, now)
    ctx.now_ms = now
    return now


def _payload(event: sqlite3.Row) -> dict:
    raw = event["payload"]
    return json.loads(raw) if raw else {}


def _handle_tick(run: RunContext, ctx: TxnContext, event: sqlite3.Row) -> dict:
    entity_id = int(event["entity_id"])
    kind = str(event["kind"])
    now = _advance_to(ctx, int(event["scheduled_at_ms"]))

    u = ctx.rng.stream(SequentialStreamName.WEATHER_PROCESS.value).next()
    temp_index = u % 100
    _observe(run, f"weather_process/tick/{event['event_id']}", u)

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
                LedgerCategory.OPERATING_OVERHEAD,
                -10 * (temp_index + 1),
                now,
            )
        ]
    )

    depth = int(_payload(event).get("depth", 0))
    cascaded = None
    if depth < 1 and chance(f, CASCADE_THRESHOLD):
        cascaded = ctx.services.events.schedule(
            EventKind(kind),
            int(event["scheduled_at_ms"]) + 30 * MINUTE_MS,
            entity_type=ENTITY,
            entity_id=entity_id,
            payload={"depth": depth + 1, "source": "cascade"},
        )
    return {"temp_index": temp_index, "cascaded_event_id": cascaded}


def _handle_settlement(
    amount_cents: int, category: LedgerCategory
) -> Callable[[RunContext, TxnContext, sqlite3.Row], dict]:

    def handler(run: RunContext, ctx: TxnContext, event: sqlite3.Row) -> dict:
        now = _advance_to(ctx, int(event["scheduled_at_ms"]))
        ctx.services.ledger.post(
            [
                LedgerEntry(
                    category,
                    amount_cents,
                    now,
                )
            ]
        )
        return {"amount_cents": amount_cents}

    return handler


HANDLERS: dict[EventKind, Callable[[RunContext, TxnContext, sqlite3.Row], dict]] = {
    EventKind.DEMAND_GENERATION: _handle_tick,
    EventKind.CALL_INVOCATION: _handle_tick,
    EventKind.DAILY_SETTLEMENT: _handle_settlement(
        -SETTLE_DEBIT_CENTS, LedgerCategory.OPERATING_OVERHEAD
    ),
    EventKind.WORK_COMPLETED: _handle_settlement(
        SETTLE_CREDIT_CENTS, LedgerCategory.REVENUE
    ),
}


def _cmd_book(run: RunContext, ctx: TxnContext) -> dict:
    ctx.services.ledger.require_cash(BOOK_PRICE_CENTS)
    u = ctx.rng.draw(
        KeyedStreamName.ACCEPTANCE.value,
        ENTITY,
        BOOK_ENTITY_ID,
        SCOPE,
        EventKind.CALL_INVOCATION.value,
        0,
        outcome_id="book",
    )
    _observe(run, f"acceptance/{BOOK_ENTITY_ID}", u)
    accepted = chance(u, HALF_THRESHOLD)
    ctx.services.ledger.post(
        [
            LedgerEntry(
                LedgerCategory.MATERIAL_COST,
                -BOOK_PRICE_CENTS,
                ctx.now_ms,
            )
        ]
    )
    ctx.services.events.schedule(
        EventKind.APPOINTMENT_DISPATCH,
        T_BOOKED_JOB,
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
        EventKind.APPOINTMENT_DISPATCH.value,
    )
    _observe(run, f"travel_noise/{TRAVEL_ENTITY_ID}", minutes)
    ctx.services.ledger.post(
        [
            LedgerEntry(
                LedgerCategory.MATERIAL_COST,
                -TRAVEL_COST_CENTS,
                ctx.now_ms,
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
        EventKind.CALL_INVOCATION.value,
        5,
        draw_index=2,
    )
    _observe(run, f"demand_slot/{RESCHEDULE_ENTITY_ID}", slot)
    ctx.services.ledger.post(
        [
            LedgerEntry(
                LedgerCategory.MATERIAL_COST,
                -RESCHEDULE_FEE_CENTS,
                ctx.now_ms,
            )
        ]
    )
    ctx.services.events.schedule(
        EventKind.APPOINTMENT_DISPATCH,
        T_RESCHEDULED + slot * 15 * MINUTE_MS,
        entity_type=ENTITY_ALT,
        entity_id=RESCHEDULE_ENTITY_ID,
        payload={"depth": 1, "source": "reschedule"},
    )
    return {"slot": slot, "entity_id": RESCHEDULE_ENTITY_ID}


def _cmd_overspend(run: RunContext, ctx: TxnContext) -> dict:
    ctx.services.ledger.require_cash(OVERSPEND_CENTS)
    raise DriverError("overspend probe unexpectedly passed the cash guard")


def _replay_trap(ctx: TxnContext) -> dict:
    raise DriverError("idempotent retry re-executed the command body")


def _run_command(run: RunContext, name: str, fn: Callable[[TxnContext], dict]) -> None:
    request_id = f"script:{name}"
    response = command(run.services, request_id, fn, operation=name, args={})
    _require_ok(name, response)
    run.public_request_ids.append(request_id)
    run.notes.append((f"response:{request_id}", response.hex()))


def _step_open_opening(run: RunContext, name: str) -> None:
    result = run.invocations.open(
        InvocationType.OPENING,
        T_OPEN,
        request_id=f"script:{name}",
        prompt_version="contract-test",
        services=run.services,
    )
    if not isinstance(result, int):
        raise DriverError(f"{name}: {json.loads(result)}")


def _step_open_call(run: RunContext, name: str) -> None:
    result = run.invocations.open(
        InvocationType.CALL,
        T_CALL,
        request_id=f"script:{name}",
        prompt_version="contract-test",
        services=run.services,
    )
    if not isinstance(result, int):
        raise DriverError(f"{name}: {json.loads(result)}")


def _step_complete_invocation(run: RunContext, name: str) -> None:
    active = run.invocations.active()
    if active is None:
        raise DriverError(f"{name}: no active invocation to complete")
    _require_ok(
        name,
        run.invocations.complete(
            int(active["invocation_id"]), request_id=f"script:{name}", services=run.services
        ),
    )


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
                LedgerEntry(LedgerCategory.PAYROLL_BASE, -(PAYROLL_BASE_CENTS + drift), now),
                LedgerEntry(LedgerCategory.REVENUE, DAILY_REVENUE_CENTS, now),
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
            [
                LedgerEntry(
                    LedgerCategory.OPENING_BALANCE_ADJUSTMENT, OPENING_CASH_CENTS, T_OPEN
                )
            ]
        )
        for kind, hour, minute, entity_id in SEED_EVENTS:
            ctx.services.events.schedule(
                kind,
                _ts(hour, minute),
                entity_type=ENTITY,
                entity_id=entity_id,
                payload={"depth": 0, "source": "seed"},
            )
        return {"seeded_events": len(SEED_EVENTS)}

    _require_ok("seed_world", privileged(run.services, privileged_request_id("seed_world", 1), fn))


def _interleave(run: RunContext, after: str) -> None:
    if run.variant != "B":
        return
    run.conn.execute("SELECT COUNT(*) FROM _events").fetchone()
    run.conn.execute("SELECT COUNT(*) FROM _random_audit").fetchone()
    run.ledger.cash_balance()
    run.events.pending_in_order()

    for request_id in run.public_request_ids:
        op = request_id.removeprefix("script:")
        run.notes.append(
            (
                f"retry:{request_id}",
                command(run.services, request_id, _replay_trap, operation=op, args={}).hex(),
            )
        )

    if run.invocations.active() is not None:
        request_id = f"probe:overspend:{after}"
        body = json.loads(
            command(
                run.services, request_id, partial(_cmd_overspend, run),
                operation="probe.overspend", args={},
            )
        )
        run.notes.append((f"rejected:{request_id}", body.get("error", {}).get("code", "")))


def run_steps(run: RunContext, first: int = 1, last: int = N_COMMITS) -> None:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="scripted determinism driver")
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
