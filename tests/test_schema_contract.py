from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from _helpers import GEO_ARTIFACT_DIR, geo_run_config
from csr_bench import clock, db, views
from csr_bench.clock import phoenix_instant
from csr_bench.db import in_memory
from csr_bench.migrations import migrate
from csr_bench.run_init import initialize_run
from csr_bench.views import VIEW_WHITELIST, VIEWS

GOLDEN_DIR = Path(__file__).parent / "golden"
GOLDEN_SCHEMA = GOLDEN_DIR / "schema_0001.sql"

_HEADER = """\
-- Golden schema dump of the migrated CSR-Bench database.
--
-- Regenerate deliberately with:
--   REGEN_GOLDEN_SCHEMA=1 uv run pytest tests/test_schema_contract.py -q
--
-- Format: the canonical sqlite_master dump of a freshly migrated database.
-- Entries are ordered (type groups table / index / trigger, names sorted
-- within a type) and each carries its stored SQL text verbatim; objects
-- without stored SQL text (SQLite's implicit auto-indexes for UNIQUE
-- constraints) are marked. Views are excluded because migrations never
-- create them. A schema change makes the compare test fail; regenerating
-- the file is the deliberate acceptance of the new schema.
"""

_SKIP_PREFIX = "sqlite_"


def canonical_schema_dump(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger') AND name NOT LIKE ? "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name",
        (_SKIP_PREFIX + "%",),
    ).fetchall()
    blocks: list[str] = []
    for row in rows:
        if row["sql"] is None:
            body = "-- (implicit object created by a table definition; no stored SQL text)"
        else:
            body = row["sql"]
        blocks.append(f"-- {row['type']}: {row['name']}\n{body}")
    return _HEADER + "\n\n".join(blocks) + "\n"


def _migrated() -> sqlite3.Connection:
    conn = in_memory()
    migrate(conn)
    return conn


@pytest.fixture
def conn():
    c = _migrated()
    try:
        yield c
    finally:
        c.close()


def test_regen_golden_schema():
    if os.environ.get("REGEN_GOLDEN_SCHEMA") != "1":
        pytest.skip(
            "set REGEN_GOLDEN_SCHEMA=1 to deliberately rewrite the golden schema file"
        )
    c = _migrated()
    try:
        text = canonical_schema_dump(c)
    finally:
        c.close()
    GOLDEN_DIR.mkdir(exist_ok=True)
    GOLDEN_SCHEMA.write_text(text)


def test_migrated_schema_matches_the_golden_dump(conn):
    assert GOLDEN_SCHEMA.is_file(), (
        "golden schema file missing — run REGEN_GOLDEN_SCHEMA=1 "
        "uv run pytest tests/test_schema_contract.py -q"
    )
    assert canonical_schema_dump(conn) == GOLDEN_SCHEMA.read_text()


def test_migrated_database_contains_no_views(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'"
    ).fetchall()
    assert rows == []


def test_every_base_table_name_starts_with_underscore(conn):
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert names
    assert all(name.startswith("_") for name in names)


def test_no_view_name_starts_with_underscore(conn):
    views.create_views(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert names == set(VIEW_WHITELIST)
    assert all(not name.startswith("_") for name in names)


def test_whitelist_and_base_tables_share_no_names(conn):
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert VIEW_WHITELIST.isdisjoint(tables)


_TIME_COLUMNS: dict[str, str] = {
    spec.name: {
        "calls": "arrived_at",
        "technician_locations": "coordinates_updated_at",
        "training_rates": "rate_date",
        "company_state": "as_of_ms",
    }.get(spec.name, "created_at")
    for spec in VIEWS
}


def assert_no_view_row_past_the_clock(conn: sqlite3.Connection, clock_ms: int, service_date: str) -> None:
    for spec in VIEWS:
        column = _TIME_COLUMNS[spec.name]
        rows = conn.execute(f"SELECT {column} AS t FROM {spec.name}").fetchall()
        for row in rows:
            value = row["t"]
            if spec.name == "training_rates":
                assert value <= service_date, f"{spec.name}: future {column} {value}"
            else:
                assert value <= clock_ms, f"{spec.name}: {column} {value} > clock {clock_ms}"


def test_sch04_sweep_every_public_view_mid_day(tmp_path):
    db_path = tmp_path / "run.db"
    initialize_run(db_path, master_seed=1, config=geo_run_config(), artifact_dir=GEO_ARTIFACT_DIR)
    conn = db.connect(db_path)
    try:
        noon = phoenix_instant(2026, 10, 1, 12, 0)
        db.begin(conn)
        clock.advance(conn, noon, "2026-10-01")
        db.commit(conn)
        assert_no_view_row_past_the_clock(conn, noon, "2026-10-01")
    finally:
        conn.close()


def test_sch04_agent_created_future_appointment_stays_visible(tmp_path):
    db_path = tmp_path / "run.db"
    initialize_run(db_path, master_seed=1, config=geo_run_config(), artifact_dir=GEO_ARTIFACT_DIR)
    conn = db.connect(db_path)
    try:
        noon = phoenix_instant(2026, 10, 1, 12, 0)
        db.begin(conn)
        clock.advance(conn, noon, "2026-10-01")
        db.commit(conn)
        conn.execute(
            "INSERT INTO _customers (id, name, segment, status, created_at, modified_at) "
            "VALUES (100, 'Acme', 'commercial', 'active', ?, ?)",
            (noon - 7_200_000, noon - 7_200_000),
        )
        conn.execute(
            "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, "
            "postal_code, latitude, longitude, active, created_at, modified_at, "
            "effective_zone_id) VALUES (100, 100, 'HQ', '1 Main St', '', 'Phoenix', 'AZ', "
            "'85001', 33.5, -112.0, 1, ?, ?, 1)",
            (noon - 7_200_000, noon - 7_200_000),
        )
        conn.execute(
            "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
            "caller_name, caller_phone, requested_service_name, urgent, created_at) "
            "VALUES (100, ?, '2026-10-01', 'new_job', 'booked', 'A', '555', "
            "'HVAC service', 0, ?)",
            (noon - 7_200_000, noon - 7_200_000),
        )
        conn.execute(
            "INSERT INTO _jobs (id, job_number, call_id, customer_id, location_id, "
            "job_type_id, requested_service_name, status, priority, no_charge, summary, "
            "created_at, modified_at) VALUES (100, 'J-100', 100, 100, 100, 1, "
            "'HVAC service', 'scheduled', 'normal', 0, 'x', ?, ?)",
            (noon - 7_200_000, noon - 7_200_000),
        )
        conn.execute(
            "INSERT INTO _appointments (id, appointment_number, job_id, status, "
            "scheduled_start, scheduled_end, arrival_window_start, arrival_window_end, "
            "actual_start, actual_end, cancelled_at, created_at, modified_at) "
            "VALUES (100, 'A-100', 100, 'scheduled', ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
            (
                noon + 7_200_000, noon + 14_400_000,
                noon + 7_200_000, noon + 14_400_000,
                noon - 7_200_000, noon - 7_200_000,
            ),
        )
        rows = conn.execute("SELECT id FROM appointments WHERE id = 100").fetchall()
        assert [r["id"] for r in rows] == [100]
        assert_no_view_row_past_the_clock(conn, noon, "2026-10-01")
    finally:
        conn.close()
