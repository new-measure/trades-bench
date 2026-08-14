from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from . import SPEC_VERSION, clock, constants, db, demand
from .adapter import AdapterPrompt, HarnessAdapter
from .config import RunConfig
from .enums import (
    EVENT_PRIORITY,
    DayType,
    EventKind,
    InvocationType,
    LedgerCategory,
    PromptKind,
    RunStatus,
)
from .errors import EngineError, ErrorCode
from .events import EventHandler, Events
from .invocations import Invocations
from .ledger import Ledger, LedgerEntry
from .rng import Rng, sqlite_audit_recorder
from .txn import Services, TxnContext, privileged, privileged_request_id

_INVOCATION_EVENT_KINDS = (
    EventKind.OPENING_INVOCATION,
    EventKind.CALL_INVOCATION,
    EventKind.CLOSING_INVOCATION,
)

_INVOCATION_TYPE_BY_KIND = {
    EventKind.OPENING_INVOCATION: InvocationType.OPENING,
    EventKind.CALL_INVOCATION: InvocationType.CALL,
    EventKind.CLOSING_INVOCATION: InvocationType.CLOSING,
}

_PROMPT_KIND_BY_TYPE = {
    InvocationType.OPENING: PromptKind.OPENING,
    InvocationType.CALL: PromptKind.CALL,
    InvocationType.CLOSING: PromptKind.CLOSING,
}

_STUB_EVENT_KINDS = (
    EventKind.EMPLOYMENT_TRANSITION,
    EventKind.LABOR_ECONOMY_UPDATE,
    EventKind.SICKNESS_DRAW,
    EventKind.ATTRITION_DRAW,
    EventKind.TRAINING_RATES_DRAWN,
    EventKind.SHIFT_MATERIALIZATION,
    EventKind.FIRST_TRIP_DISPATCH,
)

_PRE_OPENING_STUB_KINDS = (
    EventKind.EMPLOYMENT_TRANSITION,
    EventKind.WEATHER_REALIZED,
    EventKind.LABOR_ECONOMY_UPDATE,
    EventKind.SICKNESS_DRAW,
    EventKind.ATTRITION_DRAW,
    EventKind.TRAINING_RATES_DRAWN,
    EventKind.SHIFT_MATERIALIZATION,
)

BLOCK_CODES: tuple[str, ...] = ("B07_09", "B09_11", "B11_13", "B13_15", "B15_17", "B17_19")

_BLOCK_START_MINUTES = {
    code: start
    for code, (start, _end) in zip(BLOCK_CODES, constants.BLOCK_GRID_MINUTES, strict=True)
}

_PAYING_EMPLOYMENT_STATUSES = ("active", "pending_termination", "resigning_notice")


def _payload_of(row: sqlite3.Row) -> dict:
    return json.loads(row["payload"]) if row["payload"] else {}


def _canonical_payload(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _instant(service_date: str, minutes_from_midnight: int) -> int:
    d = date.fromisoformat(service_date)
    return clock.phoenix_instant(
        d.year, d.month, d.day, minutes_from_midnight // 60, minutes_from_midnight % 60
    )


def _event_service_date(row: sqlite3.Row) -> str:
    return clock.service_date_for_ms(int(row["scheduled_at_ms"]))


def _advance_to_event(ctx: TxnContext, row: sqlite3.Row) -> int:
    target = max(clock.now(ctx.conn), int(row["scheduled_at_ms"]))
    clock.advance(ctx.conn, target)
    return target


def _require_ok(response: bytes) -> dict:
    body = json.loads(response)
    if not body.get("ok"):
        error = body.get("error") or {}
        raise EngineError(
            ErrorCode(error.get("code", ErrorCode.UNEXPECTED_FAILURE.value)),
            str(error.get("message", "engine call failed")),
            details=error.get("details"),
        )
    return body.get("result") or {}


def _stub_handler(ctx: TxnContext, row: sqlite3.Row) -> dict | None:
    _advance_to_event(ctx, row)
    return None


def _day_rollover_handler(ctx: TxnContext, row: sqlite3.Row) -> dict | None:
    _advance_to_event(ctx, row)
    service_date = _event_service_date(row)
    d = date.fromisoformat(service_date)
    ctx.conn.execute(
        "INSERT INTO _day_state (service_date, day_type) VALUES (?, ?)",
        (service_date, DayType.from_iso_weekday(d.isoweekday()).value),
    )
    events = Events(ctx.conn, ctx.rng)
    batch_at = int(row["scheduled_at_ms"])
    for kind in _PRE_OPENING_STUB_KINDS:
        events.schedule(kind, batch_at)
    events.schedule(
        EventKind.DEMAND_GENERATION, batch_at, payload={"service_date": service_date}
    )
    events.schedule(EventKind.OPENING_INVOCATION, batch_at)
    events.schedule(
        EventKind.FIRST_BLOCK_LOCK, batch_at, payload={"block_code": BLOCK_CODES[0]}
    )
    events.schedule(EventKind.FIRST_TRIP_DISPATCH, batch_at)
    for code in BLOCK_CODES[1:]:
        events.schedule(
            EventKind.BLOCK_LOCK,
            _instant(service_date, _BLOCK_START_MINUTES[code]),
            payload={"block_code": code},
        )
    return None


def _weather_realized_handler(ctx: TxnContext, row: sqlite3.Row) -> dict | None:
    _advance_to_event(ctx, row)
    service_date = _event_service_date(row)
    missing = int(
        ctx.conn.execute(
            "SELECT COUNT(*) c FROM _zones z WHERE NOT EXISTS ("
            "SELECT 1 FROM _zone_weather_daily w "
            "WHERE w.zone_id = z.id AND w.local_date = ?)",
            (service_date,),
        ).fetchone()["c"]
    )
    if missing:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"{missing} zone(s) lack a precomputed weather row for {service_date}",
        )
    return None


