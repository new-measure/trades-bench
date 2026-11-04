from __future__ import annotations

import itertools
import sqlite3

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from trades_bench import clock
from trades_bench.clock import phoenix_instant
from trades_bench.db import in_memory
from trades_bench.migrations import migrate

GRID_START_MINUTES = tuple(b[0] for b in clock.BLOCK_GRID_MINUTES)
BLOCK_CODE_BY_MINUTE = {
    420: "B07_09", 540: "B09_11", 660: "B11_13",
    780: "B13_15", 900: "B15_17", 1020: "B17_19",
}
BLOCK_LENGTH_MS = 7_200_000

_IDS = itertools.count(10_000)


def _next_id() -> int:
    return next(_IDS)


@pytest.fixture
def conn():
    c = in_memory()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _insert(conn, sql: str, params: tuple) -> None:
    conn.execute(sql, params)


def _parents(conn) -> None:
    if conn.execute("SELECT 1 FROM _customers WHERE id = 1").fetchone():
        return
    _insert(
        conn,
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at) "
        "VALUES (1, 'Acme', 'commercial', 'active', 0, 0)",
        (),
    )
    _insert(
        conn,
        "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, "
        "postal_code, latitude, longitude, zone_id, active, created_at, modified_at, "
        "effective_zone_id) VALUES (1, 1, 'HQ', '1 Main St', '', 'Phoenix', 'AZ', "
        "'85001', 33.5, -112.0, 1, 1, 0, 0, 1)",
        (),
    )
    _insert(
        conn,
        "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
        "caller_name, caller_phone, customer_id, location_id, requested_service_name, "
        "urgent, created_at) VALUES (1, 0, '2026-10-01', 'new_job', 'booked', 'A', "
        "'555', 1, 1, 'HVAC service', 0, 0)",
        (),
    )
    for job_id, job_type in ((1, 1), (2, 2)):
        _insert(
            conn,
            "INSERT INTO _jobs (id, job_number, call_id, customer_id, location_id, "
            "job_type_id, business_unit_id, requested_service_name, status, priority, "
            "no_charge, summary, created_at, modified_at) VALUES (?, ?, 1, 1, 1, ?, 1, "
            "'HVAC service', 'scheduled', 'normal', 0, 'x', 0, 0)",
            (job_id, f"J-{job_id}", job_type),
        )
    _insert(
        conn,
        "INSERT INTO _appointments (id, appointment_number, job_id, status, "
        "scheduled_start, scheduled_end, arrival_window_start, arrival_window_end, "
        "created_at, modified_at) VALUES (1, 'A-1', 1, 'scheduled', 0, 0, 0, 0, 0, 0)",
        (),
    )
    _insert(
        conn,
        "INSERT INTO _technicians (id, name, street, unit, city, state, postal_code, "
        "home_latitude, home_longitude, home_zone_id, joined_on, hourly_wage_cents, "
        "employment_status, availability_status, created_at, modified_at) VALUES "
        "(1, 'T', '', '', 'Phoenix', 'AZ', '85001', 33.5, -112.0, 1, '2026-01-01', "
        "1000, 'active', 'available', 0, 0)",
        (),
    )
    _insert(
        conn,
        "INSERT INTO _non_job_appointments (id, technician_id, kind, name, start_at, "
        "end_at, block_code, status, created_at) VALUES (1, 1, 'training', 'n', 0, 0, "
        "'B07_09', 'completed', 0)",
        (),
    )
    _insert(
        conn,
        "INSERT INTO _address_catalog (id, dataset_version, house_number, street, city, "
        "state, postal_code, formatted_address, canonical_key, latitude, longitude, "
        "zone_id, effective_zone_id, anchor_id, access_distance_km, "
        "segment_eligibility, status, created_at) VALUES (1, 'v', '1', 's', 'c', 'AZ', "
        "'85001', 'f', 'k1', 33.5, -112.0, 1, 1, 'a', 1.0, '[\"residential\"]', "
        "'usable', 0)",
        (),
    )


