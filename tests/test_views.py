"""Tests for the public-view registry (spec 02 §3.1/§3.2; SCH-02; CSR-002
decisions 6–7): the 31-view whitelist, the per-view column contract, the
golden-hash tripwire, clock filtering (SCH-04), and the `company_state`
projection (spec 02 §4.9).

SCH-02's "recreated views hash-match the registry; a deliberately altered
view definition aborts startup; every whitelist entry exists and every
existing view is on the whitelist (no orphans in either direction)" is
tested directly here: the tamper tests build a *second* registry copy with
one view's SQL changed and prove `verify_views` fails — SQLite's
``sqlite_schema`` is not writable through SQL, so the altered registry, not
an UPDATE on the schema table, is the honest way to produce the stored
definition the hash must reject."""

from __future__ import annotations

import dataclasses

import pytest

from csr_bench import views
from csr_bench.clock import phoenix_instant
from csr_bench.db import in_memory, insert_run
from csr_bench.enums import RunStatus, VisibilityClass
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.migrations import migrate
from csr_bench.views import (
    VIEW_REGISTRY_HASH,
    VIEW_WHITELIST,
    VIEWS,
    compute_view_hash,
    create_views,
    verify_views,
)
from csr_bench.visibility import classify


@pytest.fixture
def conn():
    # The engine's open sequence: migration runner, then the view registry —
    # no other DDL (spec 02 §9: the registry is the one DDL exception).
    c = in_memory()
    migrate(c)
    create_views(c)
    try:
        yield c
    finally:
        c.close()


def _stored_view_names(conn) -> set[str]:
    return {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='view'")
    }


# ---------------------------------------------------------------------------
# The whitelist (spec 02 §3.2) — both directions, no orphans
# ---------------------------------------------------------------------------


def test_exactly_the_31_whitelisted_views_exist(conn):
    stored = _stored_view_names(conn)
    assert len(stored) == 31
    assert stored == set(VIEW_WHITELIST)  # no orphans, none missing
    assert {spec.name for spec in VIEWS} == VIEW_WHITELIST  # registry agrees


def test_whitelist_guard_is_a_real_literal_check(monkeypatch):
    # C8 nit: the guard used to compare the registry against its own derived
    # whitelist — a tautology that could never fire. It must now compare
    # against the literal 31-name set: a 32nd declared view fails validation
    # (spec 02 §3.2: exactly these 31), forcing a deliberate whitelist
    # decision.
    extra = views.ViewSpec(name="rogue_view", sql="CREATE VIEW rogue_view AS SELECT 1", columns=())
    monkeypatch.setattr(views, "VIEWS", views.VIEWS + (extra,))
    with pytest.raises(AssertionError):
        views.validate_registry()
    # a removed view fails the same check (drop the appended rogue AND the
    # registry's last real view, leaving 30)
    monkeypatch.setattr(views, "VIEWS", views.VIEWS[:-2])
    with pytest.raises(AssertionError):
        views.validate_registry()


# ---------------------------------------------------------------------------
# The per-view column contract (spec 02 §3.1)
# ---------------------------------------------------------------------------


def test_each_view_column_list_matches_the_created_view(conn):
    # PRAGMA table_info of the real created view must equal the registry's
    # declared columns, in order — the registry is the single source any
    # consumer cites (spec 02 §3.1).
    for spec in VIEWS:
        pragma = [r["name"] for r in conn.execute(f"PRAGMA table_info({spec.name})")]
        declared = [c.name for c in spec.columns]
        assert pragma == declared, (
            f"{spec.name}: PRAGMA {pragma} != registry {declared}"
        )


def test_registry_classification_rule_holds_for_all_31(conn):
    # Every passthrough resolves through visibility.classify to a public
    # class; every computed column declares a public class explicitly.
    views.validate_registry()
    for spec in VIEWS:
        for col in spec.columns:
            if col.source is not None:
                assert classify(col.source) in VisibilityClass.public(), col
            else:
                assert col.visibility_class in VisibilityClass.public(), col


