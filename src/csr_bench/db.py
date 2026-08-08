"""SQLite connection + core-table DDL (spec 01 §Owned state, §Interfaces).

All run state — the run record, the resolved config artifact, the event queue
envelope, idempotency records, invocation records, RNG stream states, the
append-only ledger, and the draw-audit log — lives in this single run database,
and every event application and command is one committed transaction.

Hidden engine state lives in distinct underscore-prefixed private tables
(``_events``, ``_ledger``, ``_invocations``, ``_command_responses``,
``_rng_state``, ``_rng_draw_audit``); whitelisted public views come in CSR-002.
Timestamps are UTC epoch milliseconds (simulated time only). Money is integer
cents. Run-status / event-status / etc. are stored as their enum string values.

Connection notes:
- ``isolation_level=None`` (autocommit) so callers manage ``BEGIN``/``COMMIT``
  /``ROLLBACK`` explicitly. The :mod:`csr_bench.txn` layer enforces one logical
  transaction boundary per mutation.
- Default journal mode (rollback journal, *not* WAL) so a committed database file
  is self-contained — a fresh process can copy the file and resume (DET-02).
- ``PRAGMA foreign_keys=ON`` so a run row deletes cascade to its children.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from .enums import EventStatus, InvocationStatus, InvocationType, RunStatus
from .errors import EngineError, ErrorCode

SCHEMA_VERSION = 1

#: The only legal run-status transitions (spec 01 §Run identity and time,
#: specs/01-run-contracts.md:57): ``initializing -> active -> (completed |
#: bankrupt | failed)``. Terminal statuses have no outgoing edge — once
#: reached, no later invocation or world event is processed (invariant #9),
#: and the run status itself is immutable.
_LEGAL_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.INITIALIZING: frozenset({RunStatus.ACTIVE}),
    RunStatus.ACTIVE: frozenset({RunStatus.COMPLETED, RunStatus.BANKRUPT, RunStatus.FAILED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.BANKRUPT: frozenset(),
    RunStatus.FAILED: frozenset(),
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS run (
    run_id              TEXT PRIMARY KEY,
    spec_version        TEXT NOT NULL,
    config_version      TEXT NOT NULL,
    config_hash        TEXT NOT NULL,
    config_json        TEXT NOT NULL,
    master_seed         INTEGER NOT NULL,
    start_at_ms         INTEGER NOT NULL,
    first_service_date  TEXT NOT NULL,
    last_service_date   TEXT NOT NULL,
    timezone            TEXT NOT NULL,
    status              TEXT NOT NULL,
    current_time_ms     INTEGER NOT NULL,
    model_id            TEXT,
    harness_name        TEXT,
    harness_version     TEXT,
    context_policy      TEXT,
    system_prompt_hash  TEXT
);

CREATE TABLE IF NOT EXISTS _events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    scheduled_at_ms INTEGER NOT NULL,
    priority        INTEGER NOT NULL,
    entity_type     TEXT,
    entity_id       INTEGER,
    payload_version INTEGER NOT NULL DEFAULT 1,
    payload         TEXT,
    status          TEXT NOT NULL,
    created_at_ms   INTEGER NOT NULL,
    applied_at_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS _events_order
    ON _events (run_id, scheduled_at_ms, priority, event_id);
CREATE INDEX IF NOT EXISTS _events_status
    ON _events (run_id, status);

-- Append-only ledger. cash_balance() = opening_bal_adj + sum(amount_cents).
CREATE TABLE IF NOT EXISTS _ledger (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    amount_cents    INTEGER NOT NULL,
    category        TEXT NOT NULL,
    entity_type     TEXT,
    entity_id       INTEGER,
    simulated_ts_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS _ledger_order ON _ledger (run_id, seq);

CREATE TABLE IF NOT EXISTS _invocations (
    invocation_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    ts_ms           INTEGER NOT NULL,
    sequence        INTEGER NOT NULL,
    status          TEXT NOT NULL,
    prompt_version  TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS _invocations_order ON _invocations (run_id, sequence);

-- Idempotency responses, byte-for-byte identical on retry within a run.
-- Namespace separates public commands from the controller-privileged namespace
-- that the public surface cannot produce; every read/write is namespace-scoped
-- (txn.py), so the primary key must include it too — otherwise INSERT OR
-- IGNORE (txn.py) can silently drop a response that the namespace-scoped
-- idempotency lookup can never find, breaking byte-for-byte retry.
CREATE TABLE IF NOT EXISTS _command_responses (
    run_id      TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    request_id  TEXT NOT NULL,
    namespace   TEXT NOT NULL,
    response    BLOB NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, namespace, request_id)
);

-- Sequential stream state persisted inside the advancing transaction.
CREATE TABLE IF NOT EXISTS _rng_state (
    run_id      TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    stream_name TEXT NOT NULL,
    seed        TEXT NOT NULL,
    counter     INTEGER NOT NULL,
    PRIMARY KEY (run_id, stream_name)
);

-- Draw-audit: each consumed draw key + an outcome id, never the u64 result or
-- the seed (spec 01 §Audit).
CREATE TABLE IF NOT EXISTS _rng_draw_audit (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    stream_name     TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    entity_id       INTEGER NOT NULL,
    scope           TEXT NOT NULL,
    event_kind      TEXT NOT NULL,
    draw_index      INTEGER NOT NULL,
    outcome_id      TEXT,
    simulated_ts_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS _rng_audit_key
    ON _rng_draw_audit (run_id, stream_name, entity_type, entity_id, scope, event_kind, draw_index);

CREATE TABLE IF NOT EXISTS _schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


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


def init_schema(conn: sqlite3.Connection, *, schema_version: int = SCHEMA_VERSION) -> None:
    """Create the core contract tables if absent and record the schema version."""
    conn.executescript(_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO _schema_meta(key, value) VALUES('schema_version', ?1) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(schema_version),),
    )


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
    config_json: str,
    master_seed: int,
    start_at_ms: int,
    first_service_date: str,
    last_service_date: str,
    timezone: str,
    current_time_ms: int,
    status: RunStatus = RunStatus.INITIALIZING,
    model_id: str | None = None,
    harness_name: str | None = None,
    harness_version: str | None = None,
    context_policy: str | None = None,
    system_prompt_hash: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO run (
            run_id, spec_version, config_version, config_hash, config_json,
            master_seed, start_at_ms, first_service_date, last_service_date,
            timezone, status, current_time_ms,
            model_id, harness_name, harness_version, context_policy,
            system_prompt_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            spec_version,
            config_version,
            config_hash,
            config_json,
            master_seed,
            start_at_ms,
            first_service_date,
            last_service_date,
            timezone,
            str(status),
            current_time_ms,
            model_id,
            harness_name,
            harness_version,
            context_policy,
            system_prompt_hash,
        ),
    )


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone()


def set_run_status(conn: sqlite3.Connection, run_id: str, status: RunStatus) -> None:
    """Update run status; the controller is the only writer, along the legal
    transition ``initializing -> active -> (completed|bankrupt|failed)``.

    Enforced here — not merely documented — against :data:`_LEGAL_RUN_TRANSITIONS`:
    an illegal transition (including any move out of a terminal status) raises
    :class:`EngineError` with :data:`ErrorCode.STATE_ILLEGAL` and leaves the row
    unchanged. :meth:`csr_bench.ledger.Ledger.bankruptcy_check` delegates its
    ``active -> bankrupt`` flip here rather than issuing its own raw
    ``UPDATE`` (see CSR-005 for the controller call sites).
    """
    row = conn.execute("SELECT status FROM run WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise EngineError(ErrorCode.ENTITY_NOT_FOUND, f"run {run_id!r} not found")
    current = RunStatus(row["status"])
    if status not in _LEGAL_RUN_TRANSITIONS.get(current, frozenset()):
        raise EngineError(
            ErrorCode.STATE_ILLEGAL,
            f"illegal run status transition {current.value!r} -> {status.value!r}",
        )
    conn.execute("UPDATE run SET status = ? WHERE run_id = ?", (str(status), run_id))


def set_current_time(conn: sqlite3.Connection, run_id: str, millis: int) -> None:
    conn.execute("UPDATE run SET current_time_ms = ? WHERE run_id = ?", (millis, run_id))


__all__ = [
    "EventStatus",
    "InvocationStatus",
    "InvocationType",
    "RunStatus",
    "SCHEMA_VERSION",
    "begin",
    "commit",
    "connect",
    "get_run",
    "in_memory",
    "init_schema",
    "insert_run",
    "new_run_id",
    "rollback",
    "set_current_time",
    "set_run_status",
]
