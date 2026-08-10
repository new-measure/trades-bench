"""Canonical logical dump of a run database (docs/CSR-001.md decision 6).

Determinism is defined over the **logical** record — the ordered rows of every
core table — never over the bytes of the SQLite file. Page layout, free-list
reuse, and vacuum state are not part of the contract, so this module renders each
  table as rows in a deterministic order, serializes them canonically (sorted keys,
  ASCII-escaped, floats as exact bit hex), and hashes the result with SHA-256.

Placement: this lives in the package rather than in ``tests/`` because spec 09's
DET suite (DET-01..DET-03, DET-11) must compare dumps of real runs from outside
this repository's test tree, and one shared implementation is what makes
"identical dump" mean the same thing in CSR-001 and later tickets. It is private
(underscore-prefixed): no public surface and no stability promise — bumping
:data:`DUMP_VERSION` is how a format change is declared.

CSR-002 (decisions 2, 4): the core-table set is the migration-0001 schema, not
the CSR-001 precursor tables. ``run``/``_ledger``/``_command_responses``/
``_rng_draw_audit``/``_schema_meta`` became ``_run``/``_ledger_entries``/
``_idempotency``/``_random_audit``/``_schema_migrations``, no table other than
``_run`` and ``_config`` carries ``run_id``, and the seed rows of the reference
catalogs (``_business_units`` … ``_zones``) and the migration record
(``_schema_migrations``, ``applied_at`` = 0 by design) are committed rows like
any other.

One normalization keeps the dump comparable across processes:

- the run's own identifier is replaced by :data:`RUN_ID_PLACEHOLDER` wherever it
  appears (``_run.run_id``, ``_config.run_id``), because a UUID run id is process
  metadata, not part of the logical record (spec 09 §2 excludes wall-clock
  metadata, process ids, and file paths);
- ``BLOB`` values are rendered as ``hex:<lowercase hex>`` so stored response
  bytes compare exactly without depending on a text decoding;
- ``REAL`` values (migration 0001: geometry and coordinates) are rendered as
  ``float:<lowercase IEEE-754 bit hex>`` via ``struct.pack('>d')`` — bit-exact,
  cross-platform, independent of float formatting. Floats never enter a
  money/probability *decision* path (decisions 1/4); this is serialization only,
  for stored values that migration 0001 mandates as REAL columns.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from dataclasses import dataclass

from .errors import EngineError, ErrorCode

#: Bump when the dump's shape changes; a hash from a different version is not
#: comparable and the marker makes that visible instead of silent. Bumped for
#: the CSR-002 schema sweep (every table, no run-scoping).
DUMP_VERSION = 2

RUN_ID_PLACEHOLDER = "<run_id>"


@dataclass(frozen=True)
class _Table:
    name: str
    #: A total order over the table's rows. Every migration-0001 table has a
    #: declared primary key: the ``id``/``event_id``/``invocation_id``/``seq``
    #: INTEGER PRIMARY KEYs are assigned in insertion order inside the run, and
    #: the keyed tables order by their composite primary key. Quoted
    #: identifiers where the PK column is a SQLite reserved word
    #: (``_visibility_registry``: ``"table"``, ``"column"``).
    order_by: str


#: Every table migration 0001 creates. A table missing from this tuple is a
#: determinism hole, so :func:`dump` refuses a database that has one (see
#: :func:`_require_registered_tables`). When a future migration adds a table,
#: this tuple must grow with it (the golden-schema tests enforce the same
#: discipline for the DDL itself).
CORE_TABLES: tuple[_Table, ...] = (
    # -- core run-contract tables (spec 02 §5) -------------------------------
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
    # -- reference and catalog data (spec 02 §2) -----------------------------
    _Table("_business_units", "id"),
    _Table("_skills", "id"),
    _Table("_job_types", "id"),
    _Table("_zones", "id"),
    _Table("_zone_geometry", "zone_id"),
    _Table("_service_catalog", "id"),
    _Table("_address_catalog", "id"),
    _Table("_dataset_versions", "id"),
    # -- domain tables (spec 02 §4) ------------------------------------------
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
        # Migration 0001 mandates REAL columns (geometry, coordinates) — but a
        # float never enters a money/probability decision path (decisions 1/4),
        # so this is pure serialization: render the exact IEEE-754 bit pattern
        # (big-endian) so round-trips are exact and no float-to-string
        # formatting can differ across platforms.
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
    """The run's logical record: every core table, rows in deterministic order.

    ``run_id`` is the normalization key for the placeholder substitution only —
    no table is filtered by it (CSR-002 decision 4: one run per database, so
    every row is this run's).
    """
    _require_registered_tables(conn)
    tables: dict[str, list[dict[str, object]]] = {}
    for table in CORE_TABLES:
        sql = f"SELECT * FROM {table.name} ORDER BY {table.order_by}"  # module constants
        tables[table.name] = [
            {key: _canon_value(row[key], run_id) for key in row.keys()}
            for row in conn.execute(sql).fetchall()
        ]
    return {"dump_version": DUMP_VERSION, "tables": tables}


def dump_bytes(conn: sqlite3.Connection, run_id: str) -> bytes:
    """:func:`dump` as canonical JSON — sorted keys, no whitespace, ASCII only."""
    return json.dumps(
        dump(conn, run_id),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def dump_hash(conn: sqlite3.Connection, run_id: str) -> str:
    """SHA-256 of :func:`dump_bytes` — the comparison token for DET-01/DET-02."""
    return hashlib.sha256(dump_bytes(conn, run_id)).hexdigest()


__all__ = [
    "CORE_TABLES",
    "DUMP_VERSION",
    "RUN_ID_PLACEHOLDER",
    "dump",
    "dump_bytes",
    "dump_hash",
]
