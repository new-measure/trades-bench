from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from trades_bench.db import in_memory
from trades_bench.errors import EngineError, ErrorCode
from trades_bench.migrations import MIGRATIONS_PACKAGE_DIR, applied_versions, migrate

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, "baseline"),
    (2, "routing_anchors"),
    (3, "demand_primitives"),
    (4, "day_state"),
    (5, "booking_columns"),
    (6, "call_arrival"),
    (7, "idempotency_fingerprint"),
    (8, "job_outcomes"),
    (9, "drop_attempt_count"),
    (10, "prospect_registration"),
    (11, "run_score_and_invocation_origin"),
    (12, "address_pool_indexes"),
)

UNUSED_VERSION = max(version for version, _name in MIGRATIONS) + 1


def _versions(*, without: frozenset[int] = frozenset()) -> list[tuple[int, str]]:
    return [(v, n) for v, n in MIGRATIONS if v not in without]


def _names(*, without: frozenset[int] = frozenset()) -> list[str]:
    return [f"{v:04d}_{n}" for v, n in _versions(without=without)]


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
    assert migrate(conn) == _names()
    assert migrate(conn) == []
    assert applied_versions(conn) == _versions()
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
    assert applied_versions(conn) == _versions()


def test_missing_migration_file_for_applied_version_aborts(conn, tmp_path):
    migrate(conn)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(EngineError) as ei:
        migrate(conn, migrations_path=empty)
    assert ei.value.code is ErrorCode.MIGRATION_CHECKSUM_MISMATCH


def test_custom_path_applies(conn, tmp_path):
    dst = _copy_schema_dir(tmp_path)
    assert migrate(conn, migrations_path=dst) == _names()
    assert "_customers" in _table_names(conn)


def test_apply_is_atomic_on_failure(conn, tmp_path):
    dst = _copy_schema_dir(tmp_path)
    (dst / f"{UNUSED_VERSION:04d}_break.sql").write_text(
        "CREATE TABLE _break_table (id INTEGER PRIMARY KEY);\n"
        "THIS IS NOT VALID SQL;\n"
    )
    with pytest.raises(EngineError) as ei:
        migrate(conn, migrations_path=dst)
    assert ei.value.code is ErrorCode.MIGRATION_FAILED
    assert applied_versions(conn) == _versions()
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
    (dst / f"{UNUSED_VERSION:04d}_bad.sql").write_bytes(b"CREATE TABLE _t (x);\n-- \xff\xfe latin\n")
    with pytest.raises(EngineError) as ei:
        migrate(conn, migrations_path=dst)
    assert ei.value.code is ErrorCode.MIGRATION_FAILED
    assert applied_versions(conn) == _versions()
    assert "_t" not in _table_names(conn)


def test_migrate_refuses_with_open_transaction(conn):
    conn.execute("BEGIN")
    with pytest.raises(EngineError) as ei:
        migrate(conn)
    assert ei.value.code is ErrorCode.MIGRATION_FAILED
    assert conn.in_transaction
    conn.execute("ROLLBACK")
    assert migrate(conn) == _names()


def test_migrating_old_schema_db_adds_prospect_registration_column_and_visibility_row(conn, tmp_path):
    dst = _copy_schema_dir(tmp_path)
    prospect_migration = dst / "0010_prospect_registration.sql"
    original_content = prospect_migration.read_text()
    prospect_migration.unlink()

    assert migrate(conn, migrations_path=dst) == _names(without=frozenset({10}))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(_prospect_state)").fetchall()}
    assert "registered_customer_id" not in columns
    assert conn.execute(
        'SELECT 1 FROM _visibility_registry WHERE "table" = ? AND "column" = ?',
        ("_prospect_state", "registered_customer_id"),
    ).fetchone() is None

    prospect_migration.write_text(original_content)
    assert migrate(conn, migrations_path=dst) == ["0010_prospect_registration"]

    columns_after = {row[1] for row in conn.execute("PRAGMA table_info(_prospect_state)").fetchall()}
    assert "registered_customer_id" in columns_after
    registry_row = conn.execute(
        'SELECT visibility_class FROM _visibility_registry WHERE "table" = ? AND "column" = ?',
        ("_prospect_state", "registered_customer_id"),
    ).fetchone()
    assert registry_row["visibility_class"] == "private_engine"


