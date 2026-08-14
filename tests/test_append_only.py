from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from csr_bench.db import in_memory
from csr_bench.migrations import migrate

APPEND_ONLY_TABLES: tuple[str, ...] = (
    "_ledger_entries",
    "_notifications",
    "_call_attempts",
    "_reviews",
    "_random_audit",
    "_appointment_events",
)

_SOURCE_FILES: tuple[Path, ...] = tuple(
    sorted(Path(__file__).resolve().parents[1].joinpath("src", "csr_bench").glob("*.py"))
) + (Path(__file__).resolve().parent / "_driver.py",)

_DML_TARGET = re.compile(
    r"\b(?:UPDATE|DELETE)\s+(?:(?:FROM|/\*[^*]*\*/)\s+)*[\"'`]?(_[A-Za-z0-9_]+)[\"'`]?",
    re.IGNORECASE,
)

_EVENTS_SET = re.compile(
    r"\bUPDATE\s+(?:/\*[^*]*\*/\s*)*[\"'`]?_events[\"'`]?\s+SET\s+(.*?)(?:WHERE|$)",
    re.IGNORECASE | re.DOTALL,
)

_EVENTS_ALLOWED_COLUMNS = frozenset({"status", "applied_at_ms"})

_SQLITE_COLUMN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _dml_targets(source: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for match in _DML_TARGET.finditer(source):
        statement = source[match.start():]
        out.append((match.group(1), statement[: len(match.group(0)) + 120]))
    return out


def test_no_source_updates_or_deletes_an_append_only_table():
    for path in _SOURCE_FILES:
        source = path.read_text()
        for table, _ in _dml_targets(source):
            assert table not in APPEND_ONLY_TABLES, (
                f"{path.name}: {table} is append-only — {table} in a "
                f"UPDATE/DELETE statement"
            )


def test_events_carve_out_is_exactly_events_py_status_and_applied_at():
    for path in _SOURCE_FILES:
        source = path.read_text()
        for table, statement in _dml_targets(source):
            if table != "_events":
                continue
            assert path.name == "events.py", (
                f"{path.name}: only events.py may update _events"
            )
            set_match = _EVENTS_SET.search(statement)
            assert set_match is not None, f"{path.name}: malformed _events UPDATE"
            columns = {
                m.group(1) for m in _SQLITE_COLUMN.finditer(set_match.group(1))
            }
            assert columns <= _EVENTS_ALLOWED_COLUMNS, (
                f"{path.name}: _events SET touches {columns - _EVENTS_ALLOWED_COLUMNS}; "
                f"only {_EVENTS_ALLOWED_COLUMNS}"
            )


def test_static_scan_catches_quoted_and_comment_adjacent_dml_spellings():
    synthetic = (
        "UPDATE \"_events\" SET status = 'applied'\n"
        "UPDATE /*c*/ _events SET status = 'processing'\n"
        "UPDATE `_events` SET status = 'cancelled'\n"
        "DELETE FROM \"_ledger_entries\"\n"
        "DELETE /*c*/ FROM _reviews\n"
        "UPDATE _invocations SET status = 'completed'\n"
    )
    targets = _dml_targets(synthetic)
    assert [table for table, _ in targets] == [
        "_events",
        "_events",
        "_events",
        "_ledger_entries",
        "_reviews",
        "_invocations",
    ]
    for table, statement in targets:
        if table != "_events":
            continue
        set_match = _EVENTS_SET.search(statement)
        assert set_match is not None, f"synthetic spelling missed by _EVENTS_SET: {statement!r}"
        columns = {m.group(1) for m in _SQLITE_COLUMN.finditer(set_match.group(1))}
        assert columns == {"status"}, f"{statement!r}: SET {columns}"
    caught = {table for table, _ in targets}
    assert {"_ledger_entries", "_reviews"} <= caught


@pytest.fixture
def conn():
    c = in_memory()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _parents(conn) -> None:
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at) "
        "VALUES (1, 'Acme', 'commercial', 'active', 0, 0)"
    )
    conn.execute(
        "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, "
        "postal_code, latitude, longitude, active, created_at, modified_at, "
        "effective_zone_id) VALUES (1, 1, 'HQ', '1 Main', '', 'Phoenix', 'AZ', "
        "'85001', 33.5, -112.0, 1, 0, 0, 1)"
    )
    conn.execute(
        "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
        "caller_name, caller_phone, requested_service_name, urgent, created_at) "
        "VALUES (1, 0, '2026-10-01', 'new_job', 'booked', 'A', '555', 'x', 0, 0)"
    )
    conn.execute(
        "INSERT INTO _jobs (id, job_number, call_id, customer_id, location_id, "
        "job_type_id, requested_service_name, status, priority, no_charge, summary, "
        "created_at, modified_at) VALUES (1, 'J-1', 1, 1, 1, 1, 'x', 'scheduled', "
        "'normal', 0, 'x', 0, 0)"
    )
    conn.execute(
        "INSERT INTO _appointments (id, appointment_number, job_id, status, "
        "scheduled_start, scheduled_end, arrival_window_start, arrival_window_end, "
        "created_at, modified_at) VALUES (1, 'A-1', 1, 'scheduled', 0, 0, 0, 0, 0, 0)"
    )