def test_technician_locations_is_the_five_public_columns(conn):
    # The stored row minus the six `trip_*` columns (decision 18); the PK
    # technician_id leads and is public.
    spec = next(s for s in VIEWS if s.name == "technician_locations")
    assert [c.name for c in spec.columns] == [
        "technician_id",
        "latitude",
        "longitude",
        "coordinates_updated_at",
        "coordinates_updated_at_iso",
        "location_state",
    ]
    assert classify(("_technician_locations", "technician_id")) is VisibilityClass.PUBLIC_SQL


def test_no_private_or_origin_column_reaches_any_view(conn):
    # SCH-08: `origin` and the rule-2 private columns of spec 02 §7 must never
    # be projected (02:58, 02:596). `_customers.modified_at` joins them via
    # the §4.1 exact-set exception (final-branch audit F1): it classifies
    # private and is not part of the customers view's six-tuple.
    private = {
        "_customers": {"origin", "modified_at"},
        "_locations": {"origin", "effective_zone_id", "address_catalog_id"},
        "_calls": {"origin", "address_catalog_id"},
        "_technician_locations": {
            "trip_origin_latitude",
            "trip_origin_longitude",
            "trip_destination_latitude",
            "trip_destination_longitude",
            "trip_departed_at",
            "trip_travel_minutes",
        },
    }
    for spec in VIEWS:
        for col in spec.columns:
            if col.source is not None and col.source.table in private:
                assert col.name not in private[col.source.table], (
                    f"{spec.name} projects private {col.source}"
                )


# ---------------------------------------------------------------------------
# The golden-hash tripwire (CSR-002 decision 6; SCH-02)
# ---------------------------------------------------------------------------


def test_compute_view_hash_matches_the_golden_constant(conn):
    # The core SCH-02 assertion: the stored definitions hash to the captured
    # golden constant. Changing any view SQL requires regenerating the
    # constant — this test fails otherwise.
    assert compute_view_hash(conn) == VIEW_REGISTRY_HASH
    assert len(VIEW_REGISTRY_HASH) == 64


def test_create_views_is_idempotent(conn):
    h_before = compute_view_hash(conn)
    create_views(conn)  # drop + recreate — must not error or change anything
    assert compute_view_hash(conn) == h_before
    assert _stored_view_names(conn) == set(VIEW_WHITELIST)


def test_altered_view_definition_trips_the_hash_guard(monkeypatch):
    # sqlite_schema is read-only through SQL, so the tamper is modelled the
    # honest way: a second registry copy with one view's SQL changed, built
    # into a fresh database. The set check passes (same 31 names); the hash
    # check must abort with VIEW_REGISTRY_MISMATCH (spec 02 §3.1: "A mismatch
    # is a startup failure, not a warning").
    calls = next(s for s in VIEWS if s.name == "calls")
    tampered = dataclasses.replace(
        calls,
        sql=calls.sql.replace(
            "c.arrived_at <= k.current_time_ms",
            "c.arrived_at < k.current_time_ms",
        ),
    )
    monkeypatch.setattr(
        views, "VIEWS", tuple(tampered if s.name == "calls" else s for s in VIEWS)
    )
    c = in_memory()
    migrate(c)
    try:
        create_views(c)
        with pytest.raises(EngineError) as ei:
            verify_views(c)
        assert ei.value.code is ErrorCode.VIEW_REGISTRY_MISMATCH
    finally:
        c.close()


def test_orphan_or_missing_view_fails_the_set_check(conn):
    # SCH-02: no orphans in either direction — an extra view and a dropped
    # view both fail, exactly like the hash mismatch.
    conn.execute("CREATE VIEW rogue AS SELECT 1")
    with pytest.raises(EngineError) as ei:
        verify_views(conn)
    assert ei.value.code is ErrorCode.VIEW_REGISTRY_MISMATCH
    assert "orphans" in ei.value.message

    conn.execute("DROP VIEW rogue")
    conn.execute("DROP VIEW calls")
    with pytest.raises(EngineError) as ei:
        verify_views(conn)
    assert ei.value.code is ErrorCode.VIEW_REGISTRY_MISMATCH
    assert "missing" in ei.value.message


