from __future__ import annotations

import sqlite3

from . import clock, demand, surface
from .config import RunConfig
from .enums import EntityType, EventKind, KeyedStreamName
from .errors import EngineError, ErrorCode
from .txn import Services, TxnContext, command

_ORDINARY_REJECTION_STATUS_BY_REASON = {
    "agent_rejected": "rejected",
    "customer_declined": "declined",
    "unanswered": "unanswered",
}


def arrive(conn: sqlite3.Connection, event_row: sqlite3.Row, *, now_ms: int) -> int | dict:
    entity_type = event_row["entity_type"]
    if entity_type == EntityType.PENDING_CALL.value:
        return _pending_call_arrival(conn, event_row, now_ms=now_ms)
    if entity_type == EntityType.APPOINTMENT.value:
        return _cancellation_arrival(conn, event_row, now_ms=now_ms)
    raise EngineError(
        ErrorCode.INVARIANT_VIOLATED,
        f"call invocation event {event_row['event_id']} has unknown entity_type "
        f"{entity_type!r}",
    )


def _pending_call_arrival(conn: sqlite3.Connection, event_row: sqlite3.Row, *, now_ms: int) -> int:
    pending = conn.execute(
        "SELECT * FROM _pending_calls WHERE id = ?", (event_row["entity_id"],)
    ).fetchone()
    if pending is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND,
            f"call event {event_row['event_id']} references missing pending call "
            f"{event_row['entity_id']}",
        )
    service_name = _category_name(conn, int(pending["requested_category_id"]))
    street, unit, city, state, postal_code, address_catalog_id = _service_address(conn, pending)
    segment = pending["segment"] if pending["customer_id"] is None else None
    cur = conn.execute(
        "INSERT INTO _calls (arrived_at, service_date, call_kind, status, caller_name, "
        "caller_phone, segment, customer_id, location_id, service_street, service_unit, "
        "service_city, service_state, service_postal_code, requested_service_name, urgent, "
        "related_job_id, address_catalog_id, created_at) "
        "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now_ms,
            pending["service_date"],
            pending["call_kind"],
            pending["caller_name"],
            pending["caller_phone"],
            segment,
            pending["customer_id"],
            pending["location_id"],
            street,
            unit,
            city,
            state,
            postal_code,
            service_name,
            int(pending["urgent"]),
            pending["related_job_id"],
            address_catalog_id,
            now_ms,
        ),
    )
    call_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO _call_hidden (call_id, true_segment, requested_category_id, "
        "prospect_id, arrival_event_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            call_id,
            pending["segment"],
            pending["requested_category_id"],
            pending["prospect_id"],
            int(event_row["event_id"]),
            now_ms,
        ),
    )
    conn.execute("DELETE FROM _pending_calls WHERE id = ?", (pending["id"],))
    return call_id


_ALIVE_APPOINTMENT_STATUSES = ("scheduled", "dispatched")


def _cancellation_call_hidden_category_id(conn: sqlite3.Connection, job_id: int) -> int:
    row = conn.execute(
        "SELECT h.requested_category_id FROM _call_hidden h "
        "JOIN _jobs j ON j.call_id = h.call_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"job {job_id} has no originating call's hidden requested category",
        )
    return int(row["requested_category_id"])


