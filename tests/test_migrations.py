from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from csr_bench.db import in_memory
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.migrations import MIGRATIONS_PACKAGE_DIR, applied_versions, migrate


@pytest.fixture
def conn():
    c = in_memory()
    try:
        yield c
    finally:
        c.close()


def _copy_schema_dir(tmp_path: Path) -> Path:
    dst = tmp_path / "schema"
    dst.mkdir()
    for f in MIGRATIONS_PACKAGE_DIR.iterdir():
        if f.is_file():
            shutil.copy2(f, dst / f.name)
    return dst


def _table_names(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_applies_baseline_from_package_and_reapply_is_noop(conn):
    assert migrate(conn) == [
        "0001_baseline", "0002_routing_anchors", "0003_demand_primitives",
        "0004_day_state", "0005_booking_columns", "0006_call_arrival",
        "0007_idempotency_fingerprint", "0008_job_outcomes",
    ]
    assert migrate(conn) == []
    assert applied_versions(conn) == [
        (1, "baseline"), (2, "routing_anchors"), (3, "demand_primitives"),
        (4, "day_state"), (5, "booking_columns"), (6, "call_arrival"),
        (7, "idempotency_fingerprint"), (8, "job_outcomes"),
    ]
    assert {
        "_run", "_customers", "_ledger_entries", "_clock", "_routing_anchors",
        "_pending_calls",
    }.issubset(_table_names(conn))
    n_bu = conn.execute("SELECT count(*) c FROM _business_units").fetchone()["c"]
    n_zones = conn.execute("SELECT count(*) c FROM _zones").fetchone()["c"]
    assert (n_bu, n_zones) == (3, 10)


def test_tampered_checksum_aborts(conn, tmp_path):
    migrate(conn)
    dst = _copy_schema_dir(tmp_path)
    baseline = dst / "0001_baseline.sql"
    baseline.write_text(baseline.read_text() + "\n-- tampered after the fact\n")
    with pytest.raises(EngineError) as ei:
        migrate(conn, migrations_path=dst)
    assert ei.value.code is ErrorCode.MIGRATION_CHECKSUM_MISMATCH
    assert applied_versions(conn) == [
        (1, "baseline"), (2, "routing_anchors"), (3, "demand_primitives"),
        (4, "day_state"), (5, "booking_columns"), (6, "call_arrival"),
        (7, "idempotency_fingerprint"), (8, "job_outcomes"),
    ]


def test_missing_migration_file_for_applied_version_aborts(conn, tmp_path):
    migrate(conn)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(EngineError) as ei:
        migrate(conn, migrations_path=empty)
    assert ei.value.code is ErrorCode.MIGRATION_CHECKSUM_MISMATCH


def test_custom_path_applies(conn, tmp_path):
    dst = _copy_schema_dir(tmp_path)
    assert migrate(conn, migrations_path=dst) == [
        "0001_baseline", "0002_routing_anchors", "0003_demand_primitives",
        "0004_day_state", "0005_booking_columns", "0006_call_arrival",
        "0007_idempotency_fingerprint", "0008_job_outcomes",
    ]
    assert "_customers" in _table_names(conn)


def test_apply_is_atomic_on_failure(conn, tmp_path):
    dst = _copy_schema_dir(tmp_path)
    (dst / "0009_break.sql").write_text(
        "CREATE TABLE _break_table (id INTEGER PRIMARY KEY);\n"
        "THIS IS NOT VALID SQL;\n"
    )
    with pytest.raises(EngineError) as ei:
        migrate(conn, migrations_path=dst)
    assert ei.value.code is ErrorCode.MIGRATION_FAILED
    assert applied_versions(conn) == [
        (1, "baseline"), (2, "routing_anchors"), (3, "demand_primitives"),
        (4, "day_state"), (5, "booking_columns"), (6, "call_arrival"),
        (7, "idempotency_fingerprint"), (8, "job_outcomes"),
    ]
    assert "_break_table" not in _table_names(conn)


def test_applied_at_is_deterministic(conn):
    migrate(conn)
    rows = conn.execute("SELECT applied_at FROM _schema_migrations").fetchall()
    assert rows
    assert all(r[0] == 0 for r in rows)


def test_migrations_path_missing_raises(conn, tmp_path):
    with pytest.raises(EngineError) as ei:
        migrate(conn, migrations_path=tmp_path / "does_not_exist")
    assert ei.value.code is ErrorCode.MIGRATION_FAILED


def test_non_utf8_migration_file_raises_structured_error(conn, tmp_path):
    dst = _copy_schema_dir(tmp_path)
    (dst / "0009_bad.sql").write_bytes(b"CREATE TABLE _t (x);\n-- \xff\xfe latin\n")
    with pytest.raises(EngineError) as ei:
        migrate(conn, migrations_path=dst)
    assert ei.value.code is ErrorCode.MIGRATION_FAILED
    assert applied_versions(conn) == [
        (1, "baseline"), (2, "routing_anchors"), (3, "demand_primitives"),
        (4, "day_state"), (5, "booking_columns"), (6, "call_arrival"),
        (7, "idempotency_fingerprint"), (8, "job_outcomes"),
    ]
    assert "_t" not in _table_names(conn)


def test_migrate_refuses_with_open_transaction(conn):
    conn.execute("BEGIN")
    with pytest.raises(EngineError) as ei:
        migrate(conn)
    assert ei.value.code is ErrorCode.MIGRATION_FAILED
    assert conn.in_transaction
    conn.execute("ROLLBACK")
    assert migrate(conn) == [
        "0001_baseline", "0002_routing_anchors", "0003_demand_primitives",
        "0004_day_state", "0005_booking_columns", "0006_call_arrival",
        "0007_idempotency_fingerprint", "0008_job_outcomes",
    ]


def test_malformed_filename_raises(conn, tmp_path):
    dst = _copy_schema_dir(tmp_path)
    (dst / "not_a_migration.txt").write_text("x")
    with pytest.raises(EngineError) as ei:
        migrate(conn, migrations_path=dst)
    assert ei.value.code is ErrorCode.MIGRATION_FAILED
