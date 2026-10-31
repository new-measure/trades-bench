from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import (
    SPEC_VERSION,
    calls,
    clock,
    constants,
    control,
    db,
    demand,
    execution,
    geo_reader,
    overtime,
)
from .adapter import AdapterPrompt, AdapterResponse, ControllerAdapter
from .config import RunConfig
from .enums import (
    EVENT_PRIORITY,
    DayType,
    EventKind,
    EventStatus,
    InvocationType,
    LedgerCategory,
    PromptKind,
    RunStatus,
)
from .errors import EngineError, ErrorCode
from .events import EventHandler, Events
from .geo_reader import GeoArtifact
from .invocations import PROMPT_TEMPLATE_VERSION, Invocations
from .ledger import Ledger, LedgerEntry
from .rng import Rng, sqlite_audit_recorder
from .txn import (
    Services,
    TxnContext,
    fingerprint,
    privileged,
    privileged_request_id,
    privileged_with_replay,
)

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

_NO_PENDING_EVENT_MESSAGE = (
    "the run is active but no invocation is open and no event is pending"
)

CALL_ATTEMPT_LIMIT = 3


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
    now = _advance_to_event(ctx, row)
    result = calls.arrive(ctx.conn, row, now_ms=now)
    return result if isinstance(result, dict) else None


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
    return config.get_int("payroll.off_hours_bonus_per_job")