def _cancellation_arrival(conn: sqlite3.Connection, event_row: sqlite3.Row, *, now_ms: int) -> int | dict:
    appointment_id = int(event_row["entity_id"])
    appt = conn.execute(
        "SELECT status FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if appt is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND,
            f"cancellation arrival event {event_row['event_id']} references missing "
            f"appointment {appointment_id}",
        )
    if appt["status"] not in _ALIVE_APPOINTMENT_STATUSES:
        return {"event_disposition": "cancelled"}

    job = conn.execute(
        "SELECT j.id AS job_id, j.customer_id, j.location_id, j.requested_service_name "
        "FROM _appointments a JOIN _jobs j ON j.id = a.job_id WHERE a.id = ?",
        (appointment_id,),
    ).fetchone()
    if job is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND, f"appointment {appointment_id} has no job"
        )
    customer_id = int(job["customer_id"])
    location_id = int(job["location_id"])

    customer = conn.execute(
        "SELECT segment FROM _customers WHERE id = ?", (customer_id,)
    ).fetchone()
    if customer is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"customer {customer_id} not found")

    location = conn.execute(
        "SELECT address_catalog_id FROM _locations WHERE id = ?", (location_id,)
    ).fetchone()
    if location is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"location {location_id} not found")
    address_catalog_id = location["address_catalog_id"]
    if address_catalog_id is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED, f"location {location_id} has no catalog address"
        )

    caller_name = demand._customer_caller_name(conn, customer_id)
    caller_phone = demand._customer_caller_phone(conn, customer_id)
    service_date = clock.service_date_for_ms(now_ms)

    cur = conn.execute(
        "INSERT INTO _calls (arrived_at, service_date, call_kind, status, caller_name, "
        "caller_phone, segment, customer_id, location_id, service_street, service_unit, "
        "service_city, service_state, service_postal_code, requested_service_name, urgent, "
        "related_job_id, target_appointment_id, target_job_id, address_catalog_id, created_at) "
        "VALUES (?, ?, 'cancellation', 'active', ?, ?, NULL, ?, ?, NULL, NULL, NULL, NULL, "
        "NULL, ?, 0, NULL, ?, ?, ?, ?)",
        (
            now_ms,
            service_date,
            caller_name,
            caller_phone,
            customer_id,
            location_id,
            job["requested_service_name"],
            appointment_id,
            int(job["job_id"]),
            int(address_catalog_id),
            now_ms,
        ),
    )
    call_id = int(cur.lastrowid)
    requested_category_id = _cancellation_call_hidden_category_id(conn, int(job["job_id"]))
    conn.execute(
        "INSERT INTO _call_hidden (call_id, true_segment, requested_category_id, "
        "prospect_id, arrival_event_id, created_at) VALUES (?, ?, ?, NULL, ?, ?)",
        (call_id, customer["segment"], requested_category_id, int(event_row["event_id"]), now_ms),
    )
    return call_id


def prompt_payload(conn: sqlite3.Connection, event_row: sqlite3.Row, ts_ms: int) -> dict:
    call = conn.execute(
        "SELECT c.* FROM _calls c JOIN _call_hidden h ON h.call_id = c.id "
        "WHERE h.arrival_event_id = ?",
        (event_row["event_id"],),
    ).fetchone()
    if call is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND,
            f"no arrived call recorded for event {event_row['event_id']}",
        )
    payload: dict[str, object] = {
        "time": clock.to_iso8601(ts_ms),
        "call_id": int(call["id"]),
        "caller": str(call["caller_name"]),
    }
    context = f"cancellation call {call['id']}"
    if call["call_kind"] == "cancellation":
        appointment_id = _required_int(call, "target_appointment_id", context)
        payload["customer_id"] = _required_int(call, "customer_id", context)
        payload["location_id"] = _required_int(call, "location_id", context)
        payload["appointment_id"] = appointment_id
        scheduled_start_ms, scheduled_end_ms = _appointment_window_ms(conn, appointment_id)
        payload["scheduled"] = clock.to_iso8601(scheduled_start_ms)
        payload["scheduled_end"] = clock.to_iso8601(scheduled_end_ms)
        return payload
    payload["phone"] = str(call["caller_phone"])
    payload["address"] = _formatted_address(conn, call)
    if call["customer_id"] is None:
        payload["customer_type"] = str(call["segment"])
    else:
        payload["customer_id"] = int(call["customer_id"])
        if call["location_id"] is not None:
            payload["location_id"] = int(call["location_id"])
    if call["related_job_id"] is not None:
        related_job_id = int(call["related_job_id"])
        payload["related_job_id"] = related_job_id
        payload["related_job_completed_date"] = _job_completed_date(conn, related_job_id)
    payload["requested_service"] = str(call["requested_service_name"])
    payload["urgent"] = bool(call["urgent"])
    return payload


def _category_name(conn: sqlite3.Connection, category_id: int) -> str:
    row = conn.execute(
        "SELECT name FROM _service_catalog WHERE id = ?", (category_id,)
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND, f"service catalog category {category_id} not found"
        )
    return str(row["name"])


def _service_address(
    conn: sqlite3.Connection, pending: sqlite3.Row
) -> tuple[str, str | None, str, str, str, int]:
    if pending["location_id"] is not None:
        row = conn.execute(
            "SELECT street, unit, city, state, postal_code, address_catalog_id "
            "FROM _locations WHERE id = ?",
            (pending["location_id"],),
        ).fetchone()
        if row is None:
            raise EngineError(
                ErrorCode.ENTITY_NOT_FOUND, f"location {pending['location_id']} not found"
            )
        catalog_id = row["address_catalog_id"]
        if catalog_id is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"location {pending['location_id']} has no catalog address",
            )
        return (
            str(row["street"]),
            str(row["unit"]),
            str(row["city"]),
            str(row["state"]),
            str(row["postal_code"]),
            int(catalog_id),
        )
    if pending["address_catalog_id"] is not None:
        row = conn.execute(
            "SELECT id, house_number, street, city, state, postal_code "
            "FROM _address_catalog WHERE id = ?",
            (pending["address_catalog_id"],),
        ).fetchone()
        if row is None:
            raise EngineError(
                ErrorCode.ENTITY_NOT_FOUND,
                f"catalog address {pending['address_catalog_id']} not found",
            )
        return (
            f"{row['house_number']} {row['street']}",
            None,
            str(row["city"]),
            str(row["state"]),
            str(row["postal_code"]),
            int(row["id"]),
        )
    raise EngineError(
        ErrorCode.INVARIANT_VIOLATED,
        f"pending call {pending['id']} has neither a location nor a catalog address",
    )