def _appointment(conn, job_id: int, scheduled_start: int) -> int:
    aid = _next_id()
    _insert(
        conn,
        "INSERT INTO _appointments (id, appointment_number, job_id, status, "
        "scheduled_start, scheduled_end, arrival_window_start, arrival_window_end, "
        "created_at, modified_at) VALUES (?, ?, ?, 'scheduled', ?, ?, ?, ?, 0, 0)",
        (aid, f"A-{aid}", job_id, scheduled_start, scheduled_start + BLOCK_LENGTH_MS,
         scheduled_start, scheduled_start + BLOCK_LENGTH_MS),
    )
    return aid


def _block(conn, appointment_id: int, ordinal: int, block_start: int) -> None:
    minute = (
        clock.phoenix_local(block_start).hour * 60
        + clock.phoenix_local(block_start).minute
    )
    _insert(
        conn,
        "INSERT INTO _appointment_blocks (id, appointment_id, ordinal, block_code, "
        "block_start, block_end, created_at) VALUES (?, ?, ?, ?, ?, ?, 0)",
        (_next_id(), appointment_id, ordinal,
         BLOCK_CODE_BY_MINUTE.get(minute, "B07_09"), block_start,
         block_start + BLOCK_LENGTH_MS),
    )


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(minutes=st.one_of(st.integers(0, 1439), st.sampled_from(GRID_START_MINUTES)))
def test_block_grid_membership_property(conn, minutes):
    _parents(conn)
    start = phoenix_instant(2026, 10, 2, 0, 0) + minutes * 60_000
    aid = _appointment(conn, 1, start)
    if minutes in GRID_START_MINUTES:
        _block(conn, aid, 1, start)
    else:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _block(conn, aid, 1, start)


def test_block_end_must_be_start_plus_exactly_two_hours(conn):
    _parents(conn)
    start = phoenix_instant(2026, 10, 2, 9, 0)
    aid = _appointment(conn, 1, start)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert(
            conn,
            "INSERT INTO _appointment_blocks (id, appointment_id, ordinal, block_code, "
            "block_start, block_end, created_at) VALUES (?, ?, 1, 'B09_11', ?, ?, 0)",
            (_next_id(), aid, start, start + 3_600_000),
        )


def test_block_count_overflow_trigger_per_job_type(conn):
    _parents(conn)
    start = phoenix_instant(2026, 10, 2, 7, 0)
    for job_id, allowed in ((1, 1), (2, 1)):
        aid = _appointment(conn, job_id, start)
        for ordinal in range(1, allowed + 1):
            _block(conn, aid, ordinal, start + (ordinal - 1) * BLOCK_LENGTH_MS)
        with pytest.raises(
            sqlite3.IntegrityError, match="appointment_block_overflow"
        ):
            _block(conn, aid, allowed + 1, start + allowed * BLOCK_LENGTH_MS)


def test_ordinal_one_block_must_start_at_scheduled_start(conn):
    _parents(conn)
    scheduled = phoenix_instant(2026, 10, 2, 9, 0)
    aid = _appointment(conn, 1, scheduled)
    with pytest.raises(
        sqlite3.IntegrityError, match="appointment_block_start_mismatch"
    ):
        _block(conn, aid, 1, scheduled + BLOCK_LENGTH_MS)
    _block(conn, aid, 1, scheduled)


def test_one_active_assignment_per_appointment(conn):
    _parents(conn)
    base = (
        "INSERT INTO _appointment_assignments (id, appointment_id, job_id, "
        "technician_id, status, assigned_at, unassigned_at, assigned_by, created_at) "
        "VALUES (?, 1, 1, 1, ?, 0, ?, 'agent', 0)"
    )
    _insert(conn, base, (_next_id(), "active", None))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert(conn, base, (_next_id(), "active", None))
    _insert(conn, base, (_next_id(), "reassigned", 0))


