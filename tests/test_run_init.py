"""Tests for the initialization sequence (spec 02 §9, §12; CSR-002 decision 12).

The open sequence is idempotent (migrate no-op, views recreated, golden hash
verified), the end-to-end init leaves an ``active`` run with the config
artifact and visibility registry persisted, and the failure path reports the
run ``failed`` and refuses to rebuild onto the same file — a partially built
database is never reused, so a clean re-init requires a fresh file.
"""

from __future__ import annotations

import sqlite3

import pytest

from _helpers import register_conn
from csr_bench import views, warm_start
from csr_bench.config import load_run_config
from csr_bench.db import read_config
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.migrations import applied_versions
from csr_bench.run_init import initialize_run, open_run_db
from csr_bench.views import VIEWS, verify_views
from csr_bench.visibility import FIELD_REGISTRY


def _cfg():
    return load_run_config()


def test_open_run_db_twice_is_idempotent(tmp_path):
    path = tmp_path / "idem.db"
    conn1 = open_run_db(path)
    assert applied_versions(conn1) == [(1, "baseline")]
    conn1.close()
    # second open: migrations are a no-op, views are recreated from the
    # registry, and the golden hash verifies — anything else raises
    conn2 = open_run_db(path)
    register_conn(conn2)
    verify_views(conn2)
    stored = {
        row[0]
        for row in conn2.execute("SELECT name FROM sqlite_schema WHERE type='view'")
    }
    assert stored == {spec.name for spec in VIEWS}


def test_initialize_run_end_to_end(tmp_path):
    cfg = _cfg()
    run_id = initialize_run(tmp_path / "run.db", master_seed=7, config=cfg)
    conn = open_run_db(tmp_path / "run.db")
    register_conn(conn)
    assert conn.execute("SELECT status FROM _run").fetchone()["status"] == "active"
    stored = read_config(conn, run_id)
    assert stored is not None
    version, canonical = stored
    assert version == cfg.config_version
    assert canonical == cfg.canonical_bytes.decode()
    count = conn.execute("SELECT COUNT(*) c FROM _visibility_registry").fetchone()["c"]
    assert count == len(FIELD_REGISTRY)


def test_initialize_run_uses_explicit_run_id(tmp_path):
    cfg = _cfg()
    initialize_run(tmp_path / "r2.db", run_id="my-run-id", master_seed=3, config=cfg)
    conn = open_run_db(tmp_path / "r2.db")
    register_conn(conn)
    assert conn.execute("SELECT run_id FROM _run").fetchone()["run_id"] == "my-run-id"


def test_initialize_run_failure_marks_failed_and_refuses_rebuild(tmp_path, monkeypatch):
    cfg = _cfg()
    path = tmp_path / "boom.db"

    def _boom(*args, **kwargs):
        raise RuntimeError("warm-start exploded")

    # a failure inside the warm start aborts initialization (spec 02 §12):
    # the run is reported failed, and the exception propagates
    with monkeypatch.context() as m:
        m.setattr(warm_start, "run", _boom)
        with pytest.raises(RuntimeError, match="warm-start exploded"):
            initialize_run(path, master_seed=1, config=cfg)
    conn = open_run_db(path)
    register_conn(conn)
    assert conn.execute("SELECT status FROM _run").fetchone()["status"] == "failed"
    conn.close()

    # a partially built database is never reused (spec 02 §12): a second
    # initialize_run on the same path is refused outright, even though the
    # warm start itself would now succeed
    with pytest.raises(EngineError) as ei:
        initialize_run(path, master_seed=1, config=cfg)
    assert ei.value.code is ErrorCode.STATE_ILLEGAL

    # a clean re-init requires a fresh file
    fresh = tmp_path / "fresh.db"
    initialize_run(fresh, master_seed=1, config=cfg)
    conn2 = open_run_db(fresh)
    register_conn(conn2)
    assert conn2.execute("SELECT status FROM _run").fetchone()["status"] == "active"


def test_initialize_run_failure_at_boundary_check_marks_failed(tmp_path, monkeypatch):
    cfg = _cfg()
    path = tmp_path / "boundary.db"

    def _boom(conn, config):
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, "clean-boundary violation")

    # a clean-boundary violation is a botched startup, not a valid run
    # (spec 02 §12): the run is reported failed and the error propagates —
    # the warm start itself committed, so the failure marking is what the
    # partially built database must show
    with monkeypatch.context() as m:
        m.setattr("csr_bench.run_init.boundary_check", _boom)
        with pytest.raises(EngineError) as ei:
            initialize_run(path, master_seed=1, config=cfg)
    assert ei.value.code is ErrorCode.INVARIANT_VIOLATED
    conn = open_run_db(path)
    register_conn(conn)
    assert conn.execute("SELECT status FROM _run").fetchone()["status"] == "failed"



def test_initialize_run_failure_before_run_record_leaves_no_failed_row(tmp_path, monkeypatch):
    cfg = _cfg()
    path = tmp_path / "early.db"

    def _boom(*args, **kwargs):
        raise RuntimeError("open exploded")

    # failure before the run record exists (here: at open) — nothing to mark
    # failed, but the failure still propagates
    with monkeypatch.context() as m:
        m.setattr("csr_bench.run_init.open_run_db", _boom)
        with pytest.raises(RuntimeError, match="open exploded"):
            initialize_run(path, master_seed=1, config=cfg)
    conn = open_run_db(path)
    register_conn(conn)
    assert conn.execute("SELECT COUNT(*) c FROM _run").fetchone()["c"] == 0


def test_open_run_db_wraps_view_ddl_failure_into_migration_failed(tmp_path, monkeypatch):
    # A raw sqlite3 failure in the open path's view-DDL step (a view body
    # that no longer parses, ...) must surface as the stable MIGRATION_FAILED
    # code, never as the raw exception (final-branch audit F3). EngineErrors
    # from migrate/verify_views pass through unchanged.
    def _boom(conn):
        raise sqlite3.OperationalError("no such table: _missing")

    with monkeypatch.context() as m:
        m.setattr(views, "create_views", _boom)
        with pytest.raises(EngineError) as ei:
            open_run_db(tmp_path / "viewboom.db")
    assert ei.value.code is ErrorCode.MIGRATION_FAILED


def test_warm_start_wraps_raw_fixture_failure_and_init_marks_failed(tmp_path, monkeypatch):
    # A raw sqlite3 failure inside the fixture build must surface as the
    # stable UNEXPECTED_FAILURE code (the rollback-and-reraise shape stays),
    # and the init path must still report the run failed (spec 02 §12;
    # final-branch audit F3).
    def _boom(conn, config):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: _technicians.id")

    cfg = _cfg()
    path = tmp_path / "fixtureboom.db"
    with monkeypatch.context() as m:
        m.setattr(warm_start, "_build_fixture", _boom)
        with pytest.raises(EngineError) as ei:
            initialize_run(path, master_seed=1, config=cfg)
    assert ei.value.code is ErrorCode.UNEXPECTED_FAILURE
    conn = open_run_db(path)
    register_conn(conn)
    assert conn.execute("SELECT status FROM _run").fetchone()["status"] == "failed"