# ---------------------------------------------------------------------------
# Clock filtering (spec 02 §1.5; SCH-04)
# ---------------------------------------------------------------------------


def test_clock_filtering_calls_appointments_training_rates(conn):
    noon = phoenix_instant(2026, 10, 1, 12, 0)
    conn.execute(
        "INSERT INTO _clock (singleton, current_time_ms, service_date) "
        "VALUES (1, ?, ?)",
        (noon, "2026-10-01"),
    )

    # calls: filtered on ARRIVAL time — the future call has no row the agent
    # may see (02:89, 02:269). The ISO sibling renders Phoenix-local.
    conn.execute(
        "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
        "caller_name, caller_phone, requested_service_name, urgent, created_at) "
        "VALUES (1, ?, '2026-10-01', 'new_job', 'active', 'A', '555', "
        "'HVAC service', 0, ?)",
        (noon - 3_600_000, noon - 3_600_000),
    )
    conn.execute(
        "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
        "caller_name, caller_phone, requested_service_name, urgent, created_at) "
        "VALUES (2, ?, '2026-10-01', 'new_job', 'active', 'B', '556', "
        "'Plumbing', 0, ?)",
        (noon + 3_600_000, noon + 3_600_000),
    )
    rows = conn.execute(
        "SELECT id, arrived_at_iso FROM calls ORDER BY id"
    ).fetchall()
    assert [r["id"] for r in rows] == [1]
    assert rows[0]["arrived_at_iso"] == "2026-10-01T11:00:00-07:00"

    # appointments: filtered on CREATION time — an appointment created in the
    # past stays visible even though it is scheduled in the future; one
    # created in the future is invisible whatever its schedule (SCH-04).
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, "
        "modified_at) VALUES (1, 'Acme', 'commercial', 'active', ?, ?)",
        (noon - 7_200_000, noon - 7_200_000),
    )
    conn.execute(
        "INSERT INTO _locations (id, customer_id, name, street, unit, city, "
        "state, postal_code, latitude, longitude, active, created_at, "
        "modified_at, effective_zone_id) "
        "VALUES (1, 1, 'HQ', '1 Main St', '', 'Phoenix', 'AZ', '85001', "
        "33.5, -112.0, 1, ?, ?, 1)",
        (noon - 7_200_000, noon - 7_200_000),
    )
    conn.execute(
        "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
        "caller_name, caller_phone, requested_service_name, urgent, created_at) "
        "VALUES (3, ?, '2026-10-01', 'new_job', 'booked', 'A', '555', "
        "'HVAC service', 0, ?)",
        (noon - 7_200_000, noon - 7_200_000),
    )
    conn.execute(
        "INSERT INTO _jobs (id, job_number, call_id, customer_id, location_id, "
        "job_type_id, requested_service_name, status, priority, no_charge, "
        "summary, created_at, modified_at) "
        "VALUES (1, 'J-1', 3, 1, 1, 1, 'HVAC service', 'scheduled', 'normal', "
        "0, 'x', ?, ?)",
        (noon - 7_200_000, noon - 7_200_000),
    )
    conn.execute(
        "INSERT INTO _appointments (id, appointment_number, job_id, status, "
        "scheduled_start, scheduled_end, arrival_window_start, "
        "arrival_window_end, actual_start, actual_end, cancelled_at, "
        "created_at, modified_at) "
        "VALUES (1, 'A-1', 1, 'scheduled', ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
        (
            noon + 7_200_000, noon + 14_400_000,  # future schedule
            noon + 7_200_000, noon + 14_400_000,
            noon - 7_200_000, noon - 7_200_000,  # created in the past
        ),
    )
    conn.execute(
        "INSERT INTO _appointments (id, appointment_number, job_id, status, "
        "scheduled_start, scheduled_end, arrival_window_start, "
        "arrival_window_end, actual_start, actual_end, cancelled_at, "
        "created_at, modified_at) "
        "VALUES (2, 'A-2', 1, 'scheduled', ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
        (
            noon - 7_200_000, noon,  # past schedule
            noon - 7_200_000, noon,
            noon + 3_600_000, noon + 3_600_000,  # created in the future
        ),
    )
    rows = conn.execute("SELECT id FROM appointments ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [1]

    # training_rates: `rate_date <= k.service_date` — tomorrow's rates are
    # unknowable (02:89, 02:412), yesterday's stay queryable.
    conn.execute(
        "INSERT INTO _training_rates (id, skill_code, rate_date, "
        "hourly_rate_cents, created_at) "
        "VALUES (1, 'hvac_service', '2026-09-30', 3500, ?)",
        (noon - 86_400_000,),
    )
    conn.execute(
        "INSERT INTO _training_rates (id, skill_code, rate_date, "
        "hourly_rate_cents, created_at) "
        "VALUES (2, 'hvac_service', '2026-10-02', 3600, ?)",
        (noon,),
    )
    rows = conn.execute("SELECT id FROM training_rates ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [1]


# ---------------------------------------------------------------------------
# ISO sibling rendering gaps (C8): NULL epochs and Phoenix day boundaries
# ---------------------------------------------------------------------------


def test_nullable_epoch_columns_render_null(conn):
    # decision 7's ISO siblings: a NULL epoch column renders NULL, never an
    # empty string or a sentinel date.
    noon = phoenix_instant(2026, 10, 1, 12, 0)
    conn.execute(
        "INSERT INTO _clock (singleton, current_time_ms, service_date) "
        "VALUES (1, ?, ?)",
        (noon, "2026-10-01"),
    )
    conn.execute(
        "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
        "caller_name, caller_phone, requested_service_name, urgent, terminal_at, "
        "terminal_reason, created_at) VALUES (1, ?, '2026-10-01', 'new_job', "
        "'unanswered', 'A', '555', 'HVAC service', 0, NULL, NULL, ?)",
        (noon - 3_600_000, noon - 3_600_000),
    )
    row = conn.execute("SELECT terminal_at, terminal_at_iso FROM calls").fetchone()
    assert row["terminal_at"] is None
    assert row["terminal_at_iso"] is None

    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, "
        "modified_at) VALUES (1, 'Acme', 'commercial', 'active', ?, ?)",
        (noon - 7_200_000, noon - 7_200_000),
    )
    conn.execute(
        "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, "
        "postal_code, latitude, longitude, active, created_at, modified_at, "
        "effective_zone_id) VALUES (1, 1, 'HQ', '1 Main St', '', 'Phoenix', 'AZ', "
        "'85001', 33.5, -112.0, 1, ?, ?, 1)",
        (noon - 7_200_000, noon - 7_200_000),
    )
    conn.execute(
        "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
        "caller_name, caller_phone, requested_service_name, urgent, created_at) "
        "VALUES (2, ?, '2026-10-01', 'new_job', 'booked', 'A', '555', "
        "'HVAC service', 0, ?)",
        (noon - 7_200_000, noon - 7_200_000),
    )
    conn.execute(
        "INSERT INTO _jobs (id, job_number, call_id, customer_id, location_id, "
        "job_type_id, requested_service_name, status, priority, no_charge, summary, "
        "created_at, modified_at) VALUES (1, 'J-1', 2, 1, 1, 1, 'HVAC service', "
        "'scheduled', 'normal', 0, 'x', ?, ?)",
        (noon - 7_200_000, noon - 7_200_000),
    )
    conn.execute(
        "INSERT INTO _appointments (id, appointment_number, job_id, status, "
        "scheduled_start, scheduled_end, arrival_window_start, arrival_window_end, "
        "actual_start, actual_end, cancelled_at, created_at, modified_at) "
        "VALUES (1, 'A-1', 1, 'scheduled', ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
        (noon - 7_200_000, noon, noon - 7_200_000, noon,
         noon - 7_200_000, noon - 7_200_000),
    )
    row = conn.execute(
        "SELECT actual_start, actual_start_iso, cancelled_at, cancelled_at_iso "
        "FROM appointments"
    ).fetchone()
    assert row["actual_start"] is None
    assert row["actual_start_iso"] is None
    assert row["cancelled_at"] is None
    assert row["cancelled_at_iso"] is None


def test_iso_siblings_render_phoenix_day_boundaries(conn):
    # decision 7: the ISO siblings render Phoenix-local time with the fixed
    # −07:00 offset. An instant in the first half-hour of the day must stay
    # on its own Phoenix date (never rolling over to the UTC date), and an
    # instant after midnight in UTC the previous day must keep its Phoenix
    # date. 2026-10-01T00:30-07:00 is 07:30 UTC — a UTC render would show the
    # wrong hour.
    noon = phoenix_instant(2026, 10, 1, 12, 0)
    conn.execute(
        "INSERT INTO _clock (singleton, current_time_ms, service_date) "
        "VALUES (1, ?, ?)",
        (noon, "2026-10-01"),
    )
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, "
        "modified_at) VALUES (1, 'Early', 'residential', 'active', ?, ?)",
        (phoenix_instant(2026, 10, 1, 0, 30), phoenix_instant(2026, 10, 1, 0, 30)),
    )
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, "
        "modified_at) VALUES (2, 'Late', 'residential', 'active', ?, ?)",
        (phoenix_instant(2026, 9, 30, 23, 30), phoenix_instant(2026, 9, 30, 23, 30)),
    )
    rows = conn.execute(
        "SELECT id, created_at_iso FROM customers ORDER BY id"
    ).fetchall()
    assert [r["created_at_iso"] for r in rows] == [
        "2026-10-01T00:30:00-07:00",
        "2026-09-30T23:30:00-07:00",
    ]