def test_hold_unique_index_blocks_duplicate_job_holds(conn):
    _parents(conn)
    start = phoenix_instant(2026, 10, 2, 11, 0)
    hold = (
        "INSERT INTO _technician_block_holds (technician_id, block_start, hold_kind, "
        "appointment_id, non_job_appointment_id, created_at) VALUES (1, ?, 'job', 1, "
        "NULL, 0)"
    )
    _insert(conn, hold, (start,))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert(conn, hold, (start,))


def test_job_holds_share_a_window_but_non_job_holds_do_not(conn):
    _parents(conn)
    start = phoenix_instant(2026, 10, 4, 9, 0)
    hold = (
        "INSERT INTO _technician_block_holds (technician_id, block_start, hold_kind, "
        "appointment_id, non_job_appointment_id, created_at) VALUES (1, ?, ?, ?, ?, 0)"
    )
    _insert(conn, hold, (start, "job", _appointment(conn, 1, start), None))
    _insert(conn, hold, (start, "job", _appointment(conn, 2, start), None))
    later = start + BLOCK_LENGTH_MS
    _insert(conn, hold, (later, "training", None, 1))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert(conn, hold, (later, "absence", None, 1))


def test_hold_requires_exactly_one_link(conn):
    _parents(conn)
    start = phoenix_instant(2026, 10, 3, 11, 0)
    for kind, appointment, non_job in (
        ("both-null", None, None),
        ("both-set", 1, 1),
    ):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert(
                conn,
                "INSERT INTO _technician_block_holds (technician_id, block_start, "
                "hold_kind, appointment_id, non_job_appointment_id, created_at) "
                "VALUES (1, ?, ?, ?, ?, 0)",
                (start, kind, appointment, non_job),
            )
    _insert(
        conn,
        "INSERT INTO _technician_block_holds (technician_id, block_start, hold_kind, "
        "appointment_id, non_job_appointment_id, created_at) VALUES (1, ?, 'job', 1, "
        "NULL, 0)",
        (start,),
    )


def test_invoice_checks_no_charge_and_total(conn):
    _parents(conn)
    base = (
        "INSERT INTO _invoices (id, invoice_number, job_id, customer_id, location_id, "
        "business_unit_id, invoice_date, issued_at, status, no_charge, subtotal_cents, "
        "total_cents, material_cost_cents, material_percent_basis_points, created_at) "
        "VALUES (?, ?, ?, 1, 1, 1, '2026-10-01', 0, 'paid', ?, 100, ?, 0, 0, 0)"
    )
    for no_charge, total in ((1, 100), (0, -1)):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert(conn, base, (_next_id(), f"I-{_next_id()}", 1, no_charge, total))
    _insert(conn, base, (_next_id(), f"I-{_next_id()}", 1, 1, 0))
    _insert(conn, base, (_next_id(), f"I-{_next_id()}", 2, 0, 50))


def test_review_stars_between_one_and_five(conn):
    _parents(conn)
    base = (
        "INSERT INTO _reviews (id, job_id, appointment_id, customer_id, technician_id, "
        "published_at, service_date, stars, source, created_at) VALUES (?, NULL, NULL, 1, "
        "NULL, 0, '2026-10-01', ?, 'google', 0)"
    )
    for stars in (0, 6):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert(conn, base, (_next_id(), stars))
    for stars in (1, 3, 5):
        _insert(conn, base, (_next_id(), stars))