def _demand_generation_handler(config: RunConfig) -> EventHandler:
    inner = demand.demand_generation_handler(config)

    def handler(ctx: TxnContext, row: sqlite3.Row) -> dict | None:
        _advance_to_event(ctx, row)
        return inner(ctx, row)

    return handler


def _opening_invocation_handler(ctx: TxnContext, row: sqlite3.Row) -> dict | None:
    now = _advance_to_event(ctx, row)
    service_date = _event_service_date(row)
    cur = ctx.conn.execute(
        "UPDATE _day_state SET opening_at = ? WHERE service_date = ? AND opening_at IS NULL",
        (now, service_date),
    )
    if cur.rowcount != 1:
        raise EngineError(
            ErrorCode.STATE_ILLEGAL,
            f"day {service_date} has no rolled-over day state awaiting an opening",
        )
    return None


def _block_lock_handler(ctx: TxnContext, row: sqlite3.Row) -> dict | None:
    now = _advance_to_event(ctx, row)
    service_date = _event_service_date(row)
    code = _payload_of(row).get("block_code")
    if code not in _BLOCK_START_MINUTES:
        raise EngineError(
            ErrorCode.STATE_ILLEGAL, f"block lock event carries unknown block_code {code!r}"
        )
    ctx.conn.execute(
        "INSERT INTO _block_locks (service_date, block_code, block_start, locked_at) "
        "VALUES (?, ?, ?, ?)",
        (service_date, code, _instant(service_date, _BLOCK_START_MINUTES[code]), now),
    )
    return None


def _call_invocation_handler(ctx: TxnContext, row: sqlite3.Row) -> dict | None:
    _advance_to_event(ctx, row)
    pending = ctx.conn.execute(
        "SELECT 1 FROM _pending_calls WHERE id = ?", (row["entity_id"],)
    ).fetchone()
    if pending is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND,
            f"call event {row['event_id']} references missing pending call {row['entity_id']}",
        )
    return None


def _closing_invocation_handler(ctx: TxnContext, row: sqlite3.Row) -> dict | None:
    now = _advance_to_event(ctx, row)
    service_date = str(_payload_of(row)["service_date"])
    cur = ctx.conn.execute(
        "UPDATE _day_state SET closing_at = ? WHERE service_date = ? AND closing_at IS NULL",
        (now, service_date),
    )
    if cur.rowcount != 1:
        raise EngineError(
            ErrorCode.STATE_ILLEGAL,
            f"day {service_date} has no open day state awaiting a closing",
        )
    events = Events(ctx.conn, ctx.rng)
    events.schedule(EventKind.DAILY_SETTLEMENT, now, payload={"service_date": service_date})
    events.schedule(EventKind.CHECKPOINT, now, payload={"service_date": service_date})
    return None


def _off_hours_bonus_cents(config: RunConfig) -> int:
    raise EngineError(
        ErrorCode.INVARIANT_VIOLATED,
        "off-hours bonus has no configured amount yet, and no accrual mark can exist "
        "to require one",
    )


