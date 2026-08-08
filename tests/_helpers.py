"""Plain test helper utilities (no pytest fixtures) shared across the suite.

Kept separate from ``conftest.py`` so test modules can ``from _helpers import ...``
without importing the conftest module.
"""

from __future__ import annotations

import sqlite3

from csr_bench.db import in_memory, init_schema, insert_run, new_run_id
from csr_bench.enums import RunStatus
from csr_bench.events import Events
from csr_bench.invocations import Invocations
from csr_bench.ledger import Ledger
from csr_bench.rng import Rng
from csr_bench.txn import Services

# Connections opened by per-test helper functions, closed by the autouse
# ``_close_open_conns`` fixture in conftest.
_OPEN_CONNS: list = []


def register_conn(conn) -> None:
    _OPEN_CONNS.append(conn)


def audit_recorder(conn: sqlite3.Connection, run_id: str):
    """A draw-audit recorder that persists keys inside the open transaction."""

    def rec(**kw):
        conn.execute(
            "INSERT INTO _rng_draw_audit(run_id, stream_name, entity_type, entity_id, "
            "scope, event_kind, draw_index, outcome_id, simulated_ts_ms) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            (
                run_id,
                kw["stream_name"],
                kw["entity_type"],
                kw["entity_id"],
                kw["scope"],
                kw["event_kind"],
                kw["draw_index"],
                kw.get("outcome_id"),
            ),
        )

    return rec


def make_services(
    *,
    active_invocation: bool = False,
    status: RunStatus = RunStatus.ACTIVE,
    master_seed: int = 1,
    now_ms: int = 0,
):
    """Build a fresh in-memory run + wired :class:`Services` for a test.

    Returns ``(conn, run_id, services, ledger, events, rng)``. Connections are
    auto-closed via :data:`_OPEN_CONNS`.
    """
    conn = in_memory()
    register_conn(conn)
    init_schema(conn)
    rid = new_run_id()
    conn.execute("BEGIN")
    insert_run(
        conn,
        run_id=rid,
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        config_json="{}",
        master_seed=master_seed,
        start_at_ms=0,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        current_time_ms=now_ms,
        status=status,
    )
    conn.execute("COMMIT")
    rng = Rng(master_seed, "1.0.0", audit_recorder=audit_recorder(conn, rid), conn=conn, run_id=rid)
    ledger = Ledger(conn, rid)
    events = Events(conn, rid, rng)
    inv = Invocations(conn, rid)
    if active_invocation:
        conn.execute(
            "INSERT INTO _invocations(run_id, type, ts_ms, sequence, status, attempt_count) "
            "VALUES (?, 'call', 0, 1, 'active', 1)",
            (rid,),
        )
    sv = Services(conn=conn, run_id=rid, rng=rng, ledger=ledger, invocations=inv, events=events)
    return conn, rid, sv, ledger, events, rng


def audit_count(conn, run_id) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM _rng_draw_audit WHERE run_id=?", (run_id,)
    ).fetchone()["c"]