_FK_CASES: list[
    tuple[str, str, str, tuple, tuple]
    | tuple[str, str, str, tuple, tuple, tuple]
    | tuple[str, str, str, tuple, tuple, None]
] = [
    ("_locations", "zone_id",
     "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, "
     "postal_code, latitude, longitude, zone_id, active, created_at, modified_at, "
     "effective_zone_id) VALUES (?, 1, 'n', 's', '', 'c', 'AZ', '85001', 33.5, -112.0, "
     "?, 1, 0, 0, 1)",
     (None,), (9_999_999,)),
    ("_locations", "address_catalog_id",
     "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, "
     "postal_code, latitude, longitude, zone_id, active, created_at, modified_at, "
     "effective_zone_id, address_catalog_id) VALUES (?, 1, 'n', 's', '', 'c', 'AZ', "
     "'85001', 33.5, -112.0, 1, 1, 0, 0, 1, ?)",
     (None,), (9_999_999,)),
    ("_technicians", "home_zone_id",
     "INSERT INTO _technicians (id, name, street, unit, city, state, postal_code, "
     "home_latitude, home_longitude, home_zone_id, joined_on, hourly_wage_cents, "
     "employment_status, availability_status, created_at, modified_at) VALUES (?, 'n', "
     "'', '', 'c', 'AZ', '85001', 33.5, -112.0, ?, '2026-01-01', 1000, 'active', "
     "'available', 0, 0)",
     (None,), (9_999_999,)),
    ("_jobs", "business_unit_id",
     "INSERT INTO _jobs (id, job_number, call_id, customer_id, location_id, "
     "job_type_id, business_unit_id, requested_service_name, status, priority, "
     "no_charge, summary, created_at, modified_at) VALUES (?, ?, 1, 1, 1, 1, ?, 'x', "
     "'scheduled', 'normal', 0, 'x', 0, 0)",
     (None,), (9_999_999,)),
    ("_jobs", "recall_for_job_id",
     "INSERT INTO _jobs (id, job_number, call_id, customer_id, location_id, "
     "job_type_id, business_unit_id, requested_service_name, status, priority, "
     "no_charge, recall_for_job_id, summary, created_at, modified_at) VALUES (?, ?, 1, "
     "1, 1, 1, 1, 'x', 'scheduled', 'normal', 0, ?, 'x', 0, 0)",
     (None,), (9_999_999,)),
    ("_calls", "customer_id",
     "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
     "caller_name, caller_phone, customer_id, requested_service_name, urgent, "
     "created_at) VALUES (?, 0, '2026-10-01', 'new_job', 'booked', 'A', '555', ?, 'x', "
     "0, 0)",
     (None,), (9_999_999,)),
    ("_calls", "location_id",
     "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
     "caller_name, caller_phone, customer_id, location_id, requested_service_name, "
     "urgent, created_at) VALUES (?, 0, '2026-10-01', 'new_job', 'booked', 'A', '555', "
     "1, ?, 'x', 0, 0)",
     (None,), (9_999_999,)),
    ("_calls", "related_job_id",
     "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
     "caller_name, caller_phone, customer_id, related_job_id, requested_service_name, "
     "urgent, created_at) VALUES (?, 0, '2026-10-01', 'new_job', 'booked', 'A', '555', "
     "1, ?, 'x', 0, 0)",
     (None,), (9_999_999,)),
    ("_calls", "target_appointment_id",
     "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
     "caller_name, caller_phone, customer_id, target_appointment_id, "
     "requested_service_name, urgent, created_at) VALUES (?, 0, '2026-10-01', "
     "'new_job', 'booked', 'A', '555', 1, ?, 'x', 0, 0)",
     (None,), (9_999_999,)),
    ("_calls", "target_job_id",
     "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
     "caller_name, caller_phone, customer_id, target_job_id, requested_service_name, "
     "urgent, created_at) VALUES (?, 0, '2026-10-01', 'new_job', 'booked', 'A', '555', "
     "1, ?, 'x', 0, 0)",
     (None,), (9_999_999,)),
    ("_calls", "created_job_id",
     "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
     "caller_name, caller_phone, customer_id, created_job_id, requested_service_name, "
     "urgent, created_at) VALUES (?, 0, '2026-10-01', 'new_job', 'booked', 'A', '555', "
     "1, ?, 'x', 0, 0)",
     (None,), (9_999_999,)),
    ("_calls", "address_catalog_id",
     "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
     "caller_name, caller_phone, customer_id, requested_service_name, urgent, "
     "created_at, address_catalog_id) VALUES (?, 0, '2026-10-01', 'new_job', 'booked', "
     "'A', '555', 1, 'x', 0, 0, ?)",
     (None,), (9_999_999,)),
    ("_invoices", "business_unit_id",
     "INSERT INTO _invoices (id, invoice_number, job_id, customer_id, location_id, "
     "business_unit_id, invoice_date, issued_at, status, no_charge, subtotal_cents, "
     "total_cents, material_cost_cents, material_percent_basis_points, created_at) "
     "VALUES (?, ?, 1, 1, 1, ?, '2026-10-01', 0, 'paid', 0, 100, 100, 0, 0, 0)",
     (None,), (9_999_999,)),
    ("_reviews", "job_id",
     "INSERT INTO _reviews (id, job_id, appointment_id, customer_id, technician_id, "
     "published_at, service_date, stars, source, created_at) VALUES (?, ?, NULL, 1, "
     "NULL, 0, '2026-10-01', 5, 'google', 0)",
     (None,), (9_999_999,)),
    ("_reviews", "appointment_id",
     "INSERT INTO _reviews (id, job_id, appointment_id, customer_id, technician_id, "
     "published_at, service_date, stars, source, created_at) VALUES (?, NULL, ?, 1, "
     "NULL, 0, '2026-10-01', 5, 'google', 0)",
     (None,), (9_999_999,)),
    ("_reviews", "technician_id",
     "INSERT INTO _reviews (id, job_id, appointment_id, customer_id, technician_id, "
     "published_at, service_date, stars, source, created_at) VALUES (?, NULL, NULL, 1, "
     "?, 0, '2026-10-01', 5, 'google', 0)",
     (None,), (9_999_999,)),
    ("_on_call_rotations", "technician_id",
     "INSERT INTO _on_call_rotations (id, business_unit_id, technician_id, "
     "effective_start_date, effective_end_date, status, vacated_at, created_at, "
     "modified_at) VALUES (?, 1, ?, '2026-12-01', NULL, 'active', NULL, 0, 0)",
     (None,), (9_999_999,)),
    ("_hiring_requests", "preferred_home_zone_id",
     "INSERT INTO _hiring_requests (id, requested_at, requested_skill_codes_json, "
     "minimum_experience_years, preferred_home_zone_id, status, recruiting_cost_cents, "
     "committed_hourly_wage_cents, committed_start_date, created_at, modified_at) "
     "VALUES (?, 0, '[]', 1, ?, 'completed', 0, 0, '2026-12-01', 0, 0)",
     (None,), (9_999_999,)),
    ("_hiring_requests", "technician_id",
     "INSERT INTO _hiring_requests (id, requested_at, requested_skill_codes_json, "
     "minimum_experience_years, preferred_home_zone_id, status, recruiting_cost_cents, "
     "committed_hourly_wage_cents, committed_start_date, technician_id, created_at, "
     "modified_at) VALUES (?, 0, '[]', 1, 1, 'completed', 0, 0, '2026-12-01', ?, 0, 0)",
     (None,), (9_999_999,)),
    ("_non_job_appointments", "training_reservation_id",
     "INSERT INTO _non_job_appointments (id, technician_id, kind, name, start_at, "
     "end_at, block_code, status, training_reservation_id, created_at) VALUES (?, 1, "
     "'training', 'n', 0, 0, 'B07_09', 'completed', ?, 0)",
     (None,), (9_999_999,)),
    ("_non_job_appointments", "technician_absence_id",
     "INSERT INTO _non_job_appointments (id, technician_id, kind, name, start_at, "
     "end_at, block_code, status, technician_absence_id, created_at) VALUES (?, 1, "
     "'training', 'n', 0, 0, 'B07_09', 'completed', ?, 0)",
     (None,), (9_999_999,)),
    ("_job_types", "business_unit_id",
     "INSERT INTO _job_types (id, code, name, business_unit_id, scheduled_blocks, "
     "required_skill_code, active, created_at) VALUES (?, ?, 'n', ?, 1, NULL, 1, 0)",
     (None,), (9_999_999,)),
    ("_customers", "created_from_call_id",
     "INSERT INTO _customers (id, name, segment, status, created_at, "
     "created_from_call_id, modified_at) VALUES (?, 'n', 'residential', 'active', 0, "
     "?, 0)",
     (None,), (9_999_999,)),
    ("_appointment_events", "technician_id",
     "INSERT INTO _appointment_events (id, appointment_id, technician_id, event_type, "
     "occurred_at, service_date, detail, created_at) VALUES (?, 1, ?, 'dispatched', 0, "
     "'2026-10-01', 'd', 0)",
     (None,), (9_999_999,)),
    ("_ledger_entries", "job_id",
     "INSERT INTO _ledger_entries (id, occurred_at, service_date, entry_type, "
     "amount_cents, balance_after_cents, job_id, invoice_id, payment_id, "
     "technician_id, hiring_request_id, training_reservation_id, description, "
     "created_at) VALUES (?, 0, '2026-10-01', 'revenue', 1, 1, ?, NULL, NULL, NULL, "
     "NULL, NULL, 'x', 0)",
     (None,), (9_999_999,)),
    ("_ledger_entries", "invoice_id",
     "INSERT INTO _ledger_entries (id, occurred_at, service_date, entry_type, "
     "amount_cents, balance_after_cents, job_id, invoice_id, payment_id, "
     "technician_id, hiring_request_id, training_reservation_id, description, "
     "created_at) VALUES (?, 0, '2026-10-01', 'revenue', 1, 1, NULL, ?, NULL, NULL, "
     "NULL, NULL, 'x', 0)",
     (None,), (9_999_999,)),
    ("_ledger_entries", "payment_id",
     "INSERT INTO _ledger_entries (id, occurred_at, service_date, entry_type, "
     "amount_cents, balance_after_cents, job_id, invoice_id, payment_id, "
     "technician_id, hiring_request_id, training_reservation_id, description, "
     "created_at) VALUES (?, 0, '2026-10-01', 'revenue', 1, 1, NULL, NULL, ?, NULL, "
     "NULL, NULL, 'x', 0)",
     (None,), (9_999_999,)),
    ("_ledger_entries", "technician_id",
     "INSERT INTO _ledger_entries (id, occurred_at, service_date, entry_type, "
     "amount_cents, balance_after_cents, job_id, invoice_id, payment_id, "
     "technician_id, hiring_request_id, training_reservation_id, description, "
     "created_at) VALUES (?, 0, '2026-10-01', 'revenue', 1, 1, NULL, NULL, NULL, ?, "
     "NULL, NULL, 'x', 0)",
     (None,), (9_999_999,)),
    ("_ledger_entries", "hiring_request_id",
     "INSERT INTO _ledger_entries (id, occurred_at, service_date, entry_type, "
     "amount_cents, balance_after_cents, job_id, invoice_id, payment_id, "
     "technician_id, hiring_request_id, training_reservation_id, description, "
     "created_at) VALUES (?, 0, '2026-10-01', 'revenue', 1, 1, NULL, NULL, NULL, NULL, "
     "?, NULL, 'x', 0)",
     (None,), (9_999_999,)),
    ("_ledger_entries", "training_reservation_id",
     "INSERT INTO _ledger_entries (id, occurred_at, service_date, entry_type, "
     "amount_cents, balance_after_cents, job_id, invoice_id, payment_id, "
     "technician_id, hiring_request_id, training_reservation_id, description, "
     "created_at) VALUES (?, 0, '2026-10-01', 'revenue', 1, 1, NULL, NULL, NULL, NULL, "
     "NULL, ?, 'x', 0)",
     (None,), (9_999_999,)),
    ("_technician_block_holds", "appointment_id",
     "INSERT INTO _technician_block_holds (technician_id, block_start, hold_kind, "
     "appointment_id, non_job_appointment_id, created_at) VALUES (1, ?, 'job', ?, ?, "
     "0)",
     (None, 1), (9_999_999, None)),
    ("_technician_block_holds", "non_job_appointment_id",
     "INSERT INTO _technician_block_holds (technician_id, block_start, hold_kind, "
     "appointment_id, non_job_appointment_id, created_at) VALUES (1, ?, 'job', ?, ?, "
     "0)",
     (1, None), (None, 9_999_999)),
    ("_service_catalog", "supported_job_type_id",
     "INSERT INTO _service_catalog (id, catalog_version, name, supported_job_type_id, "
     "demand_weight_key, active) VALUES (?, '1.0.0', 'x', ?, 'k', 1)",
     (None,), (9_999_999,)),
    ("_address_catalog", "zone_id",
     "INSERT INTO _address_catalog (id, dataset_version, house_number, street, city, "
     "state, postal_code, formatted_address, canonical_key, latitude, longitude, "
     "zone_id, effective_zone_id, anchor_id, access_distance_km, segment_eligibility, "
     "status, created_at) VALUES (?, 'v', '1', 's', 'c', 'AZ', '85001', 'f', ?, 33.5, "
     "-112.0, ?, 1, 'a', 1.0, '[\"residential\"]', 'usable', 0)",
     (None,), (9_999_999,)),
    ("_job_types", "required_skill_code",
     "INSERT INTO _job_types (id, code, name, business_unit_id, scheduled_blocks, "
     "required_skill_code, active, created_at) VALUES (?, ?, 'n', 1, 1, ?, 1, 0)",
     (None,), ("no_such_skill",), ("hvac_service",)),
    ("_technician_shifts", "on_call_rotation_id",
     "INSERT INTO _technician_shifts (id, technician_id, shift_type, start_at, "
     "end_at, service_date, active, on_call_rotation_id, created_at) VALUES (?, 1, "
     "'office', 0, 0, '2026-10-01', 1, ?, 0)",
     (None,), (9_999_999,), None),
]

