from __future__ import annotations

import sqlite3

from . import clock
from .errors import EngineError, ErrorCode


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


__all__ = ["arrive", "prompt_payload"]