def _off_hours_entries(
    ctx: TxnContext, config: RunConfig, service_date: str, occurred_at_ms: int
) -> list[LedgerEntry]:
    day_start = _instant(service_date, 0)
    next_day = (date.fromisoformat(service_date) + timedelta(days=1)).isoformat()
    day_end = _instant(next_day, 0)
    marks = ctx.conn.execute(
        "SELECT * FROM _events WHERE kind = ? AND status = 'applied' "
        "AND scheduled_at_ms >= ? AND scheduled_at_ms < ? "
        "ORDER BY scheduled_at_ms, priority, event_id",
        (EventKind.OFF_HOURS_ACCRUAL_MARK.value, day_start, day_end),
    ).fetchall()
    entries: list[LedgerEntry] = []
    for mark in marks:
        payload = _payload_of(mark)
        technician_id = int(payload["technician_id"])
        wages_cents = int(payload["block_hours"]) * int(payload["hourly_wage_cents"])
        entries.append(
            LedgerEntry(
                entry_type=LedgerCategory.PAYROLL_OFF_HOURS,
                amount_cents=-wages_cents,
                occurred_at_ms=occurred_at_ms,
                service_date=service_date,
                description="off-hours wages",
                technician_id=technician_id,
            )
        )
        entries.append(
            LedgerEntry(
                entry_type=LedgerCategory.PAYROLL_OFF_HOURS_BONUS,
                amount_cents=-_off_hours_bonus_cents(config),
                occurred_at_ms=occurred_at_ms,
                service_date=service_date,
                description="off-hours bonus",
                technician_id=technician_id,
            )
        )
    return entries


def _weekday_payroll_entries(
    ctx: TxnContext, service_date: str, occurred_at_ms: int
) -> list[LedgerEntry]:
    placeholders = ", ".join("?" for _ in _PAYING_EMPLOYMENT_STATUSES)
    technicians = ctx.conn.execute(
        f"SELECT id, hourly_wage_cents FROM _technicians "
        f"WHERE employment_status IN ({placeholders}) "
        "AND joined_on <= ? AND (final_working_date IS NULL OR final_working_date >= ?) "
        "ORDER BY id",
        (*_PAYING_EMPLOYMENT_STATUSES, service_date, service_date),
    ).fetchall()
    return [
        LedgerEntry(
            entry_type=LedgerCategory.PAYROLL_BASE,
            amount_cents=-(
                constants.WEEKDAY_PAY_MULTIPLIER * int(technician["hourly_wage_cents"])
            ),
            occurred_at_ms=occurred_at_ms,
            service_date=service_date,
            description="weekday base payroll",
            technician_id=int(technician["id"]),
        )
        for technician in technicians
    ]


def _daily_settlement_handler(config: RunConfig) -> EventHandler:

    def handler(ctx: TxnContext, row: sqlite3.Row) -> dict | None:
        now = _advance_to_event(ctx, row)
        service_date = str(_payload_of(row)["service_date"])
        day = ctx.conn.execute(
            "SELECT day_type, settlement_status FROM _day_state WHERE service_date = ?",
            (service_date,),
        ).fetchone()
        if day is None or day["settlement_status"] != "pending":
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                f"day {service_date} is not awaiting settlement",
            )
        entries = _off_hours_entries(ctx, config, service_date, now)
        if day["day_type"] == DayType.WEEKDAY.value:
            entries.extend(_weekday_payroll_entries(ctx, service_date, now))
        entries.append(
            LedgerEntry(
                entry_type=LedgerCategory.OPERATING_OVERHEAD,
                amount_cents=-config.get_int("finance.daily_operating_overhead"),
                occurred_at_ms=now,
                service_date=service_date,
                description="daily operating overhead",
            )
        )
        ctx.services.ledger.post(entries)
        ctx.conn.execute(
            "UPDATE _day_state SET settlement_status = 'settled' WHERE service_date = ?",
            (service_date,),
        )
        return None

    return handler


def _checkpoint_handler(ctx: TxnContext, row: sqlite3.Row) -> dict | None:
    now = _advance_to_event(ctx, row)
    service_date = str(_payload_of(row)["service_date"])
    events = Events(ctx.conn, ctx.rng)
    run_row = ctx.conn.execute("SELECT last_service_date FROM _run").fetchone()
    if service_date == str(run_row["last_service_date"]):
        events.schedule(EventKind.RUN_FINALIZE, now)
    else:
        next_date = (date.fromisoformat(service_date) + timedelta(days=1)).isoformat()
        events.schedule(
            EventKind.DAY_ROLLOVER,
            _instant(next_date, constants.OPENING_HOUR * 60 + constants.OPENING_MINUTE),
        )
    return None