def _formatted_address(conn: sqlite3.Connection, call: sqlite3.Row) -> str:
    catalog_id = _required_int(call, "address_catalog_id", f"call {call['id']}")
    row = conn.execute(
        "SELECT formatted_address FROM _address_catalog WHERE id = ?", (catalog_id,)
    ).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"catalog address {catalog_id} not found")
    return str(row["formatted_address"])


def _appointment_window_ms(conn: sqlite3.Connection, appointment_id: int) -> tuple[int, int]:
    row = conn.execute(
        "SELECT scheduled_start, scheduled_end FROM _appointments WHERE id = ?",
        (appointment_id,),
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND, f"appointment {appointment_id} not found"
        )
    return int(row["scheduled_start"]), int(row["scheduled_end"])


def _job_completed_date(conn: sqlite3.Connection, job_id: int) -> str:
    row = conn.execute("SELECT completed_at FROM _jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"job {job_id} not found")
    if row["completed_at"] is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED, f"callback references incomplete job {job_id}"
        )
    return clock.service_date_for_ms(int(row["completed_at"]))


def _required_int(row: sqlite3.Row, column: str, context: str) -> int:
    value = row[column]
    if value is None:
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, f"{context} is missing {column}")
    return int(value)


def _state_illegal(reason: str, message: str, **details: object) -> EngineError:
    return EngineError(ErrorCode.STATE_ILLEGAL, message, details={"reason": reason, **details})


def _call_row(conn: sqlite3.Connection, call_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM _calls WHERE id = ?", (call_id,)).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"call {call_id} not found")
    return row


def current_active_call_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT h.call_id FROM _events e "
        "JOIN _call_hidden h ON h.arrival_event_id = e.event_id "
        "WHERE e.status = 'applied' AND e.kind = ? "
        "ORDER BY e.applied_at_ms DESC, e.event_id DESC LIMIT 1",
        (EventKind.CALL_INVOCATION.value,),
    ).fetchone()
    return int(row["call_id"]) if row is not None else None


def require_current_new_job_call(ctx: TxnContext, call: sqlite3.Row) -> None:
    call_id = int(call["id"])
    if ctx.invocation is not None and ctx.invocation["type"] != "call":
        raise _state_illegal(
            "call_not_active",
            f"call {call_id} is reachable only inside an active call invocation",
        )
    if call["status"] != "active":
        raise _state_illegal(
            "call_not_active", f"call {call_id} is not active (status {call['status']})"
        )
    if call["call_kind"] != "new_job":
        raise _state_illegal("cancellation_call", f"call {call_id} is a cancellation call")
    if current_active_call_id(ctx.conn) != call_id:
        raise _state_illegal(
            "call_not_active", f"call {call_id} is not the currently active call"
        )


def require_current_cancellation_call(ctx: TxnContext, call: sqlite3.Row) -> None:
    call_id = int(call["id"])
    if ctx.invocation is not None and ctx.invocation["type"] != "call":
        raise _state_illegal(
            "call_not_active",
            f"call {call_id} is reachable only inside an active call invocation",
        )
    if call["status"] != "active":
        raise _state_illegal(
            "call_not_active", f"call {call_id} is not active (status {call['status']})"
        )
    if call["call_kind"] != "cancellation":
        raise _state_illegal("new_job_call", f"call {call_id} is a new-job call")
    if current_active_call_id(ctx.conn) != call_id:
        raise _state_illegal(
            "call_not_active", f"call {call_id} is not the currently active call"
        )


