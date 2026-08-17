from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from . import calls, clock, constants, surface
from .config import RunConfig
from .enums import DayType, EntityType, EventKind
from .errors import EngineError, ErrorCode
from .events import Events
from .geo_reader import GeoArtifact
from .txn import Services, TxnContext, command

BLOCK_CODES: tuple[str, ...] = ("B07_09", "B09_11", "B11_13", "B13_15", "B15_17", "B17_19")

_BLOCK_START_MINUTES: dict[str, int] = {
    code: start
    for code, (start, _end) in zip(BLOCK_CODES, constants.BLOCK_GRID_MINUTES, strict=True)
}

_BLOCK_END_MINUTES: dict[str, int] = {
    code: end
    for code, (_start, end) in zip(BLOCK_CODES, constants.BLOCK_GRID_MINUTES, strict=True)
}

_TERMINAL_APPOINTMENT_STATUSES = frozenset({"completed", "failed", "no_show", "cancelled"})


def _instant(service_date: str, minutes_from_midnight: int) -> int:
    d = date.fromisoformat(service_date)
    return clock.phoenix_instant(
        d.year, d.month, d.day, minutes_from_midnight // 60, minutes_from_midnight % 60
    )


def _block_window(service_date: str, block_code: str) -> tuple[int, int]:
    return (
        _instant(service_date, _BLOCK_START_MINUTES[block_code]),
        _instant(service_date, _BLOCK_END_MINUTES[block_code]),
    )


def _state_illegal(reason: str, message: str, **details: object) -> EngineError:
    return EngineError(ErrorCode.STATE_ILLEGAL, message, details={"reason": reason, **details})


def _call_row(conn: sqlite3.Connection, call_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"call {call_id} not found")
    return row


def validate_reservation_shape(scheduled_blocks: int, block_codes: tuple[str, ...]) -> None:
    if len(block_codes) != scheduled_blocks:
        raise _state_illegal(
            "grid_violation",
            f"expected {scheduled_blocks} reserved block(s), got {len(block_codes)}",
            scheduled_blocks=scheduled_blocks, block_codes=list(block_codes),
        )
    for code in block_codes:
        if code not in BLOCK_CODES:
            raise _state_illegal(
                "grid_violation", f"unknown block_code {code!r}", block_code=code
            )
    if scheduled_blocks == 1:
        return
    if scheduled_blocks != constants.INSTALL_BLOCKS:
        raise _state_illegal(
            "grid_violation",
            f"unsupported reservation length {scheduled_blocks}",
            scheduled_blocks=scheduled_blocks,
        )
    first, second = block_codes
    if _BLOCK_START_MINUTES[first] > constants.INSTALL_MAX_START_MIN:
        raise _state_illegal(
            "grid_violation",
            f"an install reservation may not start at {first}",
            block_code=first,
        )
    expected_index = BLOCK_CODES.index(first) + 1
    if expected_index >= len(BLOCK_CODES) or second != BLOCK_CODES[expected_index]:
        raise _state_illegal(
            "grid_violation",
            "install reservation blocks must be consecutive within the same day",
            block_codes=list(block_codes),
        )


def _derive_reservation_blocks(scheduled_blocks: int, first_block_code: str) -> tuple[str, ...]:
    if first_block_code not in BLOCK_CODES:
        raise _state_illegal(
            "grid_violation",
            f"unknown block_code {first_block_code!r}",
            block_code=first_block_code,
        )
    start_index = BLOCK_CODES.index(first_block_code)
    end_index = start_index + scheduled_blocks
    if end_index > len(BLOCK_CODES):
        raise _state_illegal(
            "grid_violation",
            f"a {scheduled_blocks}-block reservation starting at {first_block_code} "
            "would cross past the end of the day",
            block_code=first_block_code, scheduled_blocks=scheduled_blocks,
        )
    return BLOCK_CODES[start_index:end_index]


def _job_type_row(conn: sqlite3.Connection, job_type_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, code, scheduled_blocks, business_unit_id FROM _job_types WHERE id = ?",
        (job_type_id,),
    ).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"job_type_id {job_type_id} not found")
    return row


