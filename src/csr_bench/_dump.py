"""Canonical logical dump of a run database (docs/CSR-001.md decision 6).

Determinism is defined over the **logical** record — the ordered rows of every
core table — never over the bytes of the SQLite file. Page layout, free-list
reuse, and vacuum state are not part of the contract, so this module renders each
table as rows in a deterministic order, serializes them canonically (sorted keys,
ASCII-escaped, no floats), and hashes the result with SHA-256.

Placement: this lives in the package rather than in ``tests/`` because spec 09's
DET suite (DET-01..DET-03, DET-11) must compare dumps of real runs from outside
this repository's test tree, and one shared implementation is what makes
"identical dump" mean the same thing in CSR-001 and later tickets. It is private
(underscore-prefixed): no public surface and no stability promise — bumping
:data:`DUMP_VERSION` is how a format change is declared.

Two normalizations keep the dump comparable across processes:

- the run's own identifier is replaced by :data:`RUN_ID_PLACEHOLDER` wherever it
  appears, because a UUID run id is process metadata, not part of the logical
  record (spec 09 §2 excludes wall-clock metadata, process ids, and file paths);
- ``BLOB`` values are rendered as ``hex:<lowercase hex>`` so stored response
  bytes compare exactly without depending on a text decoding.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from .errors import EngineError, ErrorCode

#: Bump when the dump's shape changes; a hash from a different version is not
#: comparable and the marker makes that visible instead of silent.
DUMP_VERSION = 1

RUN_ID_PLACEHOLDER = "<run_id>"


@dataclass(frozen=True)
class _Table:
    name: str
    #: A total order over the table's rows. Every core table has one: the
    #: AUTOINCREMENT surrogate keys are assigned in insertion order inside the
    #: run, and the keyless tables order by their primary key.
    order_by: str
    run_scoped: bool = True


#: Every table :func:`csr_bench.db.init_schema` creates. A table missing from
#: this tuple is a determinism hole, so :func:`dump` refuses a database that has
#: one (see :func:`_require_registered_tables`).
CORE_TABLES: tuple[_Table, ...] = (
    _Table("run", "run_id"),
    _Table("_events", "event_id"),
    _Table("_ledger", "seq"),
    _Table("_invocations", "invocation_id"),
    _Table("_command_responses", "namespace, request_id"),
    _Table("_rng_state", "stream_name"),
    _Table("_rng_draw_audit", "seq"),
    _Table("_schema_meta", "key", False),
)


def _canon_value(value: object, run_id: str) -> object:
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        # No float may reach storage on any money or probability path
        # (docs/CSR-001.md decision 1/4); a REAL column would silently break
        # cross-platform reproducibility, so refuse rather than round.
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, f"float value in run database: {value!r}")
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
    """The run's logical record: every core table, rows in deterministic order."""
    _require_registered_tables(conn)
    tables: dict[str, list[dict[str, object]]] = {}
    for table in CORE_TABLES:
        sql = f"SELECT * FROM {table.name}"  # names are module constants
        params: tuple[object, ...] = ()
        if table.run_scoped:
            sql += " WHERE run_id = ?"
            params = (run_id,)
        sql += f" ORDER BY {table.order_by}"
        tables[table.name] = [
            {key: _canon_value(row[key], run_id) for key in row.keys()}
            for row in conn.execute(sql, params).fetchall()
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