def test_migrating_old_schema_db_backfills_registered_customer_id_from_prior_calls(conn, tmp_path):
    dst = _copy_schema_dir(tmp_path)
    prospect_migration = dst / "0010_prospect_registration.sql"
    original_content = prospect_migration.read_text()
    prospect_migration.unlink()

    migrate(conn, migrations_path=dst)

    conn.execute(
        "INSERT INTO _prospect_state (identity_key, caller_name, caller_phone, segment, "
        "home_zone_id, propensity_micros, last_outcome, competitor_captured_at, created_at, "
        "updated_at) VALUES ('4805550100|Jane Doe', 'Jane Doe', '4805550100', 'residential', "
        "NULL, 1000000, 'rejected', NULL, 0, 0)"
    )
    prospect_id = conn.execute("SELECT id FROM _prospect_state").fetchone()[0]
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at) "
        "VALUES (9001, 'Jane Doe', 'residential', 'active', 0, 0)"
    )
    conn.execute(
        "INSERT INTO _calls (arrived_at, service_date, call_kind, status, caller_name, "
        "caller_phone, segment, customer_id, requested_service_name, urgent, created_at) "
        "VALUES (0, '2026-10-01', 'new_job', 'booked', 'Jane Doe', '4805550100', NULL, 9001, "
        "'HVAC service', 0, 0)"
    )
    call_id = conn.execute("SELECT id FROM _calls").fetchone()[0]
    conn.execute(
        "INSERT INTO _call_hidden (call_id, true_segment, requested_category_id, prospect_id, "
        "created_at) VALUES (?, 'residential', 1, ?, 0)",
        (call_id, prospect_id),
    )

    prospect_migration.write_text(original_content)
    assert migrate(conn, migrations_path=dst) == ["0010_prospect_registration"]

    row = conn.execute(
        "SELECT registered_customer_id FROM _prospect_state WHERE id = ?", (prospect_id,)
    ).fetchone()
    assert row["registered_customer_id"] == 9001


def test_migrating_old_schema_db_adds_run_score_and_invocation_origin(conn, tmp_path):
    dst = _copy_schema_dir(tmp_path)
    origin_migration = dst / "0011_run_score_and_invocation_origin.sql"
    original_content = origin_migration.read_text()
    origin_migration.unlink()

    assert migrate(conn, migrations_path=dst) == _names(without=frozenset({11}))
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(_run)").fetchall()}
    assert {"final_score_cents", "track", "attempt_definition"}.isdisjoint(run_columns)
    invocation_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(_invocations)").fetchall()
    }
    assert "origin_event_id" not in invocation_columns
    conn.executemany(
        "INSERT INTO _events "
        "(event_id, kind, scheduled_at_ms, priority, status, created_at_ms, applied_at_ms) "
        "VALUES (?, ?, ?, ?, 'applied', ?, ?)",
        [
            (101, "opening_invocation", 100, 900, 100, 100),
            (102, "call_invocation", 200, 910, 200, 200),
        ],
    )
    conn.executemany(
        "INSERT INTO _invocations "
        "(invocation_id, type, ts_ms, sequence, status, prompt_version) "
        "VALUES (?, ?, ?, ?, ?, NULL)",
        [
            (201, "opening", 100, 1, "completed"),
            (202, "call", 200, 2, "active"),
        ],
    )

    origin_migration.write_text(original_content)
    assert migrate(conn, migrations_path=dst) == ["0011_run_score_and_invocation_origin"]

    run_columns_after = {row[1] for row in conn.execute("PRAGMA table_info(_run)").fetchall()}
    assert {"final_score_cents", "track", "attempt_definition"}.issubset(run_columns_after)
    invocation_columns_after = {
        row[1] for row in conn.execute("PRAGMA table_info(_invocations)").fetchall()
    }
    assert "origin_event_id" in invocation_columns_after
    assert [
        (row["invocation_id"], row["origin_event_id"])
        for row in conn.execute(
            "SELECT invocation_id, origin_event_id FROM _invocations ORDER BY sequence"
        )
    ] == [(201, 101), (202, 102)]
    registered = dict(
        conn.execute(
            'SELECT "table" || \'.\' || "column" AS field, visibility_class '
            'FROM _visibility_registry WHERE ("table" = \'_run\' AND "column" IN '
            "('final_score_cents', 'track', 'attempt_definition')) "
            'OR ("table" = \'_invocations\' AND "column" = \'origin_event_id\')'
        ).fetchall()
    )
    assert registered == {
        "_run.final_score_cents": "post_run_only",
        "_run.track": "post_run_only",
        "_run.attempt_definition": "post_run_only",
        "_invocations.origin_event_id": "post_run_only",
    }


def test_invocation_origin_migration_rejects_unmatched_legacy_history(conn, tmp_path):
    dst = _copy_schema_dir(tmp_path)
    origin_migration = dst / "0011_run_score_and_invocation_origin.sql"
    original_content = origin_migration.read_text()
    origin_migration.unlink()
    migrate(conn, migrations_path=dst)
    conn.execute(
        "INSERT INTO _invocations (type, ts_ms, sequence, status, prompt_version) "
        "VALUES ('call', 100, 1, 'active', NULL)"
    )
    origin_migration.write_text(original_content)
    with pytest.raises(EngineError) as exc_info:
        migrate(conn, migrations_path=dst)
    assert exc_info.value.code is ErrorCode.MIGRATION_FAILED
    assert "origin_event_id" not in {
        row[1] for row in conn.execute("PRAGMA table_info(_invocations)").fetchall()
    }


def test_malformed_filename_raises(conn, tmp_path):
    dst = _copy_schema_dir(tmp_path)
    (dst / "not_a_migration.txt").write_text("x")
    with pytest.raises(EngineError) as ei:
        migrate(conn, migrations_path=dst)
    assert ei.value.code is ErrorCode.MIGRATION_FAILED