def _off_hours_entries(
    ctx: TxnContext, config: RunConfig, service_date: str, occurred_at_ms: int
) -> list[LedgerEntry]:
    day_start = _instant(service_date, 0)
    next_day = (date.fromisoformat(service_date) + timedelta(days=1)).isoformat()
    day_end = _instant(next_day, 0)
    marks = ctx.conn.execute(
        "SELECT * FROM _events WHERE kind = ? AND status = 'applied' "
        "AND scheduled_at_ms >= ? AND scheduled_at_ms < ? "
        "ORDER BY order_key, event_id",
        (EventKind.OFF_HOURS_ACCRUAL_MARK.value, day_start, day_end),
    ).fetchall()
    entries: list[LedgerEntry] = []
    for mark in marks:
        payload = _payload_of(mark)
        if "technician_id" not in payload:
            continue
        technician_id = int(payload["technician_id"])
        job_id = int(payload["job_id"])
        wages_cents = int(payload["block_hours"]) * int(payload["hourly_wage_cents"])
        entries.append(
            LedgerEntry(
                entry_type=LedgerCategory.PAYROLL_OFF_HOURS,
                amount_cents=-wages_cents,
                occurred_at_ms=occurred_at_ms,
                service_date=service_date,
                description="off-hours wages",
                technician_id=technician_id,
                job_id=job_id,
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
                job_id=job_id,
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
        next_day = (date.fromisoformat(service_date) + timedelta(days=1)).isoformat()
        entries.extend(
            LedgerEntry(
                entry_type=LedgerCategory.PAYROLL_OFF_HOURS,
                amount_cents=-pay.amount_cents,
                occurred_at_ms=now,
                service_date=service_date,
                description=f"overtime wages ({pay.minutes} min past shift end)",
                technician_id=pay.technician_id,
                job_id=pay.job_id,
            )
            for pay in overtime.overtime_pay(
                ctx.conn, service_date,
                day_start=_instant(service_date, 0), day_end=_instant(next_day, 0),
            )
        )
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
    run_id = str(run_row["run_id"])
    ctx.conn.execute(
        "UPDATE _run SET final_score_cents = ? WHERE run_id = ?",
        (ctx.services.ledger.cash_balance(), run_id),
    )
    db.set_run_status(ctx.conn, run_id, RunStatus.COMPLETED)
    return None


def build_handlers(
    config: RunConfig, artifact: GeoArtifact | None = None
) -> dict[EventKind, EventHandler]:
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
    handlers.update(execution.build_handlers(config, artifact))
    for kind in _STUB_EVENT_KINDS:
        handlers[kind] = _stub_handler
    return handlers


@dataclass(frozen=True)
class AppliedEvent:

    event_id: int
    kind: str
    ts_ms: int

    def to_result(self) -> dict:
        return {"event_id": self.event_id, "kind": self.kind, "ts_ms": self.ts_ms}


@dataclass(frozen=True)
class OpenInvocation:

    invocation_id: int
    kind: InvocationType
    ts_ms: int
    payload: dict
    prompt_version: str
    applied_events: tuple[AppliedEvent, ...]

    def to_result(self) -> dict:
        return {
            "invocation_id": self.invocation_id,
            "kind": self.kind.value,
            "ts_ms": self.ts_ms,
            "payload": self.payload,
            "prompt_version": self.prompt_version,
            "applied_events": [event.to_result() for event in self.applied_events],
        }


class Controller:

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: RunConfig,
        adapter: ControllerAdapter | None = None,
        *,
        artifact_dir: str | Path | None = None,
    ) -> None:
        run_row = conn.execute("SELECT master_seed FROM _run").fetchone()
        if run_row is None:
            raise EngineError(ErrorCode.ENTITY_NOT_FOUND, "run record not found")
        db.require_matching_spec_version(conn)
        artifact: GeoArtifact | None = None
        if artifact_dir is not None:
            geo_reader.verify_artifact_sha256(
                artifact_dir, config.get_string("geography.artifact_sha256")
            )
            artifact = geo_reader.load_artifact(artifact_dir)
        rng = Rng(
            int(run_row["master_seed"]),
            SPEC_VERSION,
            audit_recorder=sqlite_audit_recorder(conn),
            conn=conn,
        )
        services = Services(
            conn=conn,
            rng=rng,
            ledger=Ledger(conn),
            invocations=Invocations(conn),
            clock=clock,
            events=Events(conn, rng),
        )
        self._bind(services, config, adapter, artifact)

    @classmethod
    def from_services(
        cls,
        services: Services,
        config: RunConfig,
        *,
        adapter: ControllerAdapter | None = None,
        artifact: GeoArtifact | None = None,
    ) -> Controller:
        controller = cls.__new__(cls)
        controller._bind(services, config, adapter, artifact)
        return controller

    def _bind(
        self,
        services: Services,
        config: RunConfig,
        adapter: ControllerAdapter | None,
        artifact: GeoArtifact | None,
    ) -> None:
        run_row = services.conn.execute("SELECT run_id FROM _run").fetchone()
        if run_row is None:
            raise EngineError(ErrorCode.ENTITY_NOT_FOUND, "run record not found")
        self.conn = services.conn
        self.config = config
        self.adapter = adapter
        self.run_id = str(run_row["run_id"])
        self.artifact = artifact
        self.services = services
        self.events = services.events
        self.invocations = services.invocations
        self.handlers = build_handlers(config, artifact)

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
            raise EngineError(ErrorCode.INVARIANT_VIOLATED, _NO_PENDING_EVENT_MESSAGE)
        self._apply_event(row)
        return True

    def advance_to_invocation(self) -> OpenInvocation | None:
        applied: list[AppliedEvent] = []
        while True:
            if self.status() in RunStatus.terminal():
                return None
            active = self.invocations.active()
            if active is not None:
                return self._describe_active_invocation(active, tuple(applied))
            unissued = self._unissued_invocation_event()
            if unissued is not None:
                return self._open_invocation(unissued, tuple(applied))
            if self._schedule_closing_if_owed():
                continue
            row = self.events.next_pending()
            if row is None:
                raise EngineError(ErrorCode.INVARIANT_VIOLATED, _NO_PENDING_EVENT_MESSAGE)
            self._apply_event(row)
            entry = self._applied_entry(int(row["event_id"]))
            if entry is not None:
                applied.append(entry)

    def fail_run(self, *, request_id: str, reason: str) -> tuple[bytes, bool]:

        def fn(ctx: TxnContext) -> dict:
            db.set_run_status(ctx.conn, self.run_id, RunStatus.FAILED)
            return {
                "run_id": self.run_id,
                "run_status": RunStatus.FAILED.value,
                "reason": reason,
            }

        return privileged_with_replay(
            self.services,
            privileged_request_id("control_fail_run", request_id),
            fn,
            fingerprint_value=fingerprint("control.fail_run", {"reason": reason}),
        )

    def _applied_entry(self, event_id: int) -> AppliedEvent | None:
        row = self.conn.execute(
            "SELECT kind, status, applied_at_ms FROM _events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None or row["status"] != EventStatus.APPLIED.value:
            return None
        return AppliedEvent(
            event_id=event_id, kind=str(row["kind"]), ts_ms=int(row["applied_at_ms"])
        )

    def _describe_active_invocation(
        self, active: sqlite3.Row, applied: tuple[AppliedEvent, ...]
    ) -> OpenInvocation:
        row = self._origin_event_of(active)
        invocation_type = InvocationType(active["type"])
        if _INVOCATION_TYPE_BY_KIND[EventKind(row["kind"])] is not invocation_type:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                "active invocation type does not match the last applied invocation event",
            )
        ts = int(active["ts_ms"])
        version = active["prompt_version"] or PROMPT_TEMPLATE_VERSION
        return OpenInvocation(
            invocation_id=int(active["invocation_id"]),
            kind=invocation_type,
            ts_ms=ts,
            payload=self._payload_for(invocation_type, ts, row),
            prompt_version=str(version),
            applied_events=applied,
        )

    def _apply_event(self, row: sqlite3.Row) -> None:
        kind = EventKind(row["kind"])
        handler = self.handlers.get(kind)
        if handler is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"no handler registered for event kind {kind.value!r}",
            )
        _require_ok(self.events.apply(int(row["event_id"]), handler, services=self.services))

    def _unissued_invocation_event(self) -> sqlite3.Row | None:
        placeholders = ", ".join("?" for _ in _INVOCATION_EVENT_KINDS)
        rows = self.conn.execute(
            f"SELECT * FROM _events WHERE status = 'applied' AND kind IN ({placeholders}) "
            "AND event_id NOT IN "
            "(SELECT origin_event_id FROM _invocations WHERE origin_event_id IS NOT NULL) "
            "ORDER BY event_id",
            [k.value for k in _INVOCATION_EVENT_KINDS],
        ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                "more than one applied invocation event has no invocation of its own",
            )
        return rows[0]

    def _origin_event_of(self, invocation: sqlite3.Row) -> sqlite3.Row:
        origin_event_id = invocation["origin_event_id"]
        if origin_event_id is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"invocation {int(invocation['invocation_id'])} has no origin event",
            )
        row = self.conn.execute(
            "SELECT * FROM _events WHERE event_id = ?", (int(origin_event_id),)
        ).fetchone()
        if row is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"origin event {int(origin_event_id)} of invocation "
                f"{int(invocation['invocation_id'])} does not exist",
            )
        return row

    def _open_invocation(
        self, row: sqlite3.Row, applied: tuple[AppliedEvent, ...]
    ) -> OpenInvocation:
        invocation_type = _INVOCATION_TYPE_BY_KIND[EventKind(row["kind"])]
        ts = max(clock.now(self.conn), int(row["scheduled_at_ms"]))
        event_id = int(row["event_id"])
        opened = self.invocations.open(
            invocation_type,
            ts,
            request_id=f"invocation-open-{event_id}",
            prompt_version=PROMPT_TEMPLATE_VERSION,
            origin_event_id=event_id,
            services=self.services,
        )
        if isinstance(opened, bytes):
            _require_ok(opened)
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED, "invocation open failed without an error response"
            )
        return OpenInvocation(
            invocation_id=int(opened),
            kind=invocation_type,
            ts_ms=ts,
            payload=self._payload_for(invocation_type, ts, row),
            prompt_version=PROMPT_TEMPLATE_VERSION,
            applied_events=applied,
        )

    def _issue_invocation(self, row: sqlite3.Row) -> None:
        opened = self._open_invocation(row, ())
        self._drive_invocation(opened.kind, opened.ts_ms, row, opened.invocation_id)

    def _resume_active_invocation(self, active: sqlite3.Row) -> None:
        row = self._origin_event_of(active)
        invocation_type = InvocationType(active["type"])
        if _INVOCATION_TYPE_BY_KIND[EventKind(row["kind"])] is not invocation_type:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                "active invocation type does not match the last applied invocation event",
            )
        self._drive_invocation(
            invocation_type, int(active["ts_ms"]), row, int(active["invocation_id"])
        )

    def _drive_invocation(
        self, invocation_type: InvocationType, ts: int, row: sqlite3.Row, invocation_id: int
    ) -> None:
        if invocation_type is InvocationType.CALL:
            self._drive_call_invocation(ts, row)
        else:
            self._send(self._build_prompt(invocation_type, ts, row))
        event_id = int(row["event_id"])
        _require_ok(
            self.invocations.complete(
                invocation_id,
                request_id=f"invocation-complete-{event_id}",
                services=self.services,
            )
        )

    def _drive_call_invocation(self, ts: int, row: sqlite3.Row) -> None:
        call_id = calls.arrived_call_id(self.conn, int(row["event_id"]))
        attempts = self._recorded_attempt_count(call_id)
        while not calls.reached_terminal_state(self.conn, call_id):
            self._send(self._call_prompt(ts, row, call_id, attempts))
            if calls.reached_terminal_state(self.conn, call_id):
                return
            attempts += 1
            if attempts == CALL_ATTEMPT_LIMIT:
                _require_ok(
                    control.record_final_unanswered_attempt_op(
                        self.services,
                        self.config,
                        call_id=call_id,
                        attempt_number=attempts,
                        artifact=self.artifact,
                    )
                )
            else:
                _require_ok(
                    control.record_call_attempt_op(
                        self.services, self.config, call_id=call_id, attempt_number=attempts
                    )
                )

    def _call_prompt(
        self, ts: int, row: sqlite3.Row, call_id: int, attempts: int
    ) -> AdapterPrompt:
        if attempts == 0:
            return self._build_prompt(InvocationType.CALL, ts, row)
        return AdapterPrompt(
            kind=PromptKind.REMINDER,
            ts_ms=ts,
            payload={
                "call_id": call_id,
                "attempt": attempts,
                "final": attempts == CALL_ATTEMPT_LIMIT - 1,
            },
        )

    def _recorded_attempt_count(self, call_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM _call_attempts WHERE call_id = ? AND outcome = 'incomplete'",
            (call_id,),
        ).fetchone()
        return int(row["c"])

    def _send(self, prompt: AdapterPrompt) -> AdapterResponse:
        if self.adapter is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED, "this controller has no adapter to drive"
            )
        try:
            return self.adapter.send(prompt)
        except Exception:
            self._fail_run()
            raise

    def _fail_run(self) -> None:
        if self.status() in RunStatus.terminal():
            return

        def fn(ctx: TxnContext) -> None:
            db.set_run_status(ctx.conn, self.run_id, RunStatus.FAILED)
            return None

        _require_ok(
            privileged(self.services, privileged_request_id("run_failed", self.run_id), fn)
        )

    def _payload_for(
        self, invocation_type: InvocationType, ts: int, row: sqlite3.Row
    ) -> dict:
        if invocation_type is InvocationType.CALL:
            return calls.prompt_payload(self.conn, row, ts)
        if invocation_type is InvocationType.OPENING:
            return {
                "service_date": _event_service_date(row),
                "time": clock.to_iso8601(ts),
            }
        return {
            "service_date": str(_payload_of(row)["service_date"]),
            "time": clock.to_iso8601(ts),
        }

    def _build_prompt(
        self, invocation_type: InvocationType, ts: int, row: sqlite3.Row
    ) -> AdapterPrompt:
        return AdapterPrompt(
            kind=_PROMPT_KIND_BY_TYPE[invocation_type],
            ts_ms=ts,
            payload=self._payload_for(invocation_type, ts, row),
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


__all__ = [
    "BLOCK_CODES",
    "AppliedEvent",
    "Controller",
    "OpenInvocation",
    "build_handlers",
]
