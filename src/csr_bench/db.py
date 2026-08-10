"""SQLite connection + run-record/clock/config helpers (spec 01 §Owned state).

All run state — the run record, the resolved config artifact, simulated time,
the event queue envelope, idempotency records, invocation records, RNG stream
states, the append-only ledger, and the draw-audit log — lives in this single
run database, and every event application and command is one committed
transaction.

The schema is created by :func:`csr_bench.migrations.migrate` (spec 02 §9:
"Runtime code Must not issue DDL outside the migration runner"), **not** by
this module; migration files live in ``schema/`` and the runner owns their
application, checksums, and the ``_schema_migrations`` record. This module
owns only the helpers that read and write the spec-01 records:

- the run record in ``_run`` (spec 01 §Run identity and time: identity,
  calendar, status, result-identification metadata — no config blob, no
  current time);
- simulated time in ``_clock``, the single time source (CSR-002 decision 3:
  ``_run`` keeps its immutable boundary fields but ``current_time_ms`` lives
  only in the clock row, written inside the same transaction that advances
  time);
- the resolved config artifact in ``_config`` (CSR-002 decision 5: the
  ``config_json`` column leaves ``_run``, which keeps only version + hash);
- idempotency records in ``_idempotency`` (CSR-002 decision 4: no ``run_id``
  columns outside ``_run``);
- the visibility-classification registry copy in ``_visibility_registry``.

Hidden engine state lives in distinct underscore-prefixed private tables
(``_events``, ``_invocations``, ``_rng_state``, ...); whitelisted public views
come from the views module. Timestamps are UTC epoch milliseconds (simulated
time only). Money is integer cents. Run-status / event-status / etc. are
stored as their enum string values.

Connection notes:
- ``isolation_level=None`` (autocommit) so callers manage ``BEGIN``/``COMMIT``
  /``ROLLBACK`` explicitly. The :mod:`csr_bench.txn` layer enforces one logical
  transaction boundary per mutation; every write helper here repeats that
  guard so a stray autocommit can never commit a partial write.
- Default journal mode (rollback journal, *not* WAL) so a committed database file
  is self-contained — a fresh process can copy the file and resume (DET-02).
- ``PRAGMA foreign_keys=ON`` so a run row deletes cascade to its children.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from . import visibility
from .enums import RunStatus
from .errors import EngineError, ErrorCode

#: The only legal run-status transitions. Spec 01 §Run identity and time
#: (specs/01-run-contracts.md:57) fixes ``initializing -> active -> (completed
#: | bankrupt | failed)``; spec 02 §12 adds the botched-startup exit
#: ``initializing -> failed`` (CSR-002 decision 12: a failed initialization is
#: the terminal, unscored outcome of a botched startup — migration failure,
#: registry-hash mismatch, or warm-start clean-boundary violation; the run is
#: reported ``failed`` and a partially built database is never reused).
#: Terminal statuses have no outgoing edge — once reached, no later invocation
#: or world event is processed (spec 01 invariant #9).
_LEGAL_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.INITIALIZING: frozenset({RunStatus.ACTIVE, RunStatus.FAILED}),
    RunStatus.ACTIVE: frozenset({RunStatus.COMPLETED, RunStatus.BANKRUPT, RunStatus.FAILED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.BANKRUPT: frozenset(),
    RunStatus.FAILED: frozenset(),
}


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a run database at ``path`` in autocommit mode with FK enforcement.

    Rollback journal (not WAL) so a committed file is copy-resumable (DET-02).
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Keep the default rollback journal (DELETE) — do NOT enable WAL so the
    # committed .db file is self-contained for file-copy resume.
    conn.execute("PRAGMA journal_mode = DELETE")
    return conn


def in_memory() -> sqlite3.Connection:
    """:func:`connect` onto a private in-memory database (for tests)."""
    return connect(":memory:")


def begin(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")


def commit(conn: sqlite3.Connection) -> None:
    conn.execute("COMMIT")


def rollback(conn: sqlite3.Connection) -> None:
    conn.execute("ROLLBACK")


def new_run_id() -> str:
    """A fresh run identifier (UUID v4 string).

    UUIDs identify runs; they are not used for randomness on the decision path
    (no UUID-randomness dependency in world logic — spec 01 §Deterministic
    randomness).
    """
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
    system_prompt_hash: str | None = None,
) -> None:
    """Create the run record in ``_run`` and seed the ``_clock`` singleton.

    The clock row ``(1, start_at_ms, first_service_date)`` is written in the
    same call and transaction as the run row (CSR-002 decision 3: ``_clock``
    is the single time source, initialized at run creation from the run's
    immutable boundary fields — the init sequence starts the clock at
    2026-10-01T06:45:00-07:00). The resolved config artifact is written
    separately by :func:`write_config`.

    Requires an open transaction, guarded the same way as ``Ledger.post``: the
    connection is autocommit, so without the guard the two rows would commit
    independently and a mid-way failure could leave a clock row without a run.
    """
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
            system_prompt_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    """Update run status; the controller is the only writer, along the legal
    transition ``initializing -> (active | failed) -> (completed|bankrupt|failed)``.

    ``initializing -> failed`` is the terminal, unscored outcome of a botched
    startup per spec 02 §12 (decision 12): migration failure, registry-hash
    mismatch, or a warm-start clean-boundary violation aborts initialization,
    the run never reaches ``active``, and a partially built database is never
    reused.

    Enforced here — not merely documented — against :data:`_LEGAL_RUN_TRANSITIONS`:
    an illegal transition (including any move out of a terminal status) raises
    :class:`EngineError` with :data:`ErrorCode.STATE_ILLEGAL` and leaves the row
    unchanged. :meth:`csr_bench.ledger.Ledger.bankruptcy_check` delegates its
    ``active -> bankrupt`` flip here rather than issuing its own raw
    ``UPDATE`` (see CSR-005 for the controller call sites).
    """
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
    """Upsert the resolved config artifact into ``_config`` (one row per run).

    The complete resolved config is persisted in the private run artifact
    (spec 01 §Configuration rule 5); the row is a durable copy keyed by the
    run it belongs to (CSR-002 decision 5), so writing it again overwrites
    rather than duplicates. Requires an open transaction, guarded like every
    write helper here.
    """
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
    """``(config_version, canonical_json)`` for ``run_id``, or ``None``."""
    row = conn.execute(
        "SELECT config_version, canonical_json FROM _config WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return (row["config_version"], row["canonical_json"])


def populate_visibility_registry(conn: sqlite3.Connection) -> None:
    """Materialize :data:`csr_bench.visibility.FIELD_REGISTRY` into ``_visibility_registry``.

    This materializes the Python registry — the single source the engine reads
    (:func:`csr_bench.visibility.classify`) — into its durable post-run copy,
    for post-run analysis and the future authorizer (CSR-002 decision 5). Rows
    are written in sorted ``(table, column)`` order; on conflict the class is
    refreshed, so re-running is a no-op sync and a sync test asserts the table
    never drifts from the Python source. Requires an open transaction, guarded
    like every write helper here.
    """
    if not conn.in_transaction:
        raise EngineError(
            ErrorCode.DRAW_OUTSIDE_TXN,
            "populate_visibility_registry is callable only inside an open transaction",
        )
    conn.executemany(
        'INSERT INTO _visibility_registry ("table", "column", visibility_class) '
        "VALUES (?, ?, ?) "
        'ON CONFLICT("table", "column") DO UPDATE SET '
        "visibility_class = excluded.visibility_class",
        [
            (field.table, field.column, str(vc))
            for field, vc in sorted(
                visibility.FIELD_REGISTRY.items(), key=lambda item: (item[0].table, item[0].column)
            )
        ],
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