def _technician_employment_active(conn: sqlite3.Connection, technician_id: int) -> bool:
    row = conn.execute(
        "SELECT employment_status FROM _technicians WHERE id = ?", (technician_id,)
    ).fetchone()
    return row is not None and row["employment_status"] == "active"


def _technician_on_call(conn: sqlite3.Connection, technician_id: int, service_date: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM _on_call_rotations WHERE technician_id = ? AND status = 'active' "
        "AND effective_start_date <= ? AND (effective_end_date IS NULL OR effective_end_date >= ?) "
        "LIMIT 1",
        (technician_id, service_date, service_date),
    ).fetchone()
    return row is not None


def _derive_shift_coverage(
    conn: sqlite3.Connection, technician_id: int, service_date: str, block_code: str
) -> bool:
    if not _technician_employment_active(conn, technician_id):
        return False
    day_type = DayType.from_iso_weekday(date.fromisoformat(service_date).isoweekday())
    if day_type is DayType.WEEKEND:
        return _technician_on_call(conn, technician_id, service_date)
    if block_code == "B17_19":
        return _technician_on_call(conn, technician_id, service_date)
    return True


def _check_shift_coverage(
    conn: sqlite3.Connection, technician_id: int, service_date: str, block_codes: tuple[str, ...]
) -> None:
    for block_code in block_codes:
        block_start, block_end = _block_window(service_date, block_code)
        covered = conn.execute(
            "SELECT 1 FROM _technician_shifts WHERE technician_id = ? AND service_date = ? "
            "AND active = 1 AND start_at <= ? AND end_at >= ? LIMIT 1",
            (technician_id, service_date, block_start, block_end),
        ).fetchone()
        if covered is not None:
            continue
        materialized = conn.execute(
            "SELECT 1 FROM _technician_shifts WHERE technician_id = ? AND service_date = ? "
            "LIMIT 1",
            (technician_id, service_date),
        ).fetchone()
        if materialized is None and _derive_shift_coverage(
            conn, technician_id, service_date, block_code
        ):
            continue
        raise _state_illegal(
            "shift_coverage",
            f"technician {technician_id} has no active shift covering {block_code} "
            f"on {service_date}",
            technician_id=technician_id, service_date=service_date, block_code=block_code,
        )


def _check_not_locked(
    conn: sqlite3.Connection,
    service_date: str,
    block_code: str,
    *,
    appointment_id: int | None,
) -> None:
    locked = conn.execute(
        "SELECT 1 FROM _block_locks WHERE service_date = ? AND block_code = ?",
        (service_date, block_code),
    ).fetchone()
    if locked is not None:
        detail: dict[str, object] = {"service_date": service_date, "block_code": block_code}
        if appointment_id is None:
            message = f"block {block_code} on {service_date} is locked and cannot be booked"
        else:
            message = (
                f"appointment {appointment_id} block {block_code} is locked "
                "and cannot be reassigned"
            )
            detail["appointment_id"] = appointment_id
        raise _state_illegal("locked_appointment", message, **detail)


def _check_overlap(
    conn: sqlite3.Connection,
    technician_id: int,
    block_starts: list[tuple[str, int]],
    *,
    exclude_appointment_id: int | None,
) -> None:
    for block_code, block_start in block_starts:
        query = "SELECT 1 FROM _technician_block_holds WHERE technician_id = ? AND block_start = ?"
        params: list[object] = [technician_id, block_start]
        if exclude_appointment_id is not None:
            query += " AND (appointment_id IS NULL OR appointment_id != ?)"
            params.append(exclude_appointment_id)
        existing = conn.execute(query, params).fetchone()
        if existing is not None:
            raise _state_illegal(
                "overlap",
                f"technician {technician_id} already holds block {block_code} "
                f"starting at {block_start}",
                technician_id=technician_id, block_code=block_code,
            )


