from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from dataclasses import dataclass

from .errors import EngineError, ErrorCode

DUMP_VERSION = 2

RUN_ID_PLACEHOLDER = "<run_id>"


@dataclass(frozen=True)
class _Table:
    name: str
    order_by: str


CORE_TABLES: tuple[_Table, ...] = (
    _Table("_run", "run_id"),
    _Table("_config", "run_id"),
    _Table("_events", "event_id"),
    _Table("_idempotency", "namespace, request_id"),
    _Table("_invocations", "invocation_id"),
    _Table("_rng_state", "stream_name"),
    _Table("_random_audit", "seq"),
    _Table("_clock", "singleton"),
    _Table("_visibility_registry", '"table", "column"'),
    _Table("_schema_migrations", "version"),
    _Table("_business_units", "id"),
    _Table("_skills", "id"),
    _Table("_job_types", "id"),
    _Table("_zones", "id"),
    _Table("_zone_geometry", "zone_id"),
    _Table("_service_catalog", "id"),
    _Table("_address_catalog", "id"),
    _Table("_dataset_versions", "id"),
    _Table("_customers", "id"),
    _Table("_customer_contacts", "id"),
    _Table("_locations", "id"),
    _Table("_location_contacts", "id"),
    _Table("_calls", "id"),
    _Table("_call_attempts", "id"),
    _Table("_jobs", "id"),
    _Table("_appointments", "id"),
    _Table("_appointment_blocks", "id"),
    _Table("_appointment_assignments", "id"),
    _Table("_appointment_events", "id"),
    _Table("_technician_block_holds", "technician_id, block_start"),
    _Table("_technicians", "id"),
    _Table("_technician_skills", "id"),
    _Table("_technician_shifts", "id"),
    _Table("_technician_locations", "technician_id"),
    _Table("_technician_movements", "id"),
    _Table("_technician_absences", "id"),
    _Table("_non_job_appointments", "id"),
    _Table("_on_call_rotations", "id"),
    _Table("_technician_true_skill", "technician_id, skill_code"),
    _Table("_technician_strain", "technician_id"),
    _Table("_hiring_requests", "id"),
    _Table("_training_rates", "id"),
    _Table("_training_reservations", "id"),
    _Table("_invoices", "id"),
    _Table("_invoice_items", "id"),
    _Table("_payments", "id"),
    _Table("_ledger_entries", "id"),
    _Table("_reviews", "id"),
    _Table("_notifications", "id"),
)


def _canon_value(value: object, run_id: str) -> object:
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return "float:" + struct.pack(">d", value).hex()
    if isinstance(value, (bytes, memoryview)):
        return "hex:" + bytes(value).hex()
    if isinstance(value, str):
        return RUN_ID_PLACEHOLDER if value == run_id else value
    raise EngineError(
        ErrorCode.INVARIANT_VIOLATED,
        f"uncanonicalizable value of type {type(value).__name__} in run database",
    )


def _require_registered_tables(conn: sqlite3.Connection) -> None:
    present = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    unknown = sorted(present - {t.name for t in CORE_TABLES})
    if unknown:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            f"canonical dump does not cover table(s) {unknown}; register them in CORE_TABLES",
        )


def dump(conn: sqlite3.Connection, run_id: str) -> dict[str, object]:
    _require_registered_tables(conn)
    tables: dict[str, list[dict[str, object]]] = {}
    for table in CORE_TABLES:
        sql = f"SELECT * FROM {table.name} ORDER BY {table.order_by}"
        tables[table.name] = [
            {key: _canon_value(row[key], run_id) for key in row.keys()}
            for row in conn.execute(sql).fetchall()
        ]
    return {"dump_version": DUMP_VERSION, "tables": tables}


def dump_bytes(conn: sqlite3.Connection, run_id: str) -> bytes:
    return json.dumps(
        dump(conn, run_id),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def dump_hash(conn: sqlite3.Connection, run_id: str) -> str:
    return hashlib.sha256(dump_bytes(conn, run_id)).hexdigest()


__all__ = [
    "CORE_TABLES",
    "DUMP_VERSION",
    "RUN_ID_PLACEHOLDER",
    "dump",
    "dump_bytes",
    "dump_hash",
]
