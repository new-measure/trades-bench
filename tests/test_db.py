from __future__ import annotations

import sqlite3

import pytest

from csr_bench import db as db_mod
from csr_bench.clock import phoenix_instant
from csr_bench.db import (
    begin,
    commit,
    get_run,
    in_memory,
    insert_run,
    rollback,
)
from csr_bench.enums import RunStatus
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.migrations import migrate
from csr_bench.visibility import FIELD_REGISTRY

START_AT_MS = phoenix_instant(2026, 10, 1, 6, 45)


@pytest.fixture
def conn():
    c = in_memory()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _seed_run(c, rid=None, *, status: RunStatus = RunStatus.INITIALIZING) -> str:
    rid = rid or db_mod.new_run_id()
    begin(c)
    insert_run(
        c,
        run_id=rid,
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        master_seed=7,
        start_at_ms=START_AT_MS,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        status=status,
    )
    commit(c)
    return rid


def test_migrate_produces_the_spec_02_schema(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r["name"] for r in rows}
    assert {"_run", "_config", "_clock", "_ledger_entries"}.issubset(names)
    assert all(n.startswith("_") for n in names)


def test_rollback_journal_not_wal_for_file_copy_resume(tmp_path):
    from csr_bench.db import connect

    path = tmp_path / "run.db"
    conn = connect(path)
    try:
        migrate(conn)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "delete"
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
    finally:
        conn.close()


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO _config(run_id, config_version, canonical_json) "
            "VALUES('nope', '1.0.0', '{}')"
        )


def test_txn_helpers_commit_and_rollback(conn):
    rid = _seed_run(conn)

    begin(conn)
    db_mod.write_config(conn, rid, "1.0.0", "{}")
    assert db_mod.read_config(conn, rid) == ("1.0.0", "{}")
    rollback(conn)
    assert db_mod.read_config(conn, rid) is None

    begin(conn)
    db_mod.write_config(conn, rid, "1.0.0", '{"a":1}')
    commit(conn)
    assert db_mod.read_config(conn, rid) == ("1.0.0", '{"a":1}')


def test_insert_run_seeds_run_and_clock(conn):
    rid = _seed_run(conn)
    run = get_run(conn, rid)
    assert run is not None
    assert run["status"] == "initializing"
    assert run["start_at_ms"] == START_AT_MS
    assert "config_json" not in run.keys()
    assert "current_time_ms" not in run.keys()

    clock = conn.execute("SELECT * FROM _clock").fetchone()
    assert clock["singleton"] == 1
    assert clock["current_time_ms"] == START_AT_MS
    assert clock["service_date"] == "2026-10-01"


def test_insert_run_requires_open_transaction(conn):
    with pytest.raises(EngineError) as ei:
        insert_run(
            conn,
            run_id=db_mod.new_run_id(),
            spec_version="1.0.0",
            config_version="1.0.0",
            config_hash="h",
            master_seed=7,
            start_at_ms=START_AT_MS,
            first_service_date="2026-10-01",
            last_service_date="2027-09-30",
            timezone="America/Phoenix",
        )
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN
    assert conn.execute("SELECT count(*) c FROM _run").fetchone()["c"] == 0
    assert conn.execute("SELECT count(*) c FROM _clock").fetchone()["c"] == 0


def test_get_run_missing_returns_none(conn):
    assert get_run(conn, "no-such-run") is None


@pytest.mark.parametrize(
    ("current", "target", "ok"),
    [
        (RunStatus.INITIALIZING, RunStatus.ACTIVE, True),
        (RunStatus.INITIALIZING, RunStatus.FAILED, True),
        (RunStatus.INITIALIZING, RunStatus.COMPLETED, False),
        (RunStatus.INITIALIZING, RunStatus.BANKRUPT, False),
        (RunStatus.ACTIVE, RunStatus.COMPLETED, True),
        (RunStatus.ACTIVE, RunStatus.BANKRUPT, True),
        (RunStatus.ACTIVE, RunStatus.FAILED, True),
        (RunStatus.ACTIVE, RunStatus.INITIALIZING, False),
        (RunStatus.COMPLETED, RunStatus.ACTIVE, False),
        (RunStatus.COMPLETED, RunStatus.BANKRUPT, False),
        (RunStatus.BANKRUPT, RunStatus.ACTIVE, False),
        (RunStatus.FAILED, RunStatus.INITIALIZING, False),
    ],
)
def test_set_run_status_transition_matrix(conn, current, target, ok):
    rid = _seed_run(conn, status=current)
    if ok:
        db_mod.set_run_status(conn, rid, target)
        assert get_run(conn, rid)["status"] == target.value
    else:
        with pytest.raises(EngineError) as ei:
            db_mod.set_run_status(conn, rid, target)
        assert ei.value.code is ErrorCode.STATE_ILLEGAL
        assert get_run(conn, rid)["status"] == current.value


def test_set_run_status_unknown_run(conn):
    with pytest.raises(EngineError) as ei:
        db_mod.set_run_status(conn, "no-such-run", RunStatus.ACTIVE)
    assert ei.value.code is ErrorCode.ENTITY_NOT_FOUND


def test_write_read_config_roundtrip_and_upsert(conn):
    rid = _seed_run(conn)

    begin(conn)
    db_mod.write_config(conn, rid, "1.0.0", '{"a":1}')
    commit(conn)
    assert db_mod.read_config(conn, rid) == ("1.0.0", '{"a":1}')

    begin(conn)
    db_mod.write_config(conn, rid, "1.1.0", '{"a":2}')
    commit(conn)
    rows = conn.execute("SELECT * FROM _config").fetchall()
    assert len(rows) == 1
    assert db_mod.read_config(conn, rid) == ("1.1.0", '{"a":2}')


def test_read_config_missing_returns_none(conn):
    assert db_mod.read_config(conn, "no-such-run") is None


def test_write_config_requires_open_transaction(conn):
    rid = _seed_run(conn)
    with pytest.raises(EngineError) as ei:
        db_mod.write_config(conn, rid, "1.0.0", "{}")
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN
    assert db_mod.read_config(conn, rid) is None


def test_populate_visibility_registry_matches_python_source(conn):
    expected = sorted(
        ((f.table, f.column, str(vc)) for f, vc in FIELD_REGISTRY.items()),
        key=lambda item: (item[0], item[1]),
    )
    begin(conn)
    db_mod.populate_visibility_registry(conn)
    db_mod.populate_visibility_registry(conn)
    commit(conn)
    rows = conn.execute(
        'SELECT "table", "column", visibility_class FROM _visibility_registry ORDER BY "table", "column"'
    ).fetchall()
    got = [(r["table"], r["column"], r["visibility_class"]) for r in rows]
    assert got == expected


def test_populate_visibility_registry_requires_open_transaction(conn):
    with pytest.raises(EngineError) as ei:
        db_mod.populate_visibility_registry(conn)
    assert ei.value.code is ErrorCode.DRAW_OUTSIDE_TXN


def test_new_run_id_unique():
    a = db_mod.new_run_id()
    b = db_mod.new_run_id()
    assert a != b
    assert len(a) == 36