@dataclass(frozen=True)
class BookingPlan:

    job_type_id: int
    job_type_code: str
    business_unit_id: int | None
    scheduled_blocks: int
    technician_id: int
    service_date: str
    block_codes: tuple[str, ...]
    windows: tuple[tuple[int, int], ...]


def validate_booking(
    ctx: TxnContext,
    config: RunConfig,
    *,
    job_type_id: int,
    technician_id: int,
    service_date: str,
    first_block_code: str,
) -> BookingPlan:
    conn = ctx.conn
    now = ctx.now()

    job_type = _job_type_row(conn, job_type_id)
    scheduled_blocks = int(job_type["scheduled_blocks"])
    block_codes = _derive_reservation_blocks(scheduled_blocks, first_block_code)
    validate_reservation_shape(scheduled_blocks, block_codes)

    _check_not_locked(conn, service_date, block_codes[0], appointment_id=None)

    windows = [_block_window(service_date, code) for code in block_codes]
    first_start = windows[0][0]
    if first_start <= now:
        raise _state_illegal(
            "block_already_started",
            f"first reserved block {block_codes[0]} on {service_date} does not start "
            "after the current time",
            service_date=service_date, block_code=block_codes[0],
        )

    _check_shift_coverage(conn, technician_id, service_date, block_codes)
    _check_overlap(
        conn, technician_id,
        list(zip(block_codes, [w[0] for w in windows], strict=True)),
        exclude_appointment_id=None,
    )

    return BookingPlan(
        job_type_id=int(job_type["id"]),
        job_type_code=job_type["code"],
        business_unit_id=job_type["business_unit_id"],
        scheduled_blocks=scheduled_blocks,
        technician_id=technician_id,
        service_date=service_date,
        block_codes=block_codes,
        windows=tuple(windows),
    )