_ONE_ROW: dict[str, tuple[str, str]] = {
    "_ledger_entries": (
        "INSERT INTO _ledger_entries (id, occurred_at, service_date, entry_type, "
        "amount_cents, balance_after_cents, description, created_at) "
        "VALUES (1, 0, '2026-10-01', 'revenue', 100, 100, 'x', 0)",
        "UPDATE _ledger_entries SET description = 'x'",
    ),
    "_notifications": (
        "INSERT INTO _notifications (id, created_at, service_date, notification_type, "
        "entity_type, entity_id, message) VALUES (1, 0, '2026-10-01', 'x', 'x', 1, 'm')",
        "UPDATE _notifications SET message = 'x'",
    ),
    "_call_attempts": (
        "INSERT INTO _call_attempts (id, call_id, attempt_number, prompted_at, "
        "outcome, created_at) VALUES (1, 1, 1, 0, 'incomplete', 0)",
        "UPDATE _call_attempts SET outcome = 'resolved'",
    ),
    "_reviews": (
        "INSERT INTO _reviews (id, job_id, appointment_id, customer_id, technician_id, "
        "published_at, service_date, stars, source, created_at) VALUES "
        "(1, NULL, NULL, 1, NULL, 0, '2026-10-01', 5, 'google', 0)",
        "UPDATE _reviews SET stars = 1",
    ),
    "_random_audit": (
        "INSERT INTO _random_audit (seq, stream_name, entity_type, entity_id, scope, "
        "event_kind, draw_index, outcome_id, simulated_ts_ms) "
        "VALUES (1, 's', 'e', 1, 's', 'k', 0, NULL, 0)",
        "UPDATE _random_audit SET scope = 'x'",
    ),
    "_appointment_events": (
        "INSERT INTO _appointment_events (id, appointment_id, technician_id, "
        "event_type, occurred_at, service_date, detail, created_at) "
        "VALUES (1, 1, NULL, 'dispatched', 0, '2026-10-01', 'd', 0)",
        "UPDATE _appointment_events SET detail = 'x'",
    ),
}


def test_update_and_delete_trigger_fires_on_every_append_only_table(conn):
    _parents(conn)
    for table, (insert_sql, update_sql) in _ONE_ROW.items():
        conn.execute(insert_sql)
        for statement in (update_sql, f"DELETE FROM {table}"):
            with pytest.raises(sqlite3.IntegrityError, match="append_only_table"):
                conn.execute(statement)


def test_events_carve_out_update_still_works(conn):
    conn.execute(
        "INSERT INTO _events (event_id, kind, scheduled_at_ms, priority, status, "
        "created_at_ms) VALUES (1, 'contract_test', 0, 0, 'pending', 0)"
    )
    conn.execute("UPDATE _events SET status = 'applied', applied_at_ms = 5")
    row = conn.execute("SELECT status, applied_at_ms FROM _events WHERE event_id = 1").fetchone()
    assert (row["status"], row["applied_at_ms"]) == ("applied", 5)
    triggers = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='_events'"
    ).fetchall()
    assert triggers == []