def _run_finalize_handler(ctx: TxnContext, row: sqlite3.Row) -> dict | None:
    _advance_to_event(ctx, row)
    run_row = ctx.conn.execute("SELECT run_id FROM _run").fetchone()
    db.set_run_status(ctx.conn, str(run_row["run_id"]), RunStatus.COMPLETED)
    return None


def build_handlers(config: RunConfig) -> dict[EventKind, EventHandler]:
    handlers: dict[EventKind, EventHandler] = {
        EventKind.DAY_ROLLOVER: _day_rollover_handler,
        EventKind.WEATHER_REALIZED: _weather_realized_handler,
        EventKind.DEMAND_GENERATION: _demand_generation_handler(config),
        EventKind.OPENING_INVOCATION: _opening_invocation_handler,
        EventKind.FIRST_BLOCK_LOCK: _block_lock_handler,
        EventKind.BLOCK_LOCK: _block_lock_handler,
        EventKind.CALL_INVOCATION: _call_invocation_handler,
        EventKind.CLOSING_INVOCATION: _closing_invocation_handler,
        EventKind.DAILY_SETTLEMENT: _daily_settlement_handler(config),
        EventKind.CHECKPOINT: _checkpoint_handler,
        EventKind.RUN_FINALIZE: _run_finalize_handler,
    }
    for kind in _STUB_EVENT_KINDS:
        handlers[kind] = _stub_handler
    return handlers