def create_booking(
    ctx: TxnContext,
    config: RunConfig,
    plan: BookingPlan,
    *,
    call_id: int,
    customer_id: int,
    location_id: int,
    requested_service_name: str,
    priority: str,
    summary: str,
    recall_for_job_id: int | None = None,
    artifact: GeoArtifact | None = None,
) -> dict:
    from . import execution

    conn = ctx.conn
    now = ctx.now()

    technician_id = plan.technician_id
    service_date = plan.service_date
    block_codes = plan.block_codes
    windows = plan.windows
    scheduled_blocks = plan.scheduled_blocks

    job_cur = conn.execute(
        "INSERT INTO _jobs (job_number, call_id, customer_id, location_id, job_type_id, "
        "business_unit_id, requested_service_name, status, priority, no_charge, "
        "recall_for_job_id, summary, created_at, modified_at) "
        "VALUES ('', ?, ?, ?, ?, ?, ?, 'scheduled', ?, 0, ?, ?, ?, ?)",
        (
            call_id, customer_id, location_id, plan.job_type_id, plan.business_unit_id,
            requested_service_name, priority, recall_for_job_id, summary, now, now,
        ),
    )
    job_id = int(job_cur.lastrowid)
    job_number = f"J-{job_id:06d}"
    conn.execute("UPDATE _jobs SET job_number = ? WHERE id = ?", (job_number, job_id))

    scheduled_start = windows[0][0]
    scheduled_end = windows[-1][1]
    arrival_window_start = windows[0][0]
    arrival_window_end = windows[0][1]
    appointment_cur = conn.execute(
        "INSERT INTO _appointments (appointment_number, job_id, status, scheduled_start, "
        "scheduled_end, arrival_window_start, arrival_window_end, created_at, modified_at) "
        "VALUES ('', ?, 'scheduled', ?, ?, ?, ?, ?, ?)",
        (
            job_id, scheduled_start, scheduled_end,
            arrival_window_start, arrival_window_end, now, now,
        ),
    )
    appointment_id = int(appointment_cur.lastrowid)
    appointment_number = f"A-{appointment_id:06d}"
    conn.execute(
        "UPDATE _appointments SET appointment_number = ? WHERE id = ?",
        (appointment_number, appointment_id),
    )

    for ordinal, (block_code, (block_start, block_end)) in enumerate(
        zip(block_codes, windows, strict=True), start=1
    ):
        conn.execute(
            "INSERT INTO _appointment_blocks (appointment_id, ordinal, block_code, "
            "block_start, block_end, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (appointment_id, ordinal, block_code, block_start, block_end, now),
        )

    assignment_cur = conn.execute(
        "INSERT INTO _appointment_assignments (appointment_id, job_id, technician_id, status, "
        "assigned_at, unassigned_at, assigned_by, created_at) "
        "VALUES (?, ?, ?, 'active', ?, NULL, 'agent', ?)",
        (appointment_id, job_id, technician_id, now, now),
    )
    assignment_id = int(assignment_cur.lastrowid)

    for _block_code, (block_start, _block_end) in zip(block_codes, windows, strict=True):
        conn.execute(
            "INSERT INTO _technician_block_holds (technician_id, block_start, hold_kind, "
            "appointment_id, non_job_appointment_id, created_at) "
            "VALUES (?, ?, 'job', ?, NULL, ?)",
            (technician_id, block_start, appointment_id, now),
        )

    day_type = DayType.from_iso_weekday(date.fromisoformat(service_date).isoweekday())
    off_hours_indices = [
        idx
        for idx, code in enumerate(block_codes)
        if day_type is DayType.WEEKEND or code == "B17_19"
    ]
    if off_hours_indices:
        first_off_hours_start = windows[off_hours_indices[0]][0]
        Events(conn, ctx.rng).schedule(
            EventKind.OFF_HOURS_ACCRUAL_MARK,
            first_off_hours_start,
            entity_type=EntityType.APPOINTMENT.value,
            entity_id=appointment_id,
            payload={"appointment_id": appointment_id, "block_hours": 2 * scheduled_blocks},
        )

    Events(conn, ctx.rng).schedule(
        EventKind.COMPANY_NO_SHOW,
        windows[0][1],
        entity_type=EntityType.APPOINTMENT.value,
        entity_id=appointment_id,
        payload={"appointment_id": appointment_id},
    )

    if execution.first_trip_dispatch_applied(conn, service_date):
        conn.execute(
            "UPDATE _day_state SET unresolved_execution_count = unresolved_execution_count + 1 "
            "WHERE service_date = ?",
            (service_date,),
        )
        execution.replan_same_day(
            ctx, config, artifact,
            technician_id=technician_id, service_date=service_date,
            appointment_id=appointment_id,
        )

    return {
        "job_id": job_id,
        "job_number": job_number,
        "appointment_id": appointment_id,
        "appointment_number": appointment_number,
        "technician_id": technician_id,
        "assignment_id": assignment_id,
        "block_codes": list(block_codes),
    }


def book(
    ctx: TxnContext,
    config: RunConfig,
    *,
    call_id: int,
    customer_id: int,
    location_id: int,
    job_type_id: int,
    requested_service_name: str,
    priority: str,
    summary: str,
    technician_id: int,
    service_date: str,
    first_block_code: str,
    recall_for_job_id: int | None = None,
    artifact: GeoArtifact | None = None,
) -> dict:
    plan = validate_booking(
        ctx, config,
        job_type_id=job_type_id, technician_id=technician_id,
        service_date=service_date, first_block_code=first_block_code,
    )
    return create_booking(
        ctx, config, plan,
        call_id=call_id, customer_id=customer_id, location_id=location_id,
        requested_service_name=requested_service_name, priority=priority, summary=summary,
        recall_for_job_id=recall_for_job_id, artifact=artifact,
    )


def trip_index_for_appointment(conn: sqlite3.Connection, appointment_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM _appointment_assignments WHERE appointment_id = ?",
        (appointment_id,),
    ).fetchone()
    return int(row["c"]) - 1


