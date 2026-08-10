"""SCH-01 golden schema dump, SCH-03 underscore rule, and the SCH-04 full
clock sweep (spec 02 §9/§13, §1.5; CSR-002 decisions 1, 16, 3).

SCH-01 (decision 16): the migrated schema must match a checked-in golden
file byte for byte. The golden holds the canonical ``sqlite_master`` dump of
the *migrated* database — tables, indexes, triggers — never views, because
migrations never create them (decision 1: views come from the registry at
open only). The no-op-reapply and tampered-checksum cases live in
``test_migrations.py`` and are deliberately not duplicated here.

Regeneration: ``REGEN_GOLDEN_SCHEMA=1 uv run pytest tests/test_schema_contract.py -q``
— the :func:`test_regen_golden_schema` skip-guarded test rewrites
``tests/golden/schema_0001.sql`` from the current migrations, and the compare
test then passes against the fresh file. A migration change fails the compare
first; regenerating is the deliberate act of accepting the new schema.

SCH-03 (decision 2): the static underscore rule — every base table name
starts with ``_``, no view name does, and the whitelist shares no name with a
base table (feeds VIS-01/VIS-05).

SCH-04 (decision 3): sweeping every public view mid-day returns no row whose
creation/arrival timestamp exceeds the frozen clock (spec 02 §1.5: "filter on
creation/arrival time, never on scheduled time"), while a future *scheduled*
appointment the agent created stays visible. ``test_views.py`` covers the
calls/appointments/training_rates core on a hand-built database; this file
owns the full 31-view sweep on the real warm-started fixture database, and
the VIS-07 test imports the same sweep (written once, spec 09 §VIS-07(c)).
The "agent-created future appointment" half is CSR-006-deferred as an
SDK/booking operation (decision 18); its mechanism — creation-time
filtering — is directly testable today by inserting an appointment with a
past ``created_at`` and a future ``scheduled_start``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from csr_bench import clock, db, views
from csr_bench.clock import phoenix_instant
from csr_bench.config import load_run_config
from csr_bench.db import in_memory
from csr_bench.migrations import migrate
from csr_bench.run_init import initialize_run
from csr_bench.views import VIEW_WHITELIST, VIEWS

GOLDEN_DIR = Path(__file__).parent / "golden"
GOLDEN_SCHEMA = GOLDEN_DIR / "schema_0001.sql"

_HEADER = """\
-- Golden schema dump of the migrated CSR-Bench database (spec 02 §9; CSR-002
-- decision 16).
--
-- Regenerate deliberately with:
--   REGEN_GOLDEN_SCHEMA=1 uv run pytest tests/test_schema_contract.py -q
--
-- Format: the canonical sqlite_master dump of a freshly migrated database.
-- Entries are ordered (type groups table / index / trigger, names sorted
-- within a type) and each carries its stored SQL text verbatim; objects
-- without stored SQL text (SQLite's implicit auto-indexes for UNIQUE
-- constraints) are marked. Views are excluded because migrations never create
-- them (CSR-002 decision 1). A schema change makes the compare test fail;
-- regenerating the file is the deliberate acceptance of the new schema.
"""

#: SQLite's internal tables/objects; not part of the schema contract.
_SKIP_PREFIX = "sqlite_"


def canonical_schema_dump(conn: sqlite3.Connection) -> str:
    """The canonical ``sqlite_master`` dump of the migrated schema.

    Tables, indexes, and triggers ordered by (type group, name); each entry is
    the stored ``sql`` text verbatim, or a marker for the implicit auto-index
    rows that carry ``sql = NULL``. The output is a pure function of the
    migrated database — the byte-for-byte comparison token for SCH-01.
    """
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


# ---------------------------------------------------------------------------
# SCH-01 — the golden schema dump (decision 16)
# ---------------------------------------------------------------------------


def test_regen_golden_schema():
    """The skip-guarded regeneration path.

    Run with ``REGEN_GOLDEN_SCHEMA=1`` to rewrite the golden file from the
    current migrations. Normal runs skip; the compare test then enforces the
    checked-in file.
    """
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
    # SCH-01 core: a fresh migrated database dumps byte-identically to the
    # checked-in golden file. No-op reapply and tampered-checksum cases live
    # in test_migrations.py.
    assert GOLDEN_SCHEMA.is_file(), (
        "golden schema file missing — run REGEN_GOLDEN_SCHEMA=1 "
        "uv run pytest tests/test_schema_contract.py -q"
    )
    assert canonical_schema_dump(conn) == GOLDEN_SCHEMA.read_text()


def test_migrated_database_contains_no_views(conn):
    # Views come from the registry at open, never from migrations
    # (decision 1); a migration that creates a view would bypass the registry
    # and its golden-hash tripwire.
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'"
    ).fetchall()
    assert rows == []


# ---------------------------------------------------------------------------
# SCH-03 — the static underscore rule (decision 2)
# ---------------------------------------------------------------------------


def test_every_base_table_name_starts_with_underscore(conn):
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert names  # the migration applied
    assert all(name.startswith("_") for name in names)


def test_no_view_name_starts_with_underscore(conn):
    # The engine open sequence, then the rule: public view names are plain
    # identifiers, so a `_`-prefixed name in SQL is always a base table
    # (feeds VIS-01's uniform denial).
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


# ---------------------------------------------------------------------------
# SCH-04 — the full clock sweep (spec 02 §1.5; feeds VIS-07(c))
# ---------------------------------------------------------------------------

#: Per-view time column: the §1.5 creation/arrival filter column of each view,
#: by which the sweep judges "no future row". Everything else in a view's
#: row is either a scheduled time (never filtered, by rule) or derived from
#: committed state. Reference views carry created_at = 0 (pre-world sentinel).
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
    """SCH-04 sweep: every public view, every row: the view's creation/arrival
    timestamp must not exceed the frozen clock.

    ``training_rates`` compares its ``rate_date`` text against the clock's
    service date (its §1.5 filter, 02:412); ``company_state.as_of_ms`` equals
    the clock by construction. Spec 02 §1.5: "filter on creation/arrival time,
    never on scheduled time" — so future scheduled starts on appointments are
    legitimate and are not judged here.
    """
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
    # Warm-started fixture (decision 11), clock advanced to noon; every row of
    # every view must be at or before the frozen instant.
    db_path = tmp_path / "run.db"
    initialize_run(db_path, master_seed=1, config=load_run_config())
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
    # Decision 18: the booking-operation half of SCH-04 is CSR-006; its
    # mechanism is creation-time filtering (02:89), directly testable: an
    # appointment created in the past with a future scheduled_start must be
    # visible in the appointments view (never filtered on scheduled time).
    # test_views.py covers the same rule on a hand-built database.
    db_path = tmp_path / "run.db"
    initialize_run(db_path, master_seed=1, config=load_run_config())
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
                noon + 7_200_000, noon + 14_400_000,  # scheduled in the future
                noon + 7_200_000, noon + 14_400_000,
                noon - 7_200_000, noon - 7_200_000,  # created in the past
            ),
        )
        rows = conn.execute("SELECT id FROM appointments WHERE id = 100").fetchall()
        assert [r["id"] for r in rows] == [100]
        # the sweep still holds — the future *schedule* is not a violation
        assert_no_view_row_past_the_clock(conn, noon, "2026-10-01")
    finally:
        conn.close()
