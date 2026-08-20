from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from . import visibility
from .enums import RunStatus
from .errors import EngineError, ErrorCode

_LEGAL_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.INITIALIZING: frozenset({RunStatus.ACTIVE, RunStatus.FAILED}),
    RunStatus.ACTIVE: frozenset({RunStatus.COMPLETED, RunStatus.BANKRUPT, RunStatus.FAILED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.BANKRUPT: frozenset(),
    RunStatus.FAILED: frozenset(),
}


_MIN_SQLITE_VERSION = (3, 35, 0)


def connect(path: str | Path) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < _MIN_SQLITE_VERSION:
        raise EngineError(
            ErrorCode.MIGRATION_FAILED,
            f"SQLite {'.'.join(map(str, _MIN_SQLITE_VERSION))} or newer is required "
            f"(found {'.'.join(map(str, sqlite3.sqlite_version_info))})",
        )
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    return conn


def in_memory() -> sqlite3.Connection:
    return connect(":memory:")


def begin(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")


def commit(conn: sqlite3.Connection) -> None:
    conn.execute("COMMIT")


def rollback(conn: sqlite3.Connection) -> None:
    conn.execute("ROLLBACK")


def new_run_id() -> str:
    return str(uuid4())


def insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    spec_version: str,
    config_version: str,
    config_hash: str,
    master_seed: int,
    start_at_ms: int,
    first_service_date: str,
    last_service_date: str,
    timezone: str,
    status: RunStatus = RunStatus.INITIALIZING,
    model_id: str | None = None,
    harness_name: str | None = None,
    harness_version: str | None = None,
    context_policy: str | None = None,
    attempt_definition: str | None = None,
    track: str | None = None,
    system_prompt_hash: str | None = None,
) -> None:
    if not conn.in_transaction:
        raise EngineError(
            ErrorCode.DRAW_OUTSIDE_TXN,
            "insert_run is callable only inside an open transaction "
            "(it seeds _run and _clock together)",
        )
    conn.execute(
        """
        INSERT INTO _run (
            run_id, spec_version, config_version, config_hash, master_seed,
            start_at_ms, first_service_date, last_service_date, timezone,
            status, model_id, harness_name, harness_version, context_policy,
            attempt_definition, track, system_prompt_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            spec_version,
            config_version,
            config_hash,
            master_seed,
            start_at_ms,
            first_service_date,
            last_service_date,
            timezone,
            str(status),
            model_id,
            harness_name,
            harness_version,
            context_policy,
            attempt_definition,
            track,
            system_prompt_hash,
        ),
    )
    conn.execute(
        "INSERT INTO _clock (singleton, current_time_ms, service_date) VALUES (1, ?, ?)",
        (start_at_ms, first_service_date),
    )


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM _run WHERE run_id = ?", (run_id,)).fetchone()


def set_run_status(conn: sqlite3.Connection, run_id: str, status: RunStatus) -> None:
    row = conn.execute("SELECT status FROM _run WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"run {run_id!r} not found")
    current = RunStatus(row["status"])
    if status not in _LEGAL_RUN_TRANSITIONS.get(current, frozenset()):
        raise EngineError(
            ErrorCode.STATE_ILLEGAL,
            f"illegal run status transition {current.value!r} -> {status.value!r}",
        )
    conn.execute("UPDATE _run SET status = ? WHERE run_id = ?", (str(status), run_id))


def write_config(conn: sqlite3.Connection, run_id: str, config_version: str, canonical_json: str) -> None:
    if not conn.in_transaction:
        raise EngineError(
            ErrorCode.DRAW_OUTSIDE_TXN,
            "write_config is callable only inside an open transaction",
        )
    conn.execute(
        "INSERT INTO _config (run_id, config_version, canonical_json) VALUES (?, ?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET "
        "config_version = excluded.config_version, canonical_json = excluded.canonical_json",
        (run_id, config_version, canonical_json),
    )


def read_config(conn: sqlite3.Connection, run_id: str) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT config_version, canonical_json FROM _config WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return (row["config_version"], row["canonical_json"])


def populate_visibility_registry(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise EngineError(
            ErrorCode.DRAW_OUTSIDE_TXN,
            "populate_visibility_registry is callable only inside an open transaction",
        )
    expected = sorted(
        ((field.table, field.column, str(vc)) for field, vc in visibility.FIELD_REGISTRY.items()),
        key=lambda item: (item[0], item[1]),
    )
    conn.executemany(
        'INSERT INTO _visibility_registry ("table", "column", visibility_class) '
        "VALUES (?, ?, ?) "
        'ON CONFLICT("table", "column") DO UPDATE SET '
        "visibility_class = excluded.visibility_class",
        expected,
    )
    registered_pairs = {(item[0], item[1]) for item in expected}
    stale = sorted(
        (row["table"], row["column"])
        for row in conn.execute('SELECT "table", "column" FROM _visibility_registry').fetchall()
        if (row["table"], row["column"]) not in registered_pairs
    )
    conn.executemany(
        'DELETE FROM _visibility_registry WHERE "table" = ? AND "column" = ?',
        stale,
    )


__all__ = [
    "RunStatus",
    "begin",
    "commit",
    "connect",
    "get_run",
    "in_memory",
    "insert_run",
    "new_run_id",
    "populate_visibility_registry",
    "read_config",
    "rollback",
    "set_run_status",
    "write_config",
]
