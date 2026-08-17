from __future__ import annotations

import sqlite3

import pytest

from _helpers import GEO_ARTIFACT_DIR, geo_run_config, register_conn
from csr_bench import views, warm_start
from csr_bench.db import read_config
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.migrations import applied_versions
from csr_bench.run_init import initialize_run, open_run_db
from csr_bench.views import VIEWS, verify_views
from csr_bench.visibility import FIELD_REGISTRY


def _cfg():
    return geo_run_config()


def test_open_run_db_twice_is_idempotent(tmp_path):
    path = tmp_path / "idem.db"
    conn1 = open_run_db(path)
    assert applied_versions(conn1) == [
        (1, "baseline"), (2, "routing_anchors"), (3, "demand_primitives"),
        (4, "day_state"), (5, "booking_columns"), (6, "call_arrival"),
        (7, "idempotency_fingerprint"), (8, "job_outcomes"),
    ]
    conn1.close()
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
    run_id = initialize_run(
        tmp_path / "run.db", master_seed=7, config=cfg, artifact_dir=GEO_ARTIFACT_DIR
    )
    conn = open_run_db(tmp_path / "run.db", artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    assert conn.execute("SELECT status FROM _run").fetchone()["status"] == "active"
    stored = read_config(conn, run_id)
    assert stored is not None
    version, canonical = stored
    assert version == cfg.config_version
    assert canonical == cfg.canonical_bytes.decode()
    count = conn.execute("SELECT COUNT(*) c FROM _visibility_registry").fetchone()["c"]
    assert count == len(FIELD_REGISTRY)


def test_initialize_run_audits_every_consumed_draw(tmp_path):
    cfg = _cfg()
    initialize_run(
        tmp_path / "audited.db", master_seed=11, config=cfg, artifact_dir=GEO_ARTIFACT_DIR
    )
    conn = open_run_db(tmp_path / "audited.db", artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    audited = conn.execute(
        "SELECT stream_name, entity_type, entity_id, event_kind, draw_index "
        "FROM _random_audit WHERE event_kind='sequential' ORDER BY seq"
    ).fetchall()
    assert audited
    per_stream: dict[str, list[int]] = {}
    for row in audited:
        assert row["entity_type"] == "run"
        assert row["entity_id"] == 1
        per_stream.setdefault(row["stream_name"], []).append(row["draw_index"])
    counters = {
        row["stream_name"]: row["counter"]
        for row in conn.execute("SELECT stream_name, counter FROM _rng_state")
        if row["counter"] > 0
    }
    assert per_stream == {
        name: list(range(counter)) for name, counter in counters.items()
    }
    assert "weather_process" in per_stream


def test_initialize_run_uses_explicit_run_id(tmp_path):
    cfg = _cfg()
    initialize_run(
        tmp_path / "r2.db", run_id="my-run-id", master_seed=3, config=cfg,
        artifact_dir=GEO_ARTIFACT_DIR,
    )
    conn = open_run_db(tmp_path / "r2.db", artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    assert conn.execute("SELECT run_id FROM _run").fetchone()["run_id"] == "my-run-id"


def test_initialize_run_failure_marks_failed_and_refuses_rebuild(tmp_path, monkeypatch):
    cfg = _cfg()
    path = tmp_path / "boom.db"

    def _boom(*args, **kwargs):
        raise RuntimeError("warm-start exploded")

    with monkeypatch.context() as m:
        m.setattr(warm_start, "run", _boom)
        with pytest.raises(RuntimeError, match="warm-start exploded"):
            initialize_run(path, master_seed=1, config=cfg, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    assert conn.execute("SELECT status FROM _run").fetchone()["status"] == "failed"
    conn.close()

    with pytest.raises(EngineError) as ei:
        initialize_run(path, master_seed=1, config=cfg, artifact_dir=GEO_ARTIFACT_DIR)
    assert ei.value.code is ErrorCode.STATE_ILLEGAL

    fresh = tmp_path / "fresh.db"
    initialize_run(fresh, master_seed=1, config=cfg, artifact_dir=GEO_ARTIFACT_DIR)
    conn2 = open_run_db(fresh, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn2)
    assert conn2.execute("SELECT status FROM _run").fetchone()["status"] == "active"


def test_initialize_run_failure_at_boundary_check_marks_failed(tmp_path, monkeypatch):
    cfg = _cfg()
    path = tmp_path / "boundary.db"

    def _boom(conn, config):
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, "clean-boundary violation")

    with monkeypatch.context() as m:
        m.setattr("csr_bench.run_init.boundary_check", _boom)
        with pytest.raises(EngineError) as ei:
            initialize_run(path, master_seed=1, config=cfg, artifact_dir=GEO_ARTIFACT_DIR)
    assert ei.value.code is ErrorCode.INVARIANT_VIOLATED
    conn = open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    assert conn.execute("SELECT status FROM _run").fetchone()["status"] == "failed"


def test_initialize_run_failure_before_run_record_leaves_no_failed_row(tmp_path, monkeypatch):
    cfg = _cfg()
    path = tmp_path / "early.db"

    def _boom(*args, **kwargs):
        raise RuntimeError("open exploded")

    with monkeypatch.context() as m:
        m.setattr("csr_bench.run_init.open_run_db", _boom)
        with pytest.raises(RuntimeError, match="open exploded"):
            initialize_run(path, master_seed=1, config=cfg, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_run_db(path)
    register_conn(conn)
    assert conn.execute("SELECT COUNT(*) c FROM _run").fetchone()["c"] == 0


def test_open_run_db_wraps_view_ddl_failure_into_migration_failed(tmp_path, monkeypatch):
    def _boom(conn):
        raise sqlite3.OperationalError("no such table: _missing")

    with monkeypatch.context() as m:
        m.setattr(views, "create_views", _boom)
        with pytest.raises(EngineError) as ei:
            open_run_db(tmp_path / "viewboom.db")
    assert ei.value.code is ErrorCode.MIGRATION_FAILED


def test_warm_start_wraps_raw_fixture_failure_and_init_marks_failed(tmp_path, monkeypatch):
    def _boom(conn, config):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: _technicians.id")

    cfg = _cfg()
    path = tmp_path / "fixtureboom.db"
    with monkeypatch.context() as m:
        m.setattr(warm_start, "_build_fixture", _boom)
        with pytest.raises(EngineError) as ei:
            initialize_run(path, master_seed=1, config=cfg, artifact_dir=GEO_ARTIFACT_DIR)
    assert ei.value.code is ErrorCode.UNEXPECTED_FAILURE
    conn = open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    assert conn.execute("SELECT status FROM _run").fetchone()["status"] == "failed"