def assign(
    ctx: TxnContext,
    config: RunConfig,
    *,
    appointment_id: int,
    technician_id: int,
    artifact: GeoArtifact | None = None,
) -> dict:
    surface.require_id("appointment_id", appointment_id)
    surface.require_id("technician_id", technician_id)

    from . import execution

    conn = ctx.conn
    now = ctx.now()

    appt = conn.execute(
        "SELECT id, job_id, status FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if appt is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND, f"appointment {appointment_id} not found"
        )
    if appt["status"] in _TERMINAL_APPOINTMENT_STATUSES:
        raise _state_illegal(
            "terminal_appointment",
            f"appointment {appointment_id} is terminal ({appt['status']}) and cannot "
            "be reassigned",
            appointment_id=appointment_id,
        )
    if appt["status"] != "scheduled":
        raise _state_illegal(
            "work_already_started",
            f"appointment {appointment_id} is {appt['status']} and cannot be reassigned",
            appointment_id=appointment_id,
        )

    block_rows = conn.execute(
        "SELECT ordinal, block_code, block_start, block_end FROM _appointment_blocks "
        "WHERE appointment_id = ? ORDER BY ordinal",
        (appointment_id,),
    ).fetchall()
    if not block_rows:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"appointment {appointment_id} has no reserved blocks",
        )
    first_block = block_rows[0]
    service_date = clock.service_date_for_ms(int(first_block["block_start"]))

    _check_not_locked(
        conn, service_date, first_block["block_code"], appointment_id=appointment_id
    )

    job_type_id = int(
        conn.execute(
            "SELECT job_type_id FROM _jobs WHERE id = ?", (appt["job_id"],)
        ).fetchone()["job_type_id"]
    )
    scheduled_blocks = int(_job_type_row(conn, job_type_id)["scheduled_blocks"])
    block_codes = tuple(row["block_code"] for row in block_rows)
    validate_reservation_shape(scheduled_blocks, block_codes)

    first_start = int(first_block["block_start"])
    if first_start <= now:
        raise _state_illegal(
            "block_already_started",
            f"appointment {appointment_id} first block {first_block['block_code']} "
            "does not start after the current time",
            appointment_id=appointment_id, block_code=first_block["block_code"],
        )

    _check_shift_coverage(conn, technician_id, service_date, block_codes)
    _check_overlap(
        conn, technician_id,
        [(row["block_code"], int(row["block_start"])) for row in block_rows],
        exclude_appointment_id=appointment_id,
    )

    current_active = conn.execute(
        "SELECT id, technician_id FROM _appointment_assignments "
        "WHERE appointment_id = ? AND status = 'active'",
        (appointment_id,),
    ).fetchone()
    previous_technician_id: int | None = None
    if current_active is not None:
        previous_technician_id = int(current_active["technician_id"])
        conn.execute(
            "UPDATE _appointment_assignments SET status = 'reassigned', unassigned_at = ? "
            "WHERE id = ?",
            (now, current_active["id"]),
        )

    conn.execute(
        "INSERT INTO _appointment_assignments (appointment_id, job_id, technician_id, status, "
        "assigned_at, unassigned_at, assigned_by, created_at) "
        "VALUES (?, ?, ?, 'active', ?, NULL, 'agent', ?)",
        (appointment_id, appt["job_id"], technician_id, now, now),
    )

    if previous_technician_id is not None:
        detail = json.dumps(
            {"previous_technician_id": previous_technician_id, "technician_id": technician_id},
            sort_keys=True, separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO _appointment_events (appointment_id, technician_id, event_type, "
            "occurred_at, service_date, detail, created_at) "
            "VALUES (?, ?, 'reassigned', ?, ?, ?, ?)",
            (appointment_id, technician_id, now, service_date, detail, now),
        )
        for row in block_rows:
            updated = conn.execute(
                "UPDATE _technician_block_holds SET technician_id = ? "
                "WHERE technician_id = ? AND block_start = ? AND appointment_id = ?",
                (technician_id, previous_technician_id, int(row["block_start"]), appointment_id),
            )
            if updated.rowcount != 1:
                raise EngineError(
                    ErrorCode.INVARIANT_VIOLATED,
                    f"expected exactly one block hold to move for appointment "
                    f"{appointment_id} block {row['block_code']}",
                )
    else:
        for row in block_rows:
            updated = conn.execute(
                "UPDATE _technician_block_holds SET technician_id = ? "
                "WHERE block_start = ? AND appointment_id = ?",
                (technician_id, int(row["block_start"]), appointment_id),
            )
            if updated.rowcount != 1:
                raise EngineError(
                    ErrorCode.INVARIANT_VIOLATED,
                    f"expected exactly one block hold to move for appointment "
                    f"{appointment_id} block {row['block_code']}",
                )

    if execution.first_trip_dispatch_applied(conn, service_date):
        if previous_technician_id is not None:
            execution.handle_outgoing_technician(
                ctx, config, artifact,
                appointment_id=appointment_id, technician_id=previous_technician_id,
                service_date=service_date,
            )
        execution.replan_same_day(
            ctx, config, artifact,
            technician_id=technician_id, service_date=service_date,
            appointment_id=appointment_id,
        )

    return {
        "appointment_id": appointment_id,
        "technician_id": technician_id,
        "previous_technician_id": previous_technician_id,
        "trip_index": trip_index_for_appointment(conn, appointment_id),
    }


def assign_command(
    services: Services,
    request_id: str,
    config: RunConfig,
    *,
    appointment_id: int,
    technician_id: int,
    artifact: GeoArtifact | None = None,
) -> bytes:
    return command(
        services,
        request_id,
        lambda ctx: assign(
            ctx, config, appointment_id=appointment_id, technician_id=technician_id,
            artifact=artifact,
        ),
        operation="dispatch.assign",
        args={"appointment_id": appointment_id, "technician_id": technician_id},
    )


_CANCELLABLE_PENDING_KINDS: tuple[EventKind, ...] = (
    EventKind.COMPANY_NO_SHOW,
    EventKind.OFF_HOURS_ACCRUAL_MARK,
)


def _cancel_pending_event(
    events: Events, conn: sqlite3.Connection, *, kind: EventKind, appointment_id: int
) -> None:
    row = conn.execute(
        "SELECT event_id FROM _events WHERE kind = ? AND status = 'pending' "
        "AND entity_type = ? AND entity_id = ?",
        (kind.value, EntityType.APPOINTMENT.value, appointment_id),
    ).fetchone()
    if row is not None:
        events.cancel(int(row["event_id"]))


def cancel_appointment(
    ctx: TxnContext,
    config: RunConfig,
    *,
    appointment_id: int,
    effective_at: int,
    artifact: GeoArtifact | None = None,
) -> dict:
    from . import execution

    conn = ctx.conn

    appt = conn.execute(
        "SELECT job_id, status FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if appt is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND, f"appointment {appointment_id} not found"
        )
    if appt["status"] not in ("scheduled", "dispatched"):
        raise _state_illegal(
            "appointment_not_active",
            f"appointment {appointment_id} is {appt['status']} and cannot be cancelled",
            appointment_id=appointment_id,
        )
    job_id = int(appt["job_id"])
    service_date = execution._appointment_service_date(conn, appointment_id)
    blocks = execution._appointment_blocks(conn, appointment_id)
    released_blocks = [
        {
            "start": clock.to_iso8601(int(block["block_start"])),
            "end": clock.to_iso8601(int(block["block_end"])),
        }
        for block in blocks
    ]

    updated = conn.execute(
        "UPDATE _appointments SET status = 'cancelled', cancelled_at = ?, modified_at = ? "
        "WHERE id = ? AND status IN ('scheduled', 'dispatched')",
        (effective_at, effective_at, appointment_id),
    )
    if updated.rowcount != 1:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"appointment {appointment_id} was not active at cancellation",
        )

    conn.execute(
        "UPDATE _jobs SET status = 'cancelled', outcome = 'cancelled_by_customer', "
        "modified_at = ? WHERE id = ?",
        (effective_at, job_id),
    )

    technician_id = execution._active_technician_id(conn, appointment_id)
    assignment_updated = conn.execute(
        "UPDATE _appointment_assignments SET status = 'cancelled', unassigned_at = ? "
        "WHERE appointment_id = ? AND status = 'active'",
        (effective_at, appointment_id),
    )
    if assignment_updated.rowcount != 1:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"appointment {appointment_id} had no active assignment at cancellation",
        )

    conn.execute(
        "DELETE FROM _technician_block_holds WHERE appointment_id = ?", (appointment_id,)
    )

    execution._write_appointment_event(
        conn, appointment_id=appointment_id, technician_id=technician_id,
        event_type="cancelled", occurred_at=effective_at, service_date=service_date,
    )

    events = Events(conn, ctx.rng)
    for kind in _CANCELLABLE_PENDING_KINDS:
        _cancel_pending_event(events, conn, kind=kind, appointment_id=appointment_id)

    if execution.first_trip_dispatch_applied(conn, service_date):
        execution.handle_outgoing_technician(
            ctx, config, artifact,
            appointment_id=appointment_id, technician_id=technician_id,
            service_date=service_date,
        )
        conn.execute(
            "UPDATE _day_state SET unresolved_execution_count = unresolved_execution_count - 1 "
            "WHERE service_date = ?",
            (service_date,),
        )

    return {
        "appointment_id": appointment_id,
        "job_id": job_id,
        "released_blocks": released_blocks,
    }


