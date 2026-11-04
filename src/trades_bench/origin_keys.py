from __future__ import annotations

import sqlite3

from .errors import EngineError, ErrorCode

ENTITY_ID = 1


def new_caller(local_date: str, zone_id: int, slot_index: int) -> str:
    return f"n:{local_date}:{zone_id}:{slot_index}"


def repeat_call(local_date: str, slot_index: int, customer_origin: str) -> str:
    return f"r:{local_date}:{slot_index}:{customer_origin}"


def cancellation_call(appointment_origin: str) -> str:
    return f"x:{appointment_origin}"


def warm_start(table: str, record_index: int) -> str:
    return f"w:{table}:{record_index}"


def scoped(origin: str, local_date: str) -> str:
    return f"{origin}|{local_date}"


def _required(row: sqlite3.Row | None, subject: str) -> str:
    if row is None or row["origin_key"] is None:
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, f"{subject} has no origin key")
    return str(row["origin_key"])


def of_call(conn: sqlite3.Connection, call_id: int) -> str:
    row = conn.execute(
        "SELECT origin_key FROM _call_hidden WHERE call_id = ?", (call_id,)
    ).fetchone()
    return _required(row, f"call {call_id}")


def of_job(conn: sqlite3.Connection, job_id: int) -> str:
    row = conn.execute(
        "SELECT h.origin_key FROM _jobs j JOIN _call_hidden h ON h.call_id = j.call_id "
        "WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    return _required(row, f"job {job_id}")


def of_appointment(conn: sqlite3.Connection, appointment_id: int) -> str:
    row = conn.execute(
        "SELECT h.origin_key FROM _appointments a JOIN _jobs j ON j.id = a.job_id "
        "JOIN _call_hidden h ON h.call_id = j.call_id WHERE a.id = ?",
        (appointment_id,),
    ).fetchone()
    return _required(row, f"appointment {appointment_id}")


def of_customer(conn: sqlite3.Connection, customer_id: int) -> str:
    row = conn.execute(
        "SELECT origin_key FROM _customer_repeat_state WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    return _required(row, f"customer {customer_id}")


__all__ = [
    "ENTITY_ID",
    "cancellation_call",
    "new_caller",
    "of_appointment",
    "of_call",
    "of_customer",
    "of_job",
    "repeat_call",
    "scoped",
    "warm_start",
]
