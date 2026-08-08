"""Tests for the SQLite connection + core-table DDL (spec 01 §Owned state)."""

from __future__ import annotations

import sqlite3

import pytest

from csr_bench import db as db_mod
from csr_bench.db import (
    SCHEMA_VERSION,
    begin,
    commit,
    get_run,
    in_memory,
    init_schema,
    insert_run,
    rollback,
)
from csr_bench.enums import RunStatus

PRIVATE_TABLES = {
    "_events",
    "_ledger",
    "_invocations",
    "_command_responses",
    "_rng_state",
    "_rng_draw_audit",
    "_schema_meta",
}


@pytest.fixture
def conn():
    c = in_memory()
    init_schema(c)
    try:
        yield c
    finally:
        c.close()


def _seed_run(c, rid=None) -> str:
    rid = rid or db_mod.new_run_id()
    insert_run(
        c,
        run_id=rid,
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        config_json="{}",
        master_seed=7,
        start_at_ms=0,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        current_time_ms=0,
    )
    return rid


def test_init_schema_creates_all_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert "run" in names
    assert PRIVATE_TABLES.issubset(names)


def test_schema_version_recorded(conn):
    row = conn.execute("SELECT value FROM _schema_meta WHERE key='schema_version'").fetchone()
    assert int(row["value"]) == SCHEMA_VERSION


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO _events(run_id, kind, scheduled_at_ms, priority, status, "
            "created_at_ms) VALUES('nope', 'k', 0, 0, 'pending', 0)"
        )


def test_txn_helpers_commit_and_rollback(conn):
    rid = _seed_run(conn)
    assert get_run(conn, rid) is not None
    assert get_run(conn, rid)["status"] == "initializing"

    begin(conn)
    conn.execute(
        "INSERT INTO _ledger(run_id, amount_cents, category, simulated_ts_ms) "
        "VALUES(?, 100, 'contract_test_credit', 0)",
        (rid,),
    )
    commit(conn)
    n = conn.execute("SELECT count(*) c FROM _ledger WHERE run_id=?", (rid,)).fetchone()["c"]
    assert n == 1

    begin(conn)
    conn.execute(
        "INSERT INTO _ledger(run_id, amount_cents, category, simulated_ts_ms) "
        "VALUES(?, 1, 'contract_test_debit', 1)",
        (rid,),
    )
    rollback(conn)
    n = conn.execute("SELECT count(*) c FROM _ledger WHERE run_id=?", (rid,)).fetchone()["c"]
    assert n == 1  # unchanged


def test_event_order_index_present(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    names = {r["name"] for r in rows}
    assert {"_events_order", "_events_status"}.issubset(names)


def test_private_tables_are_underscore_prefixed(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    internal = {n for n in names if n.startswith("sqlite_")}  # e.g. sqlite_sequence
    private = names - {"run"} - internal
    assert private == PRIVATE_TABLES
    assert all(t.startswith("_") for t in private)


def test_set_run_status_and_time(conn):
    rid = _seed_run(conn)
    db_mod.set_run_status(conn, rid, RunStatus.ACTIVE)
    db_mod.set_current_time(conn, rid, 12345)
    row = get_run(conn, rid)
    assert row["status"] == "active"
    assert row["current_time_ms"] == 12345


def test_set_run_status_enforces_legal_transitions(conn):
    # initializing -> active -> (completed | bankrupt | failed) is the only
    # legal path (specs/01-run-contracts.md:57); anything else is rejected
    # with a structured error and the row is left unchanged.
    from csr_bench.errors import EngineError, ErrorCode

    rid = _seed_run(conn)
    assert get_run(conn, rid)["status"] == "initializing"

    # skipping straight to a terminal status without going through active fails.
    with pytest.raises(EngineError) as ei:
        db_mod.set_run_status(conn, rid, RunStatus.COMPLETED)
    assert ei.value.code is ErrorCode.STATE_ILLEGAL
    assert get_run(conn, rid)["status"] == "initializing"  # unchanged

    db_mod.set_run_status(conn, rid, RunStatus.ACTIVE)
    db_mod.set_run_status(conn, rid, RunStatus.COMPLETED)
    assert get_run(conn, rid)["status"] == "completed"


@pytest.mark.parametrize("terminal", [RunStatus.COMPLETED, RunStatus.BANKRUPT, RunStatus.FAILED])
def test_set_run_status_terminal_states_are_immutable(conn, terminal):
    from csr_bench.errors import EngineError, ErrorCode

    rid = _seed_run(conn)
    db_mod.set_run_status(conn, rid, RunStatus.ACTIVE)
    db_mod.set_run_status(conn, rid, terminal)
    assert get_run(conn, rid)["status"] == terminal.value

    # no transition out of a terminal status is legal, including re-entering
    # active or a different terminal status.
    for target in (RunStatus.ACTIVE, RunStatus.COMPLETED, RunStatus.BANKRUPT, RunStatus.FAILED):
        with pytest.raises(EngineError) as ei:
            db_mod.set_run_status(conn, rid, target)
        assert ei.value.code is ErrorCode.STATE_ILLEGAL
    assert get_run(conn, rid)["status"] == terminal.value  # unchanged throughout


def test_new_run_id_unique():
    a = db_mod.new_run_id()
    b = db_mod.new_run_id()
    assert a != b
    assert len(a) == 36  # canonical UUID string


def test_rollback_journal_not_wal_for_file_copy_resume(tmp_path):
    # On a real file DB the engine must not enable WAL so a committed file is
    # self-contained for file-copy resume (DET-02).
    from csr_bench.db import connect

    path = tmp_path / "run.db"
    conn = connect(path)
    try:
        init_schema(conn)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "delete"
    finally:
        conn.close()