def ordinary_rejection(
    ctx: TxnContext, config: RunConfig, call: sqlite3.Row, *, terminal_reason: str
) -> None:
    status = _ORDINARY_REJECTION_STATUS_BY_REASON.get(terminal_reason)
    if status is None:
        raise EngineError(
            ErrorCode.INVALID_ARGUMENT,
            "terminal_reason must be one of "
            f"{sorted(_ORDINARY_REJECTION_STATUS_BY_REASON)}, got {terminal_reason!r}",
        )

    call_id = int(call["id"])
    if call["call_kind"] != "new_job":
        raise _state_illegal("cancellation_call", f"call {call_id} is a cancellation call")
    if call["status"] != "active":
        raise _state_illegal(
            "call_not_active", f"call {call_id} is not active (status {call['status']})"
        )

    conn = ctx.conn
    now = ctx.now()

    conn.execute(
        "UPDATE _calls SET status = ?, terminal_reason = ?, terminal_at = ? WHERE id = ?",
        (status, terminal_reason, now, call_id),
    )

    customer_id = call["customer_id"]
    prospect_id: int | None = None

    hidden = conn.execute(
        "SELECT true_segment, prospect_id FROM _call_hidden WHERE call_id = ?", (call_id,)
    ).fetchone()
    if hidden is None:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED, f"call {call_id} has no _call_hidden row"
        )

    if customer_id is None and hidden["prospect_id"] is not None:
        registered = conn.execute(
            "SELECT registered_customer_id FROM _prospect_state WHERE id = ?",
            (hidden["prospect_id"],),
        ).fetchone()
        if registered is not None and registered["registered_customer_id"] is not None:
            customer_id = registered["registered_customer_id"]

    if customer_id is not None:
        demand.apply_rejection_consequence(conn, config, now_ms=now, customer_id=customer_id)
    else:
        if call["address_catalog_id"] is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED, f"call {call_id} has no address_catalog_id"
            )
        catalog = conn.execute(
            "SELECT effective_zone_id FROM _address_catalog WHERE id = ?",
            (call["address_catalog_id"],),
        ).fetchone()
        if catalog is None:
            raise EngineError(
                ErrorCode.ENTITY_NOT_FOUND,
                f"catalog address {call['address_catalog_id']} not found",
            )
        prospect = {
            "caller_name": call["caller_name"],
            "caller_phone": call["caller_phone"],
            "segment": hidden["true_segment"],
            "home_zone_id": int(catalog["effective_zone_id"]),
        }
        prospect_id = demand.apply_rejection_consequence(
            conn, config, now_ms=now, prospect=prospect
        )

    attempt = ctx.rng.chance(
        KeyedStreamName.COMPETITOR_LEAK.value, "call", call_id, "", "competitor_attempt",
        config.get_prob_threshold("competitors.attempt_probability"), draw_index=0,
    )
    if attempt:
        served = ctx.rng.chance(
            KeyedStreamName.COMPETITOR_LEAK.value, "call", call_id, "", "competitor_served_well",
            config.get_prob_threshold("competitors.served_well_probability"), draw_index=1,
        )
        if served:
            if customer_id is not None:
                demand.apply_competitor_capture(conn, config, now_ms=now, customer_id=customer_id)
            else:
                demand.apply_competitor_capture(conn, config, now_ms=now, prospect_id=prospect_id)


def record_resolved_attempt(ctx: TxnContext, call_id: int) -> None:
    row = ctx.conn.execute(
        "SELECT COUNT(*) c FROM _call_attempts WHERE call_id = ? AND outcome = 'incomplete'",
        (call_id,),
    ).fetchone()
    incomplete_count = int(row["c"])
    if incomplete_count == 0:
        return
    ctx.conn.execute(
        "INSERT INTO _call_attempts (call_id, attempt_number, prompted_at, outcome, created_at) "
        "VALUES (?, ?, ?, 'resolved', ?)",
        (call_id, incomplete_count + 1, ctx.now(), ctx.now()),
    )


def reject(ctx: TxnContext, config: RunConfig, *, call_id: int) -> dict:
    surface.require_id("call_id", call_id)

    call = _call_row(ctx.conn, call_id)
    require_current_new_job_call(ctx, call)
    record_resolved_attempt(ctx, int(call["id"]))
    ordinary_rejection(ctx, config, call, terminal_reason="agent_rejected")

    return {"call_id": call_id, "outcome": "rejected", "message": "Call rejected."}


def reject_command(
    services: Services, request_id: str, config: RunConfig, *, call_id: int
) -> bytes:
    return command(
        services,
        request_id,
        lambda ctx: reject(ctx, config, call_id=call_id),
        operation="calls.reject",
        args={"call_id": call_id},
    )


__all__ = [
    "arrive",
    "current_active_call_id",
    "ordinary_rejection",
    "prompt_payload",
    "record_resolved_attempt",
    "reject",
    "reject_command",
    "require_current_cancellation_call",
    "require_current_new_job_call",
]