_UNIQUE_KEYS: dict[str, str] = {
    "_jobs": "job_number",
    "_invoices": "invoice_number",
    "_job_types": "code",
    "_address_catalog": "canonical_key",
}


def _shift_rotation(conn) -> int:
    rid = _next_id()
    _insert(
        conn,
        "INSERT INTO _on_call_rotations (id, business_unit_id, technician_id, "
        "effective_start_date, effective_end_date, status, vacated_at, created_at, "
        "modified_at) VALUES (?, 1, 1, '2026-12-15', NULL, 'active', NULL, 0, 0)",
        (rid,),
    )
    return rid


def test_nullable_fk_matrix(conn):
    _parents(conn)
    hold_start = 50_000
    for index, case in enumerate(_FK_CASES):
        table, column, sql, null_params, bad_params = case[:5]
        valid_params = case[5] if len(case) > 5 else None
        key_col = _UNIQUE_KEYS.get(table)
        if table == "_technician_shifts":
            valid_params = (_shift_rotation(conn),)
        variants = [(bad_params, False)]
        if valid_params is not None:
            variants.append((valid_params, True))
        variants.append((null_params, True))
        for params, expect_ok in variants:
            row_id = _next_id()
            if table == "_technician_block_holds":
                full = (hold_start + index * 1000, *params)
            else:
                key = None if key_col is None else f"{key_col}-{row_id}"
                full = (row_id, key, *params) if key_col else (row_id, *params)
            if expect_ok:
                _insert(conn, sql, full)
            else:
                with pytest.raises(
                    sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"
                ):
                    _insert(conn, sql, full)
        assert f"{table}.{column} covered"


def test_two_active_calls_are_not_rejected_at_storage(conn):
    _parents(conn)
    for _n in (2, 3):
        _insert(
            conn,
            "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
            "caller_name, caller_phone, requested_service_name, urgent, created_at) "
            "VALUES (?, ?, '2026-10-01', 'new_job', 'active', 'A', '555', 'x', 0, ?)",
            (_next_id(), 0, 0),
        )
    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM _calls WHERE status = 'active'"
        ).fetchone()["c"]
        == 2
    )