class Controller:

    def __init__(
        self, conn: sqlite3.Connection, config: RunConfig, adapter: HarnessAdapter
    ) -> None:
        run_row = conn.execute("SELECT run_id, master_seed FROM _run").fetchone()
        if run_row is None:
            raise EngineError(ErrorCode.ENTITY_NOT_FOUND, "run record not found")
        self.conn = conn
        self.config = config
        self.adapter = adapter
        self.run_id = str(run_row["run_id"])
        rng = Rng(
            int(run_row["master_seed"]),
            SPEC_VERSION,
            audit_recorder=sqlite_audit_recorder(conn),
            conn=conn,
        )
        self.events = Events(conn, rng)
        self.invocations = Invocations(conn)
        self.services = Services(
            conn=conn,
            rng=rng,
            ledger=Ledger(conn),
            invocations=self.invocations,
            clock=clock,
            events=self.events,
        )
        self.handlers = build_handlers(config)

    def run(self, *, max_steps: int | None = None) -> RunStatus:
        steps = 0
        while max_steps is None or steps < max_steps:
            if not self.step():
                break
            steps += 1
        return self.status()

    def status(self) -> RunStatus:
        row = self.conn.execute("SELECT status FROM _run").fetchone()
        if row is None:
            raise EngineError(ErrorCode.ENTITY_NOT_FOUND, "run record not found")
        return RunStatus(row["status"])

    def step(self) -> bool:
        if self.status() in RunStatus.terminal():
            return False
        active = self.invocations.active()
        if active is not None:
            self._resume_active_invocation(active)
            return True
        unissued = self._unissued_invocation_event()
        if unissued is not None:
            self._issue_invocation(unissued)
            return True
        if self._schedule_closing_if_owed():
            return True
        row = self.events.next_pending()
        if row is None:
            return False
        self._apply_event(row)
        return True

    def _apply_event(self, row: sqlite3.Row) -> None:
        kind = EventKind(row["kind"])
        handler = self.handlers.get(kind)
        if handler is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"no handler registered for event kind {kind.value!r}",
            )
        _require_ok(self.events.apply(int(row["event_id"]), handler, services=self.services))

    def _last_applied_invocation_event(self) -> sqlite3.Row | None:
        placeholders = ", ".join("?" for _ in _INVOCATION_EVENT_KINDS)
        return self.conn.execute(
            f"SELECT * FROM _events WHERE status = 'applied' AND kind IN ({placeholders}) "
            "ORDER BY applied_at_ms DESC, event_id DESC LIMIT 1",
            [k.value for k in _INVOCATION_EVENT_KINDS],
        ).fetchone()

    def _unissued_invocation_event(self) -> sqlite3.Row | None:
        invocation_rows = int(
            self.conn.execute("SELECT COUNT(*) c FROM _invocations").fetchone()["c"]
        )
        placeholders = ", ".join("?" for _ in _INVOCATION_EVENT_KINDS)
        applied_events = int(
            self.conn.execute(
                f"SELECT COUNT(*) c FROM _events "
                f"WHERE status = 'applied' AND kind IN ({placeholders})",
                [k.value for k in _INVOCATION_EVENT_KINDS],
            ).fetchone()["c"]
        )
        if applied_events == invocation_rows:
            return None
        if applied_events != invocation_rows + 1:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                "applied invocation events and invocation rows diverge by more than one",
            )
        return self._last_applied_invocation_event()

    def _issue_invocation(self, row: sqlite3.Row) -> None:
        invocation_type = _INVOCATION_TYPE_BY_KIND[EventKind(row["kind"])]
        ts = max(clock.now(self.conn), int(row["scheduled_at_ms"]))
        opened = self.invocations.open(invocation_type, ts, services=self.services)
        if isinstance(opened, bytes):
            _require_ok(opened)
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED, "invocation open failed without an error response"
            )
        self.adapter.send(self._build_prompt(invocation_type, ts, row))
        _require_ok(self.invocations.complete(int(opened), services=self.services))

    def _resume_active_invocation(self, active: sqlite3.Row) -> None:
        row = self._last_applied_invocation_event()
        if row is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                "an invocation is active but no applied invocation event exists",
            )
        invocation_type = InvocationType(active["type"])
        if _INVOCATION_TYPE_BY_KIND[EventKind(row["kind"])] is not invocation_type:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                "active invocation type does not match the last applied invocation event",
            )
        self.adapter.send(self._build_prompt(invocation_type, int(active["ts_ms"]), row))
        _require_ok(
            self.invocations.complete(int(active["invocation_id"]), services=self.services)
        )

    def _build_prompt(
        self, invocation_type: InvocationType, ts: int, row: sqlite3.Row
    ) -> AdapterPrompt:
        if invocation_type is InvocationType.CALL:
            pending = self.conn.execute(
                "SELECT caller_name, caller_phone FROM _pending_calls WHERE id = ?",
                (row["entity_id"],),
            ).fetchone()
            if pending is None:
                raise EngineError(
                    ErrorCode.ENTITY_NOT_FOUND,
                    f"pending call {row['entity_id']} not found for prompt",
                )
            payload: dict[str, object] = {
                "time": clock.to_iso8601(ts),
                "caller_name": str(pending["caller_name"]),
                "caller_phone": str(pending["caller_phone"]),
            }
        elif invocation_type is InvocationType.OPENING:
            payload = {
                "service_date": _event_service_date(row),
                "time": clock.to_iso8601(ts),
            }
        else:
            payload = {
                "service_date": str(_payload_of(row)["service_date"]),
                "time": clock.to_iso8601(ts),
            }
        return AdapterPrompt(
            kind=_PROMPT_KIND_BY_TYPE[invocation_type], ts_ms=ts, payload=payload
        )

    def _schedule_closing_if_owed(self) -> bool:
        day = self.conn.execute(
            "SELECT service_date, unresolved_execution_count FROM _day_state "
            "WHERE opening_at IS NOT NULL AND closing_at IS NULL "
            "ORDER BY service_date LIMIT 1"
        ).fetchone()
        if day is None:
            return False
        if int(day["unresolved_execution_count"]) != 0:
            return False
        service_date = str(day["service_date"])
        payload_json = _canonical_payload({"service_date": service_date})
        already_scheduled = self.conn.execute(
            "SELECT 1 FROM _events WHERE kind = ? AND payload = ? LIMIT 1",
            (EventKind.CLOSING_INVOCATION.value, payload_json),
        ).fetchone()
        if already_scheduled is not None:
            return False
        ts = max(
            _instant(service_date, constants.CALL_WINDOW_END_MIN), clock.now(self.conn)
        )
        next_row = self.events.next_pending()
        if next_row is not None:
            closing_key = (ts, EVENT_PRIORITY[EventKind.CLOSING_INVOCATION])
            next_key = (int(next_row["scheduled_at_ms"]), int(next_row["priority"]))
            if closing_key >= next_key:
                return False

        def fn(ctx: TxnContext) -> dict:
            event_id = Events(ctx.conn, ctx.rng).schedule(
                EventKind.CLOSING_INVOCATION, ts, payload={"service_date": service_date}
            )
            return {"event_id": event_id}

        _require_ok(
            privileged(
                self.services, privileged_request_id("schedule_closing", service_date), fn
            )
        )
        return True


__all__ = ["BLOCK_CODES", "Controller", "build_handlers"]