def acknowledge_cancellation(
    ctx: TxnContext,
    config: RunConfig,
    *,
    call_id: int,
    artifact: GeoArtifact | None = None,
) -> dict:
    surface.require_id("call_id", call_id)

    call = _call_row(ctx.conn, call_id)
    calls.require_current_cancellation_call(ctx, call)

    appointment_id = calls._required_int(
        call, "target_appointment_id", f"cancellation call {call_id}"
    )
    now = ctx.now()
    result = cancel_appointment(
        ctx, config, appointment_id=appointment_id, effective_at=now, artifact=artifact,
    )

    ctx.conn.execute(
        "UPDATE _calls SET status = 'acknowledged', terminal_reason = 'cancellation_acknowledged', "
        "terminal_at = ? WHERE id = ?",
        (now, call_id),
    )
    calls.record_resolved_attempt(ctx, int(call_id))

    appointment_number = str(
        ctx.conn.execute(
            "SELECT appointment_number FROM _appointments WHERE id = ?", (appointment_id,)
        ).fetchone()["appointment_number"]
    )
    service_date = clock.service_date_for_ms(now)
    ctx.conn.execute(
        "INSERT INTO _notifications (created_at, service_date, notification_type, "
        "entity_type, entity_id, message) "
        "VALUES (?, ?, 'customer_cancellation_acknowledged', 'appointment', ?, ?)",
        (
            now, service_date, appointment_id,
            f"Customer cancellation for appointment {appointment_number} was acknowledged "
            f"and its blocks were released.",
        ),
    )

    return {
        "call_id": call_id,
        "outcome": "acknowledged",
        "appointment_id": appointment_id,
        "released_blocks": result["released_blocks"],
        "message": "Cancellation acknowledged. The appointment is cancelled and its blocks "
        "are released.",
    }


def acknowledge_cancellation_command(
    services: Services,
    request_id: str,
    config: RunConfig,
    *,
    call_id: int,
    artifact: GeoArtifact | None = None,
) -> bytes:
    return command(
        services,
        request_id,
        lambda ctx: acknowledge_cancellation(ctx, config, call_id=call_id, artifact=artifact),
        operation="appointments.acknowledge_cancellation",
        args={"call_id": call_id},
    )


__all__ = [
    "BLOCK_CODES",
    "BookingPlan",
    "acknowledge_cancellation",
    "acknowledge_cancellation_command",
    "assign",
    "assign_command",
    "book",
    "cancel_appointment",
    "create_booking",
    "trip_index_for_appointment",
    "validate_booking",
    "validate_reservation_shape",
]
