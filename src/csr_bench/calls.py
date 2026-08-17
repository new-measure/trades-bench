from __future__ import annotations

import sqlite3

from . import clock, demand, surface
from .config import RunConfig
from .enums import EventKind, KeyedStreamName
from .errors import EngineError, ErrorCode
from .txn import Services, TxnContext, command

_ORDINARY_REJECTION_STATUS_BY_REASON = {
    "agent_rejected": "rejected",
    "customer_declined": "declined",
    "unanswered": "unanswered",
}


def arrive(conn: sqlite3.Connection, event_row: sqlite3.Row, *, now_ms: int) -> int:
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
        payload["scheduled"] = clock.to_iso8601(_appointment_start_ms(conn, appointment_id))
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


def _appointment_start_ms(conn: sqlite3.Connection, appointment_id: int) -> int:
    row = conn.execute(
        "SELECT scheduled_start FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if row is None:
        raise EngineError(
            ErrorCode.ENTITY_NOT_FOUND, f"appointment {appointment_id} not found"
        )
    return int(row["scheduled_start"])


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
    if customer_id is not None:
        demand.apply_rejection_consequence(conn, config, now_ms=now, customer_id=customer_id)
    else:
        hidden = conn.execute(
            "SELECT true_segment FROM _call_hidden WHERE call_id = ?", (call_id,)
        ).fetchone()
        if hidden is None:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED, f"call {call_id} has no _call_hidden row"
            )
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


def reject(ctx: TxnContext, config: RunConfig, *, call_id: int) -> dict:
    surface.require_id("call_id", call_id)

    call = _call_row(ctx.conn, call_id)
    require_current_new_job_call(ctx, call)
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
    "reject",
    "reject_command",
    "require_current_new_job_call",
]