# ---------------------------------------------------------------------------
# company_state (spec 02 §4.9 — the one verbatim view)
# ---------------------------------------------------------------------------


def test_company_state_projection(conn):
    noon = phoenix_instant(2026, 10, 1, 12, 0)
    conn.execute("BEGIN")
    insert_run(
        conn,
        run_id="r-1",
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        master_seed=1,
        start_at_ms=noon,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        status=RunStatus.ACTIVE,
    )
    conn.execute("COMMIT")
    # One committed entry (visible), one future entry (excluded by the
    # `occurred_at <= k.current_time_ms` guard of §4.9).
    conn.execute(
        "INSERT INTO _ledger_entries (id, occurred_at, service_date, entry_type, "
        "amount_cents, balance_after_cents, description, created_at) "
        "VALUES (1, ?, '2026-10-01', 'revenue', 1000, 1000, 'x', ?)",
        (noon - 3_600_000, noon - 3_600_000),
    )
    conn.execute(
        "INSERT INTO _ledger_entries (id, occurred_at, service_date, entry_type, "
        "amount_cents, balance_after_cents, description, created_at) "
        "VALUES (2, ?, '2026-10-01', 'revenue', 500, 1500, 'x', ?)",
        (noon + 3_600_000, noon + 3_600_000),
    )
    assert [r["name"] for r in conn.execute("PRAGMA table_info(company_state)")] == [
        "service_date",
        "as_of_ms",
        "cash_balance_cents",
        "run_status",
    ]
    rows = conn.execute("SELECT * FROM company_state").fetchall()
    assert len(rows) == 1  # one-row view
    row = rows[0]
    assert row["service_date"] == "2026-10-01"
    assert row["as_of_ms"] == noon
    assert row["cash_balance_cents"] == 1000
    assert row["run_status"] == "active"
