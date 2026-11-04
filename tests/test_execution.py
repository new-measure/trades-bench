from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from itertools import count

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from _adapters import ScriptedNoOpAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    audit_count,
    geo_artifact_sha256,
    next_test_origin,
    register_conn,
)
from trades_bench import (
    SPEC_VERSION,
    appointments,
    clock,
    constants,
    demand,
    execution,
    geo_reader,
    geography,
    origin_keys,
    travel,
)
from trades_bench._dump import dump_hash
from trades_bench.config import load_run_config
from trades_bench.controller import Controller
from trades_bench.enums import ConditionCode, EventKind, KeyedStreamName, LedgerCategory
from trades_bench.errors import EngineError, ErrorCode
from trades_bench.events import Events
from trades_bench.ledger import Ledger
from trades_bench.money import basis_point_amount
from trades_bench.rng import U64_MOD, Rng, inverse_cdf_variation
from trades_bench.run_init import initialize_run, open_run_db
from trades_bench.sql_gate import execute_gated, open_agent_session
from trades_bench.txn import privileged, privileged_request_id

SEED = 5
END_DATE = "2026-10-14"
WEEKDAY = "2026-10-01"
SECOND_WEEKDAY = "2026-10-02"
WEEKEND = "2026-10-03"

HVAC_SERVICE_CATEGORY_ID = 2
PLUMBING_CATEGORY_ID = 3
UNSUPPORTED_CATEGORY_ID = 5

ARTIFACT = geo_reader.load_artifact(GEO_ARTIFACT_DIR)
DESTINATION_ADDRESS = next(row for row in ARTIFACT.catalog if row.address_id == 2)
SECOND_LOCATION_ADDRESS = next(row for row in ARTIFACT.catalog if row.address_id == 3)

_TECHNICIAN_QUALIFIED_CATEGORY_ID = {
    1: HVAC_SERVICE_CATEGORY_ID,
    2: HVAC_SERVICE_CATEGORY_ID,
    3: PLUMBING_CATEGORY_ID,
    4: 4,
}

EARLY_FINISH_ORIGIN = "t:chain:28"

_id_seq = count(9500)
_request_seq = count(1)


def _config(**overrides):
    keys = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": [0, 0, 0, 1, 0, 0, 0, 0, 0, 0],
        "run.end_date": END_DATE,
        **overrides,
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def _build(tmp_path):
    cfg = _config()
    db_path = tmp_path / "run.db"
    initialize_run(db_path, master_seed=SEED, config=cfg, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_run_db(db_path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    controller = Controller(conn, cfg, ScriptedNoOpAdapter(), artifact_dir=GEO_ARTIFACT_DIR)
    return conn, cfg, controller


def _build_run(tmp_path, *, end_date=WEEKDAY, **config_overrides):
    cfg = _config(**{"run.end_date": end_date, **config_overrides})
    db_path = tmp_path / "run.db"
    initialize_run(db_path, master_seed=SEED, config=cfg, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_run_db(db_path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    controller = Controller(conn, cfg, ScriptedNoOpAdapter(), artifact_dir=GEO_ARTIFACT_DIR)
    return conn, cfg, controller, db_path


def _customer_location_call(
    conn, now_ms: int, service_date: str, *, address_catalog_id: int, config=None,
    requested_category_id: int = HVAC_SERVICE_CATEGORY_ID, origin_key: str | None = None,
) -> int:
    row_id = next(_id_seq)
    origin_key = origin_key if origin_key is not None else next_test_origin(conn)
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at) "
        "VALUES (?, 'Acme', 'commercial', 'active', ?, ?)",
        (row_id, now_ms, now_ms),
    )
    if config is not None:
        demand.register_customer_repeat_state(
            conn, config, customer_id=row_id, segment="commercial",
            origin_key=origin_key, now_ms=now_ms,
        )
    conn.execute(
        "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, "
        "postal_code, latitude, longitude, active, created_at, modified_at, "
        "effective_zone_id, address_catalog_id) VALUES (?, ?, 'HQ', '1 Main St', '', "
        "'Tempe', 'AZ', '85282', 33.4, -111.9, 1, ?, ?, ?, ?)",
        (row_id, row_id, now_ms, now_ms, DESTINATION_ADDRESS.effective_zone_id, address_catalog_id),
    )
    conn.execute(
        "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, "
        "caller_name, caller_phone, requested_service_name, urgent, created_at) "
        "VALUES (?, ?, ?, 'new_job', 'booked', 'A', '555', 'HVAC service', 0, ?)",
        (row_id, now_ms, service_date, now_ms),
    )
    conn.execute(
        "INSERT INTO _call_hidden (call_id, true_segment, requested_category_id, "
        "prospect_id, arrival_event_id, origin_key, created_at) "
        "VALUES (?, 'commercial', ?, NULL, NULL, ?, ?)",
        (row_id, requested_category_id, origin_key, now_ms),
    )
    return row_id


def _book(services, config, **kwargs):
    request_id = privileged_request_id("book", next(_request_seq))
    response = privileged(
        services, request_id, lambda ctx: appointments.book(ctx, config, **kwargs)
    )
    body = json.loads(response)
    assert body["ok"] is True, body
    return body["result"]


def test_block_index_and_code_round_trip():
    for i, code in enumerate(execution.BLOCK_CODES):
        assert execution.block_index(code) == i
        assert execution.block_code_at(i) == code


def test_block_start_and_end_instants_match_the_grid():
    assert execution.block_start_instant(WEEKDAY, "B07_09") == clock.phoenix_instant(
        2026, 10, 1, 7, 0
    )
    assert execution.block_end_instant(WEEKDAY, "B07_09") == clock.phoenix_instant(
        2026, 10, 1, 9, 0
    )


def test_execution_duration_multiplier_is_exactly_one_under_shipped_defaults():
    cfg = load_run_config()
    multiplier = execution.execution_duration_multiplier(
        cfg, skill_input=Decimal(0), strain_input=Decimal(0), complexity_input=Decimal(0)
    )
    assert multiplier == Decimal(1)


def test_execution_duration_multiplier_floors_at_the_configured_minimum():
    cfg = load_run_config(
        scenario_json=json.dumps({"keys": {"quality.duration_skill_coefficient": 10}})
    )
    multiplier = execution.execution_duration_multiplier(cfg, skill_input=Decimal(5))
    assert multiplier == cfg.get_decimal("appointments.min_duration_multiplier")


def test_inverse_cdf_variation_pinned_boundary_points():
    assert inverse_cdf_variation(0) == -15
    assert inverse_cdf_variation(1 << 63) == 0
    assert inverse_cdf_variation((1 << 63) - 1) == 0
    assert inverse_cdf_variation((1 << 64) - 1) == 15


def test_inverse_cdf_variation_hand_computed_interior_literal():
    u = 1 << 62
    p = Fraction(u, U64_MOD)
    assert p == Fraction(1, 4)
    assert inverse_cdf_variation(u) == -4


@given(st.integers(min_value=0, max_value=(1 << 64) - 1))
@settings(max_examples=300, deadline=None)
def test_inverse_cdf_variation_output_always_in_range(u):
    assert -15 <= inverse_cdf_variation(u) <= 15


@given(
    st.integers(min_value=0, max_value=(1 << 64) - 2),
    st.integers(min_value=1, max_value=1000),
)
@settings(max_examples=300, deadline=None)
def test_inverse_cdf_variation_is_monotone_nondecreasing(u, step):
    u2 = min(u + step, (1 << 64) - 1)
    assert inverse_cdf_variation(u) <= inverse_cdf_variation(u2)


@given(st.integers(min_value=1, max_value=(1 << 63) - 1))
@settings(max_examples=300, deadline=None)
def test_inverse_cdf_variation_symmetric_pair_property(offset):
    u = (1 << 63) - offset
    mirrored = (1 << 64) - u
    assert inverse_cdf_variation(u) == -inverse_cdf_variation(mirrored)


def test_inverse_cdf_variation_rejects_out_of_range_u():
    with pytest.raises(EngineError) as ei:
        inverse_cdf_variation(1 << 64)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(EngineError) as ei:
        inverse_cdf_variation(-1)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT


def test_technician_home_point_resolves_the_seeded_home_address(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    point = execution.technician_home_point(conn, 1)
    assert point.catalog_point.address_id == 414
    assert point.effective_zone_id > 0


def test_location_point_requires_a_catalog_address(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    now = clock.now(conn)
    location_id = _customer_location_call(conn, now, WEEKDAY, address_catalog_id=None)
    with pytest.raises(EngineError) as ei:
        execution.location_point(conn, location_id)
    assert ei.value.code is ErrorCode.STATE_ILLEGAL


def test_planned_travel_minutes_matches_a_hand_computed_literal(tmp_path):
    conn, cfg, controller = _build(tmp_path)
    origin = execution.technician_home_point(conn, 1)
    now = clock.now(conn)
    location_id = _customer_location_call(
        conn, now, WEEKDAY, address_catalog_id=DESTINATION_ADDRESS.address_id
    )
    destination = execution.location_point(conn, location_id)

    minute_of_day = 600
    result = execution.planned_travel_minutes(
        conn, ARTIFACT, cfg,
        origin=origin, destination=destination,
        service_date=WEEKDAY, minute_of_day=minute_of_day,
    )

    distance_km = geography.route_distance_km(
        origin.catalog_point.access_distance_km, origin.catalog_point.anchor_id,
        destination.catalog_point.access_distance_km, destination.catalog_point.anchor_id,
        ARTIFACT, cfg,
    )
    band = travel.band_for_minute(cfg, minute_of_day)
    speed = cfg.resolved["travel.speed_by_time_band"]["weekday"][band]
    weather_row = conn.execute(
        "SELECT condition_code FROM _zone_weather_daily WHERE zone_id = ? AND local_date = ?",
        (destination.effective_zone_id, WEEKDAY),
    ).fetchone()
    condition_code = ConditionCode(weather_row["condition_code"])
    weather_multiplier = cfg.resolved["travel.weather_travel_multipliers"][condition_code.value]
    raw = Decimal(60) * Decimal(str(distance_km)) / speed * weather_multiplier
    expected = max(
        int(raw.to_integral_value(rounding=ROUND_HALF_UP)),
        cfg.get_int("travel.min_travel_minutes"),
    )
    assert result == expected


def _expected_duration_minutes(cfg, shadow, job_origin: str, true_code: str) -> int:
    drawn = execution.work_duration_minutes(
        shadow, job_origin,
        cfg.resolved["execution.duration_minutes_by_job_type"][true_code],
    )
    scaled = Decimal(drawn) * execution.execution_duration_multiplier(cfg)
    return max(
        int(scaled.to_integral_value(rounding=ROUND_HALF_UP)),
        cfg.get_int("appointments.min_actual_duration_minutes"),
    )


def _shadow_rng() -> Rng:
    rng = Rng(SEED, SPEC_VERSION)
    rng.enter_txn()
    return rng


def _appointment_event_rows(conn, appointment_id):
    return conn.execute(
        "SELECT event_type, technician_id, occurred_at, service_date "
        "FROM _appointment_events WHERE appointment_id = ? ORDER BY occurred_at, id",
        (appointment_id,),
    ).fetchall()


def _technician_location_row(conn, technician_id):
    return conn.execute(
        "SELECT * FROM _technician_locations WHERE technician_id = ?", (technician_id,)
    ).fetchone()


def _step_until(controller, predicate, max_steps=20000):
    for _ in range(max_steps):
        if predicate():
            return
        if not controller.step():
            break
    assert predicate(), "condition never became true within the step budget"


def _book_execution(controller, cfg, *, technician_id, service_date, first_block_code,
                     job_type_id=1, address_catalog_id=DESTINATION_ADDRESS.address_id,
                     requested_category_id=None, priority="normal", origin_key=None):
    now = clock.now(controller.conn)
    category_id = (
        requested_category_id
        if requested_category_id is not None
        else _TECHNICIAN_QUALIFIED_CATEGORY_ID[technician_id]
    )
    location_id = _customer_location_call(
        controller.conn, now, service_date, address_catalog_id=address_catalog_id, config=cfg,
        requested_category_id=category_id, origin_key=origin_key,
    )
    return _book(
        controller.services, cfg,
        call_id=location_id, customer_id=location_id, location_id=location_id,
        job_type_id=job_type_id, requested_service_name="HVAC service",
        priority=priority, summary="x",
        technician_id=technician_id, service_date=service_date,
        first_block_code=first_block_code,
    )


def _cancel_appointment(controller, cfg, *, appointment_id, effective_at):
    request_id = privileged_request_id("cancel", next(_request_seq))
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.cancel_appointment(
            ctx, cfg, appointment_id=appointment_id, effective_at=effective_at, artifact=ARTIFACT,
        ),
    )
    body = json.loads(response)
    assert body["ok"] is True, body
    return body["result"]


def _weekday_wage_total(conn) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(hourly_wage_cents), 0) AS s FROM _technicians "
        "WHERE employment_status IN ('active','pending_termination','resigning_notice')"
    ).fetchone()
    return int(row["s"])


def test_planner_schedules_pending_dispatch_for_a_mid_day_block(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    appointment_id = booked["appointment_id"]

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT 1 FROM _events WHERE kind = ? AND entity_id = ?",
            (EventKind.APPOINTMENT_DISPATCH.value, appointment_id),
        ).fetchone() is not None,
    )

    row = conn.execute(
        "SELECT scheduled_at_ms, status FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.APPOINTMENT_DISPATCH.value, appointment_id),
    ).fetchone()
    assert row["status"] == "pending"

    origin = execution.technician_home_point(conn, 1)
    _job_id, location_id = execution._appointment_job_location_id(conn, appointment_id)
    destination = execution.location_point(conn, location_id)
    block_start = clock.phoenix_instant(2026, 10, 1, 9, 0)
    planned = execution.planned_travel_minutes(
        conn, ARTIFACT, cfg, origin=origin, destination=destination,
        service_date=WEEKDAY, minute_of_day=9 * 60,
    )
    assert row["scheduled_at_ms"] == block_start - planned * 60000


_SLOW_MORNING_SPEED_OVERRIDE = {
    "weekday": {
        "night": 62, "morning_peak": 1, "midday": 52,
        "evening_peak": 34, "evening": 50, "night_late": 60,
    },
    "weekend": {
        "night": 60, "morning_peak": 52, "midday": 55,
        "evening_peak": 50, "evening": 52, "night_late": 60,
    },
}


_SLOW_MIDDAY_SPEED_OVERRIDE = {
    "weekday": {
        "night": 62, "morning_peak": 38, "midday": 8,
        "evening_peak": 34, "evening": 50, "night_late": 60,
    },
    "weekend": {
        "night": 60, "morning_peak": 52, "midday": 8,
        "evening_peak": 50, "evening": 52, "night_late": 60,
    },
}


def test_planner_dispatches_at_opening_when_planned_travel_exceeds_the_floor_gap(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"travel.speed_by_time_band": _SLOW_MORNING_SPEED_OVERRIDE}
    )
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B07_09",
    )
    appointment_id = booked["appointment_id"]

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT 1 FROM _appointment_events WHERE appointment_id = ? AND event_type = 'dispatched'",
            (appointment_id,),
        ).fetchone() is not None,
    )

    opening = clock.phoenix_instant(2026, 10, 1, 6, 45)
    event = conn.execute(
        "SELECT occurred_at FROM _appointment_events WHERE appointment_id = ? "
        "AND event_type = 'dispatched'",
        (appointment_id,),
    ).fetchone()
    assert event["occurred_at"] == opening
    assert conn.execute(
        "SELECT 1 FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.APPOINTMENT_DISPATCH.value, appointment_id),
    ).fetchone() is None


def test_happy_path_office_day_full_chain_and_settlement(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    appointment_id = booked["appointment_id"]

    origin = execution.technician_home_point(conn, 1)
    _job_id, location_id = execution._appointment_job_location_id(conn, appointment_id)
    destination = execution.location_point(conn, location_id)
    block_start = clock.phoenix_instant(2026, 10, 1, 9, 0)
    planned = execution.planned_travel_minutes(
        conn, ARTIFACT, cfg, origin=origin, destination=destination,
        service_date=WEEKDAY, minute_of_day=9 * 60,
    )
    expected_departure = block_start - planned * 60000

    shadow = _shadow_rng()
    distance_km = geography.route_distance_km(
        origin.catalog_point.access_distance_km, origin.catalog_point.anchor_id,
        destination.catalog_point.access_distance_km, destination.catalog_point.anchor_id,
        ARTIFACT, cfg,
    )
    departure_local = clock.phoenix_local(expected_departure)
    departure_minute = departure_local.hour * 60 + departure_local.minute
    weather_row = conn.execute(
        "SELECT condition_code FROM _zone_weather_daily WHERE zone_id = ? AND local_date = ?",
        (destination.effective_zone_id, WEEKDAY),
    ).fetchone()
    condition_code = ConditionCode(weather_row["condition_code"])
    noise_key = travel.inbound_trip_noise_key(origin_keys.of_appointment(conn, appointment_id), WEEKDAY, 0)
    actual_travel = travel.actual_travel_minutes(
        shadow, cfg, distance_km=distance_km, day_type="weekday",
        minute_of_day=departure_minute, condition_code=condition_code, noise_key=noise_key,
    )
    expected_arrival = expected_departure + actual_travel * 60000

    start_variation = execution.start_variation_minutes(shadow, origin_keys.of_appointment(conn, appointment_id), WEEKDAY)
    expected_work_started = max(
        expected_arrival, block_start, block_start + start_variation * 60000
    )

    duration_minutes = _expected_duration_minutes(
        cfg, shadow, origin_keys.of_job(conn, booked["job_id"]), "hvac_service"
    )
    expected_completion = expected_work_started + duration_minutes * 60000

    status = controller.run()
    assert str(status) == "completed"

    events = _appointment_event_rows(conn, appointment_id)
    assert [e["event_type"] for e in events] == [
        "dispatched", "arrived", "work_started", "work_completed",
    ]
    assert events[0]["occurred_at"] == expected_departure
    assert events[1]["occurred_at"] == expected_arrival
    assert events[2]["occurred_at"] == expected_work_started
    assert events[3]["occurred_at"] == expected_completion
    assert all(e["technician_id"] == 1 for e in events)

    appt = conn.execute(
        "SELECT status, actual_start, actual_end FROM _appointments WHERE id = ?",
        (appointment_id,),
    ).fetchone()
    assert appt["status"] == "completed"
    assert appt["actual_start"] == expected_work_started
    assert appt["actual_end"] == expected_completion

    day = conn.execute(
        "SELECT unresolved_execution_count, closing_at, last_execution_completed_at "
        "FROM _day_state WHERE service_date = ?",
        (WEEKDAY,),
    ).fetchone()
    assert day["unresolved_execution_count"] == 0
    assert day["closing_at"] == clock.phoenix_instant(2026, 10, 1, 19, 0)
    assert day["last_execution_completed_at"] == expected_completion

    starting_cash = cfg.get_int("finance.starting_cash")
    overhead = cfg.get_int("finance.daily_operating_overhead")
    wage_total = _weekday_wage_total(conn)
    invoice = conn.execute(
        "SELECT total_cents, material_cost_cents FROM _invoices WHERE job_id = ?",
        (booked["job_id"],),
    ).fetchone()
    expected_cash = (
        starting_cash
        + int(invoice["total_cents"])
        - int(invoice["material_cost_cents"])
        - constants.WEEKDAY_PAY_MULTIPLIER * wage_total
        - overhead
        - 2 * (_wage(conn, 1) + _wage(conn, 3))
    )
    assert controller.services.ledger.cash_balance() == expected_cash

    home = execution.technician_home_point(conn, 1)
    loc = _technician_location_row(conn, 1)
    assert loc["location_state"] == "home"
    assert loc["latitude"] == home.latitude
    assert loc["longitude"] == home.longitude


HVAC_INSTALLATION_CATEGORY_ID = 1
INSTALL_TECHNICIAN_ID = 2


def _expected_work_window(conn, cfg, appointment_id, *, technician_id, true_code):
    appointment_origin = origin_keys.of_appointment(conn, appointment_id)
    origin = execution.technician_home_point(conn, technician_id)
    job_id, location_id = execution._appointment_job_location_id(conn, appointment_id)
    destination = execution.location_point(conn, location_id)
    block_start = clock.phoenix_instant(2026, 10, 1, 9, 0)
    planned = execution.planned_travel_minutes(
        conn, ARTIFACT, cfg, origin=origin, destination=destination,
        service_date=WEEKDAY, minute_of_day=9 * 60,
    )
    expected_departure = block_start - planned * 60000

    shadow = _shadow_rng()
    distance_km = geography.route_distance_km(
        origin.catalog_point.access_distance_km, origin.catalog_point.anchor_id,
        destination.catalog_point.access_distance_km, destination.catalog_point.anchor_id,
        ARTIFACT, cfg,
    )
    departure_local = clock.phoenix_local(expected_departure)
    departure_minute = departure_local.hour * 60 + departure_local.minute
    weather_row = conn.execute(
        "SELECT condition_code FROM _zone_weather_daily WHERE zone_id = ? AND local_date = ?",
        (destination.effective_zone_id, WEEKDAY),
    ).fetchone()
    condition_code = ConditionCode(weather_row["condition_code"])
    noise_key = travel.inbound_trip_noise_key(appointment_origin, WEEKDAY, 0)
    actual_travel = travel.actual_travel_minutes(
        shadow, cfg, distance_km=distance_km, day_type="weekday",
        minute_of_day=departure_minute, condition_code=condition_code, noise_key=noise_key,
    )
    expected_arrival = expected_departure + actual_travel * 60000

    start_variation = execution.start_variation_minutes(shadow, appointment_origin, WEEKDAY)
    expected_work_started = max(
        expected_arrival, block_start, block_start + start_variation * 60000
    )

    duration_minutes = _expected_duration_minutes(
        cfg, shadow, origin_keys.of_job(conn, job_id), true_code
    )
    return expected_work_started, expected_work_started + duration_minutes * 60000


def _reserved_block_count(conn, appointment_id):
    return int(
        conn.execute(
            "SELECT COUNT(*) c FROM _appointment_blocks WHERE appointment_id = ?",
            (appointment_id,),
        ).fetchone()["c"]
    )


def _actual_window(conn, appointment_id):
    return conn.execute(
        "SELECT actual_start, actual_end, scheduled_end FROM _appointments WHERE id = ?",
        (appointment_id,),
    ).fetchone()


def test_installation_books_one_window_and_works_the_installation_duration(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=INSTALL_TECHNICIAN_ID, service_date=WEEKDAY,
        first_block_code="B09_11", job_type_id=2,
        requested_category_id=HVAC_INSTALLATION_CATEGORY_ID,
    )
    appointment_id = booked["appointment_id"]
    assert _reserved_block_count(conn, appointment_id) == 1
    expected_start, expected_end = _expected_work_window(
        conn, cfg, appointment_id, technician_id=INSTALL_TECHNICIAN_ID,
        true_code="hvac_installation",
    )

    assert str(controller.run()) == "completed"

    appt = _actual_window(conn, appointment_id)
    assert appt["actual_start"] == expected_start
    assert appt["actual_end"] == expected_end
    install = cfg.resolved["execution.duration_minutes_by_job_type"]["hvac_installation"]
    assert appt["actual_end"] - appt["actual_start"] >= install["min"] * 60000


def test_an_installation_booked_as_a_one_block_service_works_its_true_duration_and_overruns(
    tmp_path,
):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=INSTALL_TECHNICIAN_ID, service_date=WEEKDAY,
        first_block_code="B09_11", job_type_id=1,
        requested_category_id=HVAC_INSTALLATION_CATEGORY_ID,
    )
    appointment_id = booked["appointment_id"]
    assert _reserved_block_count(conn, appointment_id) == 1
    expected_start, expected_end = _expected_work_window(
        conn, cfg, appointment_id, technician_id=INSTALL_TECHNICIAN_ID,
        true_code="hvac_installation",
    )

    assert str(controller.run()) == "completed"

    appt = _actual_window(conn, appointment_id)
    assert appt["actual_start"] == expected_start
    assert appt["actual_end"] == expected_end
    install = cfg.resolved["execution.duration_minutes_by_job_type"]["hvac_installation"]
    assert appt["actual_end"] - appt["actual_start"] >= install["min"] * 60000
    assert appt["actual_end"] > appt["scheduled_end"]


def test_late_finish_moves_closing_invocation_to_actual_finish(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"appointments.min_actual_duration_minutes": 200}
    )
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B17_19",
    )
    appointment_id = booked["appointment_id"]

    status = controller.run()
    assert str(status) == "completed"

    appt = conn.execute(
        "SELECT actual_start, actual_end FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    assert appt["actual_end"] - appt["actual_start"] == 200 * 60000
    assert appt["actual_end"] > clock.phoenix_instant(2026, 10, 1, 19, 0)

    day = conn.execute(
        "SELECT closing_at FROM _day_state WHERE service_date = ?", (WEEKDAY,)
    ).fetchone()
    assert day["closing_at"] == appt["actual_end"]


def test_completion_past_midnight_keeps_service_date_on_the_scheduled_day(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"appointments.min_actual_duration_minutes": 450}
    )
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B17_19",
    )
    appointment_id = booked["appointment_id"]

    status = controller.run()
    assert str(status) == "completed"

    appt = conn.execute(
        "SELECT actual_end FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    assert clock.service_date_for_ms(int(appt["actual_end"])) != WEEKDAY

    events = _appointment_event_rows(conn, appointment_id)
    completed_event = next(e for e in events if e["event_type"] == "work_completed")
    started_event = next(e for e in events if e["event_type"] == "work_started")
    assert completed_event["service_date"] == WEEKDAY
    assert started_event["service_date"] == WEEKDAY

    day = conn.execute(
        "SELECT unresolved_execution_count, closing_at, last_execution_completed_at "
        "FROM _day_state WHERE service_date = ?",
        (WEEKDAY,),
    ).fetchone()
    assert day["unresolved_execution_count"] == 0
    assert day["closing_at"] == appt["actual_end"]
    assert day["last_execution_completed_at"] == appt["actual_end"]


def test_consecutive_chain_departs_from_previous_job_at_actual_finish(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    first = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        address_catalog_id=DESTINATION_ADDRESS.address_id,
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    status = controller.run()
    assert str(status) == "completed"

    first_events = _appointment_event_rows(conn, first["appointment_id"])
    second_events = _appointment_event_rows(conn, second["appointment_id"])
    first_completed = next(e for e in first_events if e["event_type"] == "work_completed")
    second_dispatched = next(e for e in second_events if e["event_type"] == "dispatched")
    assert second_dispatched["occurred_at"] == first_completed["occurred_at"]

    movements = conn.execute(
        "SELECT COUNT(*) c FROM _technician_movements WHERE technician_id = 1"
    ).fetchone()
    assert movements["c"] == 1

    second_arrived = next(e for e in second_events if e["event_type"] == "arrived")
    assert second_arrived["occurred_at"] > second_dispatched["occurred_at"]

    origin = execution.location_point(
        conn, execution._appointment_job_location_id(conn, first["appointment_id"])[1]
    )
    destination = execution.location_point(
        conn, execution._appointment_job_location_id(conn, second["appointment_id"])[1]
    )
    distance_km = geography.route_distance_km(
        origin.catalog_point.access_distance_km, origin.catalog_point.anchor_id,
        destination.catalog_point.access_distance_km, destination.catalog_point.anchor_id,
        ARTIFACT, cfg,
    )
    departure_local = clock.phoenix_local(second_dispatched["occurred_at"])
    departure_minute = departure_local.hour * 60 + departure_local.minute
    condition_code = ConditionCode(
        conn.execute(
            "SELECT condition_code FROM _zone_weather_daily WHERE zone_id = ? AND local_date = ?",
            (destination.effective_zone_id, WEEKDAY),
        ).fetchone()["condition_code"]
    )
    trip_index = appointments.trip_index_for_appointment(conn, second["appointment_id"])
    assert trip_index == 0
    noise_key = travel.inbound_trip_noise_key(origin_keys.of_appointment(conn, second["appointment_id"]), WEEKDAY, trip_index)
    shadow = _shadow_rng()
    actual_minutes = travel.actual_travel_minutes(
        shadow, cfg, distance_km=distance_km, day_type="weekday",
        minute_of_day=departure_minute, condition_code=condition_code, noise_key=noise_key,
    )
    expected_arrival = second_dispatched["occurred_at"] + actual_minutes * 60000
    assert second_arrived["occurred_at"] == expected_arrival

    home = execution.technician_home_point(conn, 1)
    loc = _technician_location_row(conn, 1)
    assert loc["location_state"] == "home"
    assert loc["latitude"] == home.latitude
    assert loc["longitude"] == home.longitude


def test_reassigning_away_a_pending_dispatch_replans_the_technicians_remaining_day(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    first = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        address_catalog_id=DESTINATION_ADDRESS.address_id,
        requested_category_id=PLUMBING_CATEGORY_ID,
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    first_appointment_id = first["appointment_id"]
    second_appointment_id = second["appointment_id"]

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT 1 FROM _events WHERE kind = ? AND entity_id = ? AND status = 'pending'",
            (EventKind.APPOINTMENT_DISPATCH.value, first_appointment_id),
        ).fetchone() is not None,
    )
    assert conn.execute(
        "SELECT location_state FROM _technician_locations WHERE technician_id = 1"
    ).fetchone()["location_state"] == "home"

    request_id = privileged_request_id("assign", next(_request_seq))
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.assign(
            ctx, cfg, appointment_id=first_appointment_id, technician_id=3, artifact=ARTIFACT,
        ),
    )
    body = json.loads(response)
    assert body["ok"] is True, body

    replanned = conn.execute(
        "SELECT event_id, payload, status FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.APPOINTMENT_DISPATCH.value, second_appointment_id),
    ).fetchone()
    assert replanned is not None
    assert replanned["status"] == "pending"
    assert json.loads(replanned["payload"])["technician_id"] == 1

    status = controller.run()
    assert str(status) == "completed"

    first_appt = conn.execute(
        "SELECT status FROM _appointments WHERE id = ?", (first_appointment_id,)
    ).fetchone()
    assert first_appt["status"] == "completed"

    second_appt = conn.execute(
        "SELECT status FROM _appointments WHERE id = ?", (second_appointment_id,)
    ).fetchone()
    assert second_appt["status"] == "completed"

    day = conn.execute(
        "SELECT unresolved_execution_count FROM _day_state WHERE service_date = ?", (WEEKDAY,)
    ).fetchone()
    assert day["unresolved_execution_count"] == 0


def test_idle_gap_drives_straight_to_the_next_job_and_returns_home_after_the_last(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    first = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        address_catalog_id=DESTINATION_ADDRESS.address_id,
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    status = controller.run()
    assert str(status) == "completed"

    first_events = _appointment_event_rows(conn, first["appointment_id"])
    second_events = _appointment_event_rows(conn, second["appointment_id"])
    first_completed = next(e for e in first_events if e["event_type"] == "work_completed")
    second_dispatched = next(e for e in second_events if e["event_type"] == "dispatched")
    second_completed = next(e for e in second_events if e["event_type"] == "work_completed")
    assert second_dispatched["occurred_at"] == first_completed["occurred_at"]

    movements = conn.execute(
        "SELECT * FROM _technician_movements WHERE technician_id = 1"
    ).fetchall()
    assert len(movements) == 1
    assert movements[0]["movement_kind"] == "return_home"
    assert movements[0]["departed_at"] == second_completed["occurred_at"]
    second_point = execution.location_point(
        conn, execution._appointment_job_location_id(conn, second["appointment_id"])[1]
    )
    assert movements[0]["origin_latitude"] == second_point.latitude

    home = execution.technician_home_point(conn, 1)
    assert movements[0]["destination_latitude"] == home.latitude
    assert movements[0]["destination_longitude"] == home.longitude


_SLOW_MIDDAY_LONG_RETURN_OVERRIDE = {
    "weekday": {
        "night": 62, "morning_peak": 38, "midday": 2,
        "evening_peak": 34, "evening": 50, "night_late": 60,
    },
    "weekend": {
        "night": 60, "morning_peak": 52, "midday": 2,
        "evening_peak": 50, "evening": 52, "night_late": 60,
    },
}

_FAR_ADDRESS_ID = 626
_NEAR_ADDRESS_ID = 472


def test_post_idle_dispatch_clamp_binds_when_home_arrival_is_the_later_bound(
    tmp_path, monkeypatch,
):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"travel.speed_by_time_band": _SLOW_MIDDAY_LONG_RETURN_OVERRIDE}
    )
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        address_catalog_id=_FAR_ADDRESS_ID,
    )
    _step_until(
        controller,
        lambda: _technician_location_row(conn, 1)["location_state"] == "traveling"
        and conn.execute(
            "SELECT 1 FROM _technician_movements WHERE technician_id = 1"
        ).fetchone() is not None,
    )
    monkeypatch.setattr(sys.modules[__name__], "_id_seq", count(50_000_000))
    location_id = _customer_location_call(
        conn, clock.now(conn), WEEKDAY, address_catalog_id=_NEAR_ADDRESS_ID, config=cfg,
    )
    second = _book(
        controller.services, cfg,
        call_id=location_id, customer_id=location_id, location_id=location_id,
        job_type_id=1, requested_service_name="HVAC service", priority="normal",
        summary="x", technician_id=1, service_date=WEEKDAY, first_block_code="B13_15",
        artifact=ARTIFACT,
    )
    status = controller.run()
    assert str(status) == "completed"

    second_events = _appointment_event_rows(conn, second["appointment_id"])
    second_dispatched = next(e for e in second_events if e["event_type"] == "dispatched")

    movement = conn.execute(
        "SELECT * FROM _technician_movements WHERE technician_id = 1 ORDER BY departed_at"
    ).fetchone()
    assert movement is not None

    home = execution.technician_home_point(conn, 1)
    destination = execution.location_point(
        conn, execution._appointment_job_location_id(conn, second["appointment_id"])[1]
    )
    second_block_start = clock.phoenix_instant(2026, 10, 1, 13, 0)
    planned_from_home = execution.planned_travel_minutes(
        conn, ARTIFACT, cfg, origin=home, destination=destination,
        service_date=WEEKDAY, minute_of_day=13 * 60,
    )
    naive_dispatch = second_block_start - planned_from_home * 60000

    assert movement["arrives_at"] > naive_dispatch
    assert second_dispatched["occurred_at"] == movement["arrives_at"]
    assert second_dispatched["occurred_at"] != naive_dispatch


def _set_clock(conn, ms: int, service_date: str) -> None:
    conn.execute(
        "UPDATE _clock SET current_time_ms = ?, service_date = ? WHERE singleton = 1",
        (ms, service_date),
    )


def test_gps_view_reports_interpolated_position_mid_trip_and_hides_trip_fields(tmp_path):
    conn, cfg, controller, db_path = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    appointment_id = booked["appointment_id"]

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT location_state FROM _technician_locations WHERE technician_id = 1"
        ).fetchone()["location_state"] == "traveling",
    )

    trip = _technician_location_row(conn, 1)
    departed_at = int(trip["trip_departed_at"])
    travel_ms = int(trip["trip_travel_minutes"]) * 60000
    mid_ms = departed_at + int(travel_ms * 0.6)
    _set_clock(conn, mid_ms, WEEKDAY)

    ping_interval_ms = cfg.get_int("geography.gps_ping_interval_minutes") * 60000
    ping_ms = departed_at + ((mid_ms - departed_at) // ping_interval_ms) * ping_interval_ms
    fraction = min(1.0, max(0.0, (ping_ms - departed_at) / travel_ms))
    expected_lat = trip["trip_origin_latitude"] + (
        trip["trip_destination_latitude"] - trip["trip_origin_latitude"]
    ) * fraction
    expected_lon = trip["trip_origin_longitude"] + (
        trip["trip_destination_longitude"] - trip["trip_origin_longitude"]
    ) * fraction

    agent = open_agent_session(db_path)
    try:
        row = execute_gated(
            agent, "SELECT * FROM technician_locations WHERE technician_id = 1"
        ).fetchone()
        assert set(row.keys()) == {
            "technician_id", "latitude", "longitude", "coordinates_updated_at", "location_state",
        }
        assert row["location_state"] == "traveling"
        assert row["coordinates_updated_at"] == ping_ms
        assert abs(row["latitude"] - expected_lat) < 1e-9
        assert abs(row["longitude"] - expected_lon) < 1e-9

        with pytest.raises(EngineError) as ei:
            execute_gated(
                agent, "SELECT trip_origin_latitude FROM _technician_locations"
            ).fetchall()
        assert ei.value.code is ErrorCode.UNAUTHORIZED

        arrival_ms = departed_at + travel_ms
        _set_clock(conn, arrival_ms + 1, WEEKDAY)
    finally:
        agent.close()

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT 1 FROM _appointment_events WHERE appointment_id = ? AND event_type = 'arrived'",
            (appointment_id,),
        ).fetchone() is not None,
    )
    destination = execution.location_point(
        conn, execution._appointment_job_location_id(conn, appointment_id)[1]
    )
    agent2 = open_agent_session(db_path)
    try:
        row = execute_gated(
            agent2, "SELECT * FROM technician_locations WHERE technician_id = 1"
        ).fetchone()
        assert row["location_state"] == "on_site"
        assert row["latitude"] == destination.latitude
        assert row["longitude"] == destination.longitude
    finally:
        agent2.close()


def test_gps_view_quantizes_by_configured_ping_interval_not_the_default(tmp_path):
    conn, cfg, controller, db_path = _build_run(
        tmp_path,
        **{
            "geography.gps_ping_interval_minutes": 15,
            "travel.speed_by_time_band": _SLOW_MORNING_SPEED_OVERRIDE,
        },
    )
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B07_09",
    )

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT location_state FROM _technician_locations WHERE technician_id = 1"
        ).fetchone()["location_state"] == "traveling",
    )

    trip = _technician_location_row(conn, 1)
    departed_at = int(trip["trip_departed_at"])
    travel_minutes = int(trip["trip_travel_minutes"])
    assert travel_minutes >= 20

    configured_ping_ms = cfg.get_int("geography.gps_ping_interval_minutes") * 60000
    assert configured_ping_ms == 900000
    default_ping_ms = 300000
    mid_ms = departed_at + 660000
    _set_clock(conn, mid_ms, WEEKDAY)

    configured_quantized = departed_at + (
        (mid_ms - departed_at) // configured_ping_ms
    ) * configured_ping_ms
    default_quantized = departed_at + (
        (mid_ms - departed_at) // default_ping_ms
    ) * default_ping_ms
    assert configured_quantized != default_quantized

    agent = open_agent_session(db_path)
    try:
        row = execute_gated(
            agent, "SELECT * FROM technician_locations WHERE technician_id = 1"
        ).fetchone()
        assert row["coordinates_updated_at"] == configured_quantized
    finally:
        agent.close()


def _ledger_off_hours_entries(conn, service_date):
    return conn.execute(
        "SELECT entry_type, amount_cents, technician_id, job_id FROM _ledger_entries "
        "WHERE service_date = ? AND entry_type IN ('payroll_off_hours', 'payroll_off_hours_bonus') "
        "AND description NOT LIKE 'overtime wages%' AND description != 'on-call wages' "
        "ORDER BY id",
        (service_date,),
    ).fetchall()


def _on_call_wage_entries(conn, service_date):
    return [
        (int(row["technician_id"]), int(row["amount_cents"]))
        for row in conn.execute(
            "SELECT technician_id, amount_cents FROM _ledger_entries "
            "WHERE service_date = ? AND entry_type = 'payroll_off_hours' "
            "AND description = 'on-call wages' ORDER BY id",
            (service_date,),
        )
    ]


def test_on_call_technicians_are_paid_every_on_call_hour_whether_or_not_they_work(tmp_path):
    conn, _cfg, controller, _ = _build_run(tmp_path, end_date=WEEKEND)

    status = controller.run()
    assert str(status) == "completed"

    assert _on_call_wage_entries(conn, WEEKDAY) == [
        (1, -2 * _wage(conn, 1)), (3, -2 * _wage(conn, 3)),
    ]
    assert _on_call_wage_entries(conn, WEEKEND) == [
        (1, -12 * _wage(conn, 1)), (3, -12 * _wage(conn, 3)),
    ]


def _off_hours_share_cents(conn, cfg, job_id):
    return basis_point_amount(
        int(_invoice_row(conn, job_id)["subtotal_cents"]),
        cfg.get_int("payroll.off_hours_share_basis_points"),
    )


def test_a_paid_weekend_job_pays_the_off_hours_share_on_top_of_on_call_wages(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path, end_date=WEEKEND)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B09_11",
        job_type_id=1,
    )

    status = controller.run()
    assert str(status) == "completed"

    assert _job_row(conn, booked["job_id"])["outcome"] == "completed_paid"
    entries = _ledger_off_hours_entries(conn, WEEKEND)
    assert [tuple(row) for row in entries] == [
        (
            "payroll_off_hours_bonus",
            -_off_hours_share_cents(conn, cfg, booked["job_id"]),
            1,
            booked["job_id"],
        )
    ]
    description = conn.execute(
        "SELECT description FROM _ledger_entries WHERE entry_type = 'payroll_off_hours_bonus'"
    ).fetchone()["description"]
    assert description == "off-hours technician share"


def test_a_paid_weekend_service_job_pays_one_share_and_no_block_wages(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path, end_date=WEEKEND)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B09_11",
    )

    status = controller.run()
    assert str(status) == "completed"

    entries = _ledger_off_hours_entries(conn, WEEKEND)
    assert [(row["entry_type"], row["amount_cents"]) for row in entries] == [
        ("payroll_off_hours_bonus", -_off_hours_share_cents(conn, cfg, booked["job_id"])),
    ]


def test_off_hours_settlement_weekday_after_hours_block(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B17_19",
    )
    mark = conn.execute(
        "SELECT scheduled_at_ms FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.OFF_HOURS_ACCRUAL_MARK.value, booked["appointment_id"]),
    ).fetchone()
    assert mark["scheduled_at_ms"] == clock.phoenix_instant(2026, 10, 1, 17, 0)

    status = controller.run()
    assert str(status) == "completed"

    entries = _ledger_off_hours_entries(conn, WEEKDAY)
    assert [(row["entry_type"], row["amount_cents"]) for row in entries] == [
        ("payroll_off_hours_bonus", -_off_hours_share_cents(conn, cfg, booked["job_id"])),
    ]


def test_off_hours_settlement_reassignment_before_block_moves_the_accrual(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path, end_date=WEEKEND)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B09_11",
        requested_category_id=PLUMBING_CATEGORY_ID,
    )
    request_id = privileged_request_id("assign", next(_request_seq))
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.assign(
            ctx, cfg, appointment_id=booked["appointment_id"], technician_id=3, artifact=ARTIFACT,
        ),
    )
    body = json.loads(response)
    assert body["ok"] is True, body

    status = controller.run()
    assert str(status) == "completed"

    entries = _ledger_off_hours_entries(conn, WEEKEND)
    assert [(row["technician_id"], row["amount_cents"]) for row in entries] == [
        (3, -_off_hours_share_cents(conn, cfg, booked["job_id"])),
    ]


def test_abandoned_trip_arrived_not_started_reassignment_replans_the_chain(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path,
        **{
            "travel.speed_by_time_band": _SLOW_MIDDAY_SPEED_OVERRIDE,
            "travel.noise.max_multiplier": 0.75,
        },
    )
    first = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=DESTINATION_ADDRESS.address_id,
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B15_17",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    first_appointment_id = first["appointment_id"]
    second_appointment_id = second["appointment_id"]

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT 1 FROM _appointment_events WHERE appointment_id = ? AND event_type = 'arrived'",
            (first_appointment_id,),
        ).fetchone() is not None,
    )
    work_started_pending = conn.execute(
        "SELECT event_id FROM _events WHERE kind = ? AND entity_id = ? AND status = 'pending'",
        (EventKind.WORK_STARTED.value, first_appointment_id),
    ).fetchone()
    assert work_started_pending is not None
    assert conn.execute(
        "SELECT location_state FROM _technician_locations WHERE technician_id = 1"
    ).fetchone()["location_state"] == "on_site"

    request_id = privileged_request_id("assign", next(_request_seq))
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.assign(
            ctx, cfg, appointment_id=first_appointment_id, technician_id=3, artifact=ARTIFACT,
        ),
    )
    body = json.loads(response)
    assert body["ok"] is True, body

    cancelled = conn.execute(
        "SELECT status FROM _events WHERE event_id = ?", (work_started_pending["event_id"],)
    ).fetchone()
    assert cancelled["status"] == "cancelled"

    new_dispatch = conn.execute(
        "SELECT payload FROM _events WHERE kind = ? AND entity_id = ? AND status = 'pending'",
        (EventKind.APPOINTMENT_DISPATCH.value, first_appointment_id),
    ).fetchone()
    assert new_dispatch is not None
    assert json.loads(new_dispatch["payload"])["technician_id"] == 3

    status = controller.run()
    assert str(status) == "completed"

    first_events = _appointment_event_rows(conn, first_appointment_id)
    reassigned_dispatch = [
        e for e in first_events if e["event_type"] == "dispatched" and e["technician_id"] == 3
    ]
    assert len(reassigned_dispatch) == 1

    first_appt = conn.execute(
        "SELECT status FROM _appointments WHERE id = ?", (first_appointment_id,)
    ).fetchone()
    assert first_appt["status"] == "failed"
    first_job = _job_row(conn, first["job_id"])
    assert first_job["outcome"] == "failed_unqualified"

    second_appt = conn.execute(
        "SELECT status FROM _appointments WHERE id = ?", (second_appointment_id,)
    ).fetchone()
    assert second_appt["status"] == "completed"

    day = conn.execute(
        "SELECT unresolved_execution_count FROM _day_state WHERE service_date = ?", (WEEKDAY,)
    ).fetchone()
    assert day["unresolved_execution_count"] == 0


def test_abandoned_trip_en_route_reassignment_turns_technician_back(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        requested_category_id=PLUMBING_CATEGORY_ID,
    )
    appointment_id = booked["appointment_id"]

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT location_state FROM _technician_locations WHERE technician_id = 1"
        ).fetchone()["location_state"] == "traveling",
    )
    trip_before = _technician_location_row(conn, 1)
    departed_at = int(trip_before["trip_departed_at"])
    travel_ms = int(trip_before["trip_travel_minutes"]) * 60000
    home = execution.technician_home_point(conn, 1)
    assert trip_before["trip_origin_latitude"] == home.latitude
    assert trip_before["trip_origin_longitude"] == home.longitude

    now = departed_at + travel_ms // 3
    _set_clock(conn, now, WEEKDAY)

    request_id = privileged_request_id("assign", next(_request_seq))
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.assign(
            ctx, cfg, appointment_id=appointment_id, technician_id=3, artifact=ARTIFACT,
        ),
    )
    body = json.loads(response)
    assert body["ok"] is True, body

    original_arrival = conn.execute(
        "SELECT event_id, status FROM _events WHERE kind = ? AND entity_id = ? "
        "AND scheduled_at_ms = ?",
        (EventKind.APPOINTMENT_ARRIVAL.value, appointment_id, departed_at + travel_ms),
    ).fetchone()
    assert original_arrival["status"] == "cancelled"

    outgoing = _technician_location_row(conn, 1)
    assert outgoing["location_state"] == "traveling"
    elapsed_ms = now - departed_at
    ready_at = now + elapsed_ms
    assert outgoing["trip_departed_at"] == now
    assert outgoing["trip_travel_minutes"] == max(elapsed_ms // 60000, 1)
    assert outgoing["trip_destination_latitude"] == home.latitude
    assert outgoing["trip_destination_longitude"] == home.longitude

    home_arrival_pending = conn.execute(
        "SELECT scheduled_at_ms FROM _events WHERE kind = ? AND entity_id = 1 AND status = 'pending'",
        (EventKind.TECHNICIAN_HOME_ARRIVAL.value,),
    ).fetchone()
    assert home_arrival_pending is not None
    assert home_arrival_pending["scheduled_at_ms"] == ready_at

    new_dispatch = conn.execute(
        "SELECT payload FROM _events WHERE kind = ? AND entity_id = ? AND status = 'pending'",
        (EventKind.APPOINTMENT_DISPATCH.value, appointment_id),
    ).fetchone()
    assert new_dispatch is not None
    payload = json.loads(new_dispatch["payload"])
    assert payload["technician_id"] == 3
    assert appointments.trip_index_for_appointment(conn, appointment_id) == 1

    status = controller.run()
    assert str(status) == "completed"

    appt = conn.execute(
        "SELECT status FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    assert appt["status"] == "completed"

    events = _appointment_event_rows(conn, appointment_id)
    dispatched = [e for e in events if e["event_type"] == "dispatched"][-1]
    arrived = [e for e in events if e["event_type"] == "arrived"][-1]
    assert dispatched["technician_id"] == 3

    _job_id, location_id = execution._appointment_job_location_id(conn, appointment_id)
    destination = execution.location_point(conn, location_id)
    distance_km = geography.route_distance_km(
        home.catalog_point.access_distance_km, home.catalog_point.anchor_id,
        destination.catalog_point.access_distance_km, destination.catalog_point.anchor_id,
        ARTIFACT, cfg,
    )
    condition_code = ConditionCode(
        conn.execute(
            "SELECT condition_code FROM _zone_weather_daily WHERE zone_id = ? AND local_date = ?",
            (destination.effective_zone_id, WEEKDAY),
        ).fetchone()["condition_code"]
    )
    departure_local = clock.phoenix_local(dispatched["occurred_at"])
    departure_minute = departure_local.hour * 60 + departure_local.minute
    original_noise_key = travel.inbound_trip_noise_key(origin_keys.of_appointment(conn, appointment_id), WEEKDAY, 0)
    new_noise_key = travel.inbound_trip_noise_key(origin_keys.of_appointment(conn, appointment_id), WEEKDAY, 1)
    would_be_original_minutes = travel.actual_travel_minutes(
        _shadow_rng(), cfg, distance_km=distance_km, day_type="weekday",
        minute_of_day=departure_minute, condition_code=condition_code, noise_key=original_noise_key,
    )
    actual_new_minutes = travel.actual_travel_minutes(
        _shadow_rng(), cfg, distance_km=distance_km, day_type="weekday",
        minute_of_day=departure_minute, condition_code=condition_code, noise_key=new_noise_key,
    )
    assert arrived["occurred_at"] == dispatched["occurred_at"] + actual_new_minutes * 60000
    assert original_noise_key != new_noise_key
    if would_be_original_minutes != actual_new_minutes:
        assert dispatched["occurred_at"] + would_be_original_minutes * 60000 != arrived["occurred_at"]


def test_reassign_during_a_same_address_chain_trip_turns_back_to_the_true_origin(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    first = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        origin_key=EARLY_FINISH_ORIGIN,
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        requested_category_id=PLUMBING_CATEGORY_ID,
    )
    first_id = first["appointment_id"]
    second_id = second["appointment_id"]
    shadow = _shadow_rng()
    assert execution.start_variation_minutes(shadow, EARLY_FINISH_ORIGIN, WEEKDAY) < 0
    assert execution.work_duration_minutes(
        shadow, EARLY_FINISH_ORIGIN,
        cfg.resolved["execution.duration_minutes_by_job_type"]["hvac_service"],
    ) < 60

    first_point = execution.location_point(
        conn, execution._appointment_job_location_id(conn, first_id)[1]
    )
    _relocate_job_location(conn, second_id, first_point.latitude, first_point.longitude)

    _step_until(
        controller,
        lambda: _pending_event(conn, EventKind.APPOINTMENT_ARRIVAL, second_id) is not None,
    )
    assert _appointment_status(conn, first_id) == "completed"

    trip = _technician_location_row(conn, 1)
    assert trip["trip_origin_latitude"] == first_point.latitude
    assert trip["trip_origin_longitude"] == first_point.longitude
    departed_at = int(trip["trip_departed_at"])
    travel_ms = int(trip["trip_travel_minutes"]) * 60000
    now = departed_at + travel_ms // 3
    _set_clock(conn, now, WEEKDAY)

    request_id = privileged_request_id("assign", next(_request_seq))
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.assign(
            ctx, cfg, appointment_id=second_id, technician_id=3, artifact=ARTIFACT,
        ),
    )
    body = json.loads(response)
    assert body["ok"] is True, body

    outgoing = _technician_location_row(conn, 1)
    assert outgoing["location_state"] == "traveling"
    assert outgoing["trip_destination_latitude"] == first_point.latitude
    assert outgoing["trip_destination_longitude"] == first_point.longitude

    elapsed_ms = now - departed_at
    ready_at = now + elapsed_ms
    return_home = conn.execute(
        "SELECT scheduled_at_ms, payload FROM _events WHERE kind = ? "
        "AND status = 'pending' AND entity_id = 1",
        (EventKind.TECHNICIAN_RETURN_HOME.value,),
    ).fetchone()
    assert return_home is not None
    assert return_home["scheduled_at_ms"] == ready_at
    payload = json.loads(return_home["payload"])
    assert payload["appointment_id"] == first_id
    assert payload["block_index"] == execution.block_index("B09_11")

    new_dispatch = _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, second_id)
    assert new_dispatch is not None
    assert json.loads(new_dispatch["payload"])["technician_id"] == 3

    status = controller.run()
    assert str(status) == "completed"
    assert _appointment_status(conn, second_id) == "completed"
    assert _technician_location_row(conn, 1)["location_state"] == "home"
    assert _day_state_row(conn, WEEKDAY)["unresolved_execution_count"] == 0


def test_abandoned_trip_en_route_reassignment_at_dispatch_instant_keeps_position_visible(tmp_path):
    conn, cfg, controller, db_path = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
    )
    appointment_id = booked["appointment_id"]

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT location_state FROM _technician_locations WHERE technician_id = 1"
        ).fetchone()["location_state"] == "traveling",
    )
    trip_before = _technician_location_row(conn, 1)
    departed_at = int(trip_before["trip_departed_at"])
    _set_clock(conn, departed_at, WEEKDAY)

    request_id = privileged_request_id("assign", next(_request_seq))
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.assign(
            ctx, cfg, appointment_id=appointment_id, technician_id=3, artifact=ARTIFACT,
        ),
    )
    body = json.loads(response)
    assert body["ok"] is True, body

    outgoing = _technician_location_row(conn, 1)
    assert outgoing["location_state"] == "traveling"
    assert outgoing["trip_travel_minutes"] >= 1

    agent = open_agent_session(db_path)
    try:
        row = execute_gated(
            agent, "SELECT * FROM technician_locations WHERE technician_id = 1"
        ).fetchone()
        assert row["location_state"] == "traveling"
        assert row["latitude"] is not None
        assert row["longitude"] is not None
    finally:
        agent.close()


def test_draw_isolation_a_second_bookings_does_not_perturb_the_first(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    conn1, cfg1, controller1, _ = _build_run(tmp_path / "a")
    first1 = _book_execution(
        controller1, cfg1, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    status1 = controller1.run()
    assert str(status1) == "completed"
    events1 = _appointment_event_rows(conn1, first1["appointment_id"])

    conn2, cfg2, controller2, _ = _build_run(tmp_path / "b")
    first2 = _book_execution(
        controller2, cfg2, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    _book_execution(
        controller2, cfg2, technician_id=3, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    status2 = controller2.run()
    assert str(status2) == "completed"
    events2 = _appointment_event_rows(conn2, first2["appointment_id"])

    assert [(e["event_type"], e["occurred_at"]) for e in events1] == [
        (e["event_type"], e["occurred_at"]) for e in events2
    ]


def _twin_bookings(controller, cfg):
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    _book_execution(
        controller, cfg, technician_id=3, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )


def _run_twin_to_completion(path, *, max_steps=None):
    conn, cfg, controller, db_path = _build_run(path)
    _twin_bookings(controller, cfg)
    status = controller.run(max_steps=max_steps)
    return conn, cfg, controller, db_path, status


def test_twin_determinism_same_seed_same_bookings_identical_dump(tmp_path):
    global _id_seq, _request_seq
    saved_id_seq, saved_request_seq = _id_seq, _request_seq
    try:
        (tmp_path / "a").mkdir()
        _id_seq, _request_seq = count(9500), count(1)
        conn_a, _cfg_a, _controller_a, db_path_a, status_a = _run_twin_to_completion(
            tmp_path / "a"
        )
        assert str(status_a) == "completed"
        run_id_a = conn_a.execute("SELECT run_id FROM _run").fetchone()["run_id"]
        hash_a = dump_hash(conn_a, run_id_a)

        (tmp_path / "b").mkdir()
        _id_seq, _request_seq = count(9500), count(1)
        conn_b, _cfg_b, _controller_b, _db_path_b, status_b = _run_twin_to_completion(
            tmp_path / "b"
        )
        assert str(status_b) == "completed"
        run_id_b = conn_b.execute("SELECT run_id FROM _run").fetchone()["run_id"]
        hash_b = dump_hash(conn_b, run_id_b)
        assert hash_a == hash_b
        conn_b.close()

        (tmp_path / "c").mkdir()
        _id_seq, _request_seq = count(9500), count(1)
        conn_c, cfg_c, controller_c, db_path_c, status_c = _run_twin_to_completion(
            tmp_path / "c", max_steps=25
        )
        assert str(status_c) == "active"
        run_id_c = conn_c.execute("SELECT run_id FROM _run").fetchone()["run_id"]
        conn_c.close()

        resumed_path = tmp_path / "c" / "resumed.db"
        shutil.copyfile(db_path_c, resumed_path)
        conn_resumed = open_run_db(resumed_path, artifact_dir=GEO_ARTIFACT_DIR)
        register_conn(conn_resumed)
        status_resumed = Controller(
            conn_resumed, cfg_c, ScriptedNoOpAdapter(), artifact_dir=GEO_ARTIFACT_DIR
        ).run()
        assert str(status_resumed) == "completed"
        hash_resumed = dump_hash(conn_resumed, run_id_c)
        assert hash_resumed == hash_a
    finally:
        _id_seq, _request_seq = saved_id_seq, saved_request_seq


def _guard_event(conn, appointment_id):
    return conn.execute(
        "SELECT event_id, scheduled_at_ms, status, payload FROM _events "
        "WHERE kind = ? AND entity_id = ?",
        (EventKind.COMPANY_NO_SHOW.value, appointment_id),
    ).fetchone()


def _pending_event(conn, kind, entity_id):
    return conn.execute(
        "SELECT event_id, scheduled_at_ms, payload FROM _events "
        "WHERE kind = ? AND entity_id = ? AND status = 'pending'",
        (kind.value, entity_id),
    ).fetchone()


def _notification_rows(conn):
    return conn.execute(
        "SELECT created_at, service_date, notification_type, entity_type, entity_id, message "
        "FROM _notifications ORDER BY id"
    ).fetchall()


def _appointment_status(conn, appointment_id):
    return conn.execute(
        "SELECT status FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()["status"]


def _hold_count(conn, appointment_id):
    return int(
        conn.execute(
            "SELECT COUNT(*) c FROM _technician_block_holds WHERE appointment_id = ?",
            (appointment_id,),
        ).fetchone()["c"]
    )


def _day_state_row(conn, service_date):
    return conn.execute(
        "SELECT * FROM _day_state WHERE service_date = ?",
        (service_date,),
    ).fetchone()


def _latest_terminal_execution_at(conn, service_date):
    return conn.execute(
        "SELECT MAX(occurred_at) AS latest FROM _appointment_events WHERE service_date = ? "
        "AND event_type IN ('work_completed', 'work_failed', 'no_show', 'cancelled')",
        (service_date,),
    ).fetchone()["latest"]


def _move_pending_event(conn, event_id, scheduled_at_ms):
    moved = conn.execute(
        "UPDATE _events SET scheduled_at_ms = ? WHERE event_id = ? AND status = 'pending'",
        (scheduled_at_ms, event_id),
    )
    assert moved.rowcount == 1
    conn.commit()


def _retime_pending_arrival(conn, *, appointment_id, technician_id, arrival_ms):
    arrival = _pending_event(conn, EventKind.APPOINTMENT_ARRIVAL, appointment_id)
    assert arrival is not None
    departed_at = int(_technician_location_row(conn, technician_id)["trip_departed_at"])
    assert (arrival_ms - departed_at) % 60000 == 0
    conn.execute(
        "UPDATE _technician_locations SET trip_travel_minutes = ? WHERE technician_id = ?",
        ((arrival_ms - departed_at) // 60000, technician_id),
    )
    _move_pending_event(conn, int(arrival["event_id"]), arrival_ms)


def _relocate_job_location(conn, appointment_id, latitude, longitude):
    _job_id, location_id = execution._appointment_job_location_id(conn, appointment_id)
    conn.execute(
        "UPDATE _locations SET latitude = ?, longitude = ? WHERE id = ?",
        (latitude, longitude, location_id),
    )
    conn.commit()


def _cancel_event(controller, event_id):
    request_id = privileged_request_id("cancel_event", next(_request_seq))
    response = privileged(
        controller.services, request_id,
        lambda ctx: Events(ctx.conn, ctx.rng).cancel(event_id),
    )
    body = json.loads(response)
    assert body["ok"] is True, body


def _pause_before_event(controller, kind, entity_id):
    while True:
        upcoming = controller.events.next_pending()
        assert upcoming is not None, "ran out of events before reaching the target"
        if EventKind(upcoming["kind"]) is kind and upcoming["entity_id"] == entity_id:
            return
        assert controller.step()


def test_no_show_cutoff_is_a_no_op_when_work_completes(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    appointment_id = booked["appointment_id"]

    guard = _guard_event(conn, appointment_id)
    assert guard is not None
    assert guard["status"] == "pending"
    assert guard["scheduled_at_ms"] == clock.phoenix_instant(2026, 10, 1, 11, 0)
    assert json.loads(guard["payload"]) == {"appointment_id": appointment_id}

    status = controller.run()
    assert str(status) == "completed"

    assert _guard_event(conn, appointment_id)["status"] == "applied"
    assert _appointment_status(conn, appointment_id) == "completed"
    events = _appointment_event_rows(conn, appointment_id)
    assert [e["event_type"] for e in events] == [
        "dispatched", "arrived", "work_started", "work_completed",
    ]
    assert _hold_count(conn, appointment_id) == 1
    assert _notification_rows(conn) == []


def test_cascade_overrun_no_shows_the_next_appointment_at_its_cutoff(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"appointments.min_actual_duration_minutes": 260}
    )
    first = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    second_id = second["appointment_id"]
    cutoff = clock.phoenix_instant(2026, 10, 1, 13, 0)

    status = controller.run()
    assert str(status) == "completed"

    first_appt = conn.execute(
        "SELECT status, actual_start, actual_end FROM _appointments WHERE id = ?",
        (first["appointment_id"],),
    ).fetchone()
    assert first_appt["status"] == "completed"
    assert first_appt["actual_end"] - first_appt["actual_start"] == 260 * 60000
    assert first_appt["actual_end"] > cutoff

    second_appt = conn.execute(
        "SELECT status, appointment_number FROM _appointments WHERE id = ?", (second_id,)
    ).fetchone()
    assert second_appt["status"] == "no_show"
    second_events = _appointment_event_rows(conn, second_id)
    assert [(e["event_type"], e["occurred_at"]) for e in second_events] == [("no_show", cutoff)]
    assert second_events[0]["technician_id"] == 1

    assert _hold_count(conn, second_id) == 0
    blocks = conn.execute(
        "SELECT COUNT(*) c FROM _appointment_blocks WHERE appointment_id = ?", (second_id,)
    ).fetchone()
    assert blocks["c"] == 1

    notifications = _notification_rows(conn)
    assert len(notifications) == 1
    note = notifications[0]
    assert note["created_at"] == cutoff
    assert note["service_date"] == WEEKDAY
    assert note["notification_type"] == "company_no_show"
    assert note["entity_type"] == "appointment"
    assert note["entity_id"] == second_id
    assert second_appt["appointment_number"] in note["message"]
    assert WEEKDAY in note["message"]

    day = _day_state_row(conn, WEEKDAY)
    assert day["unresolved_execution_count"] == 0
    assert day["closing_at"] == clock.phoenix_instant(2026, 10, 1, 19, 0)


def test_no_show_cancels_a_pending_dispatch_and_replans_the_next_appointment(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    first = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B15_17",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    first_id = first["appointment_id"]
    second_id = second["appointment_id"]
    cutoff = clock.phoenix_instant(2026, 10, 1, 13, 0)

    _step_until(
        controller,
        lambda: _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, first_id) is not None,
    )
    dispatch = _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, first_id)
    _move_pending_event(conn, int(dispatch["event_id"]), cutoff + 30 * 60000)

    _step_until(controller, lambda: _appointment_status(conn, first_id) == "no_show")

    assert conn.execute(
        "SELECT status FROM _events WHERE event_id = ?", (dispatch["event_id"],)
    ).fetchone()["status"] == "cancelled"

    replanned = _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, second_id)
    assert replanned is not None
    assert json.loads(replanned["payload"])["technician_id"] == 1

    home = execution.technician_home_point(conn, 1)
    destination = execution.location_point(
        conn, execution._appointment_job_location_id(conn, second_id)[1]
    )
    planned = execution.planned_travel_minutes(
        conn, ARTIFACT, cfg, origin=home, destination=destination,
        service_date=WEEKDAY, minute_of_day=15 * 60,
    )
    second_block_start = clock.phoenix_instant(2026, 10, 1, 15, 0)
    assert replanned["scheduled_at_ms"] == max(second_block_start - planned * 60000, cutoff)

    status = controller.run()
    assert str(status) == "completed"
    assert _appointment_status(conn, second_id) == "completed"
    assert _day_state_row(conn, WEEKDAY)["unresolved_execution_count"] == 0


def test_no_show_with_no_remaining_appointment_plans_nothing(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
    )
    appointment_id = booked["appointment_id"]
    cutoff = clock.phoenix_instant(2026, 10, 1, 13, 0)

    _step_until(
        controller,
        lambda: _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, appointment_id) is not None,
    )
    dispatch = _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, appointment_id)
    _move_pending_event(conn, int(dispatch["event_id"]), cutoff + 30 * 60000)

    status = controller.run()
    assert str(status) == "completed"

    assert _appointment_status(conn, appointment_id) == "no_show"
    assert conn.execute(
        "SELECT status FROM _events WHERE event_id = ?", (dispatch["event_id"],)
    ).fetchone()["status"] == "cancelled"
    assert conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = ?",
        (EventKind.APPOINTMENT_DISPATCH.value,),
    ).fetchone()["c"] == 1
    events = _appointment_event_rows(conn, appointment_id)
    assert [(e["event_type"], e["occurred_at"]) for e in events] == [("no_show", cutoff)]
    assert _technician_location_row(conn, 1)["location_state"] == "home"
    day = _day_state_row(conn, WEEKDAY)
    assert day["closing_at"] == clock.phoenix_instant(2026, 10, 1, 19, 0)
    assert day["last_execution_completed_at"] == cutoff


def test_arrival_after_cutoff_completes_movement_and_chains_to_the_consecutive_block(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    first = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    first_id = first["appointment_id"]
    second_id = second["appointment_id"]
    _relocate_job_location(conn, second_id, 33.45, -111.95)
    cutoff = clock.phoenix_instant(2026, 10, 1, 11, 0)
    late_arrival = clock.phoenix_instant(2026, 10, 1, 11, 20)

    _step_until(
        controller,
        lambda: _technician_location_row(conn, 1)["location_state"] == "traveling",
    )
    _retime_pending_arrival(
        conn, appointment_id=first_id, technician_id=1, arrival_ms=late_arrival
    )

    _step_until(controller, lambda: _appointment_status(conn, first_id) == "no_show")
    no_show_events = _appointment_event_rows(conn, first_id)
    assert [e["event_type"] for e in no_show_events] == ["dispatched", "no_show"]
    assert no_show_events[-1]["occurred_at"] == cutoff
    assert _pending_event(conn, EventKind.APPOINTMENT_ARRIVAL, first_id) is not None
    assert _hold_count(conn, first_id) == 0

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT 1 FROM _appointment_events WHERE appointment_id = ? "
            "AND event_type = 'dispatched'",
            (second_id,),
        ).fetchone() is not None,
    )
    assert [e["event_type"] for e in _appointment_event_rows(conn, first_id)] == [
        "dispatched", "no_show",
    ]
    second_dispatched = conn.execute(
        "SELECT occurred_at FROM _appointment_events WHERE appointment_id = ? "
        "AND event_type = 'dispatched'",
        (second_id,),
    ).fetchone()
    assert second_dispatched["occurred_at"] == late_arrival

    first_point = execution.location_point(
        conn, execution._appointment_job_location_id(conn, first_id)[1]
    )
    trip = _technician_location_row(conn, 1)
    assert trip["location_state"] == "traveling"
    assert trip["trip_origin_latitude"] == first_point.latitude
    assert trip["trip_origin_longitude"] == first_point.longitude
    assert trip["trip_destination_latitude"] == 33.45
    assert trip["trip_destination_longitude"] == -111.95
    assert trip["trip_departed_at"] == late_arrival

    status = controller.run()
    assert str(status) == "completed"

    assert [e["event_type"] for e in _appointment_event_rows(conn, first_id)] == [
        "dispatched", "no_show",
    ]
    assert conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.WORK_STARTED.value, first_id),
    ).fetchone()["c"] == 0
    assert _appointment_status(conn, second_id) == "completed"
    assert len(_notification_rows(conn)) == 1
    assert _day_state_row(conn, WEEKDAY)["unresolved_execution_count"] == 0


def test_install_no_show_fires_at_its_window_end_releases_its_hold_and_draws_nothing(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=2,
    )
    appointment_id = booked["appointment_id"]
    cutoff = clock.phoenix_instant(2026, 10, 1, 11, 0)

    assert _guard_event(conn, appointment_id)["scheduled_at_ms"] == cutoff
    assert _hold_count(conn, appointment_id) == 1

    _step_until(
        controller,
        lambda: _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, appointment_id) is not None,
    )
    dispatch = _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, appointment_id)
    _cancel_event(controller, int(dispatch["event_id"]))

    _pause_before_event(controller, EventKind.COMPANY_NO_SHOW, appointment_id)
    draws_before = audit_count(conn)
    assert controller.step()
    assert audit_count(conn) == draws_before

    assert _appointment_status(conn, appointment_id) == "no_show"
    assert _hold_count(conn, appointment_id) == 0
    blocks = conn.execute(
        "SELECT block_code FROM _appointment_blocks WHERE appointment_id = ? ORDER BY ordinal",
        (appointment_id,),
    ).fetchall()
    assert [b["block_code"] for b in blocks] == ["B09_11"]
    events = _appointment_event_rows(conn, appointment_id)
    assert [(e["event_type"], e["occurred_at"]) for e in events] == [("no_show", cutoff)]

    status = controller.run()
    assert str(status) == "completed"
    assert _day_state_row(conn, WEEKDAY)["unresolved_execution_count"] == 0


def test_work_started_at_the_exact_cutoff_instant_beats_the_no_show(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    appointment_id = booked["appointment_id"]
    cutoff = clock.phoenix_instant(2026, 10, 1, 11, 0)

    _step_until(
        controller,
        lambda: _technician_location_row(conn, 1)["location_state"] == "traveling",
    )
    _retime_pending_arrival(
        conn, appointment_id=appointment_id, technician_id=1, arrival_ms=cutoff
    )

    status = controller.run()
    assert str(status) == "completed"

    events = _appointment_event_rows(conn, appointment_id)
    assert [e["event_type"] for e in events] == [
        "dispatched", "arrived", "work_started", "work_completed",
    ]
    assert events[1]["occurred_at"] == cutoff
    assert events[2]["occurred_at"] == cutoff

    appt = conn.execute(
        "SELECT status, lateness_minutes FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    assert appt["status"] == "completed"
    assert appt["lateness_minutes"] == 110
    assert _guard_event(conn, appointment_id)["status"] == "applied"
    assert _hold_count(conn, appointment_id) == 1
    assert _notification_rows(conn) == []


def test_lateness_zero_at_the_grace_boundary_and_positive_beyond_it(tmp_path):
    conn, cfg, controller, db_path = _build_run(tmp_path)
    grace = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    late = _book_execution(
        controller, cfg, technician_id=3, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    grace_id = grace["appointment_id"]
    late_id = late["appointment_id"]
    grace_start = clock.phoenix_instant(2026, 10, 1, 9, 10)
    late_start = clock.phoenix_instant(2026, 10, 1, 13, 27)

    _step_until(
        controller,
        lambda: _pending_event(conn, EventKind.WORK_STARTED, grace_id) is not None,
    )
    assert clock.now(conn) < grace_start
    _move_pending_event(
        conn,
        int(_pending_event(conn, EventKind.WORK_STARTED, grace_id)["event_id"]),
        grace_start,
    )

    _step_until(
        controller,
        lambda: _pending_event(conn, EventKind.WORK_STARTED, late_id) is not None,
    )
    assert clock.now(conn) < late_start
    _move_pending_event(
        conn,
        int(_pending_event(conn, EventKind.WORK_STARTED, late_id)["event_id"]),
        late_start,
    )

    status = controller.run()
    assert str(status) == "completed"

    rows = {
        row["id"]: row
        for row in conn.execute(
            "SELECT id, status, actual_start, lateness_minutes FROM _appointments"
        ).fetchall()
    }
    assert rows[grace_id]["status"] == "completed"
    assert rows[grace_id]["actual_start"] == grace_start
    assert rows[grace_id]["lateness_minutes"] == 0
    assert rows[late_id]["status"] == "completed"
    assert rows[late_id]["actual_start"] == late_start
    assert rows[late_id]["lateness_minutes"] == 17

    agent = open_agent_session(db_path)
    try:
        visible = {
            row["id"]: row["lateness_minutes"]
            for row in execute_gated(
                agent, "SELECT id, lateness_minutes FROM appointments"
            ).fetchall()
        }
        assert visible[grace_id] == 0
        assert visible[late_id] == 17
    finally:
        agent.close()


def test_an_off_hours_no_show_keeps_the_accrual_mark_and_posts_no_share(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path, end_date=WEEKEND)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B09_11",
    )
    appointment_id = booked["appointment_id"]

    _step_until(
        controller,
        lambda: _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, appointment_id) is not None,
    )
    dispatch = _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, appointment_id)
    _cancel_event(controller, int(dispatch["event_id"]))

    wage = int(
        conn.execute("SELECT hourly_wage_cents FROM _technicians WHERE id = 1").fetchone()[0]
    )

    status = controller.run()
    assert str(status) == "completed"

    assert _appointment_status(conn, appointment_id) == "no_show"
    mark = conn.execute(
        "SELECT status, payload FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.OFF_HOURS_ACCRUAL_MARK.value, appointment_id),
    ).fetchone()
    assert mark["status"] == "applied"
    assert json.loads(mark["payload"])["technician_id"] == 1

    assert _ledger_off_hours_entries(conn, WEEKEND) == []
    assert (1, -12 * wage) in _on_call_wage_entries(conn, WEEKEND)
    assert _invoice_row(conn, booked["job_id"]) is None

    notifications = _notification_rows(conn)
    assert len(notifications) == 1
    assert notifications[0]["service_date"] == WEEKEND


def test_draw_isolation_a_no_show_does_not_perturb_an_unrelated_appointment(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    conn1, cfg1, controller1, _ = _build_run(tmp_path / "a")
    solo = _book_execution(
        controller1, cfg1, technician_id=3, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    status1 = controller1.run()
    assert str(status1) == "completed"
    events1 = _appointment_event_rows(conn1, solo["appointment_id"])
    assert [e["event_type"] for e in events1] == [
        "dispatched", "arrived", "work_started", "work_completed",
    ]

    conn2, cfg2, controller2, _ = _build_run(tmp_path / "b")
    paired = _book_execution(
        controller2, cfg2, technician_id=3, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    doomed = _book_execution(
        controller2, cfg2, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    assert paired["appointment_id"] == solo["appointment_id"]

    _step_until(
        controller2,
        lambda: _pending_event(
            conn2, EventKind.APPOINTMENT_DISPATCH, doomed["appointment_id"]
        ) is not None,
    )
    dispatch = _pending_event(conn2, EventKind.APPOINTMENT_DISPATCH, doomed["appointment_id"])
    _cancel_event(controller2, int(dispatch["event_id"]))
    status2 = controller2.run()
    assert str(status2) == "completed"

    assert _appointment_status(conn2, doomed["appointment_id"]) == "no_show"
    events2 = _appointment_event_rows(conn2, paired["appointment_id"])
    assert [(e["event_type"], e["occurred_at"]) for e in events1] == [
        (e["event_type"], e["occurred_at"]) for e in events2
    ]


def test_no_show_cutoff_no_ops_on_a_terminal_appointment(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    appointment_id = booked["appointment_id"]
    conn.execute("UPDATE _appointments SET status = 'failed' WHERE id = ?", (appointment_id,))
    conn.commit()

    status = controller.run()
    assert str(status) == "completed"

    assert _guard_event(conn, appointment_id)["status"] == "applied"
    assert _appointment_status(conn, appointment_id) == "failed"
    assert _appointment_event_rows(conn, appointment_id) == []
    assert _hold_count(conn, appointment_id) == 1
    assert _notification_rows(conn) == []
    assert _technician_location_row(conn, 1)["location_state"] == "home"


def test_reassign_during_a_post_no_show_chain_trip_turns_back_to_the_no_show_location(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    first = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=2,
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
        requested_category_id=PLUMBING_CATEGORY_ID,
    )
    first_id = first["appointment_id"]
    second_id = second["appointment_id"]
    _relocate_job_location(conn, second_id, 33.45, -111.95)
    late_arrival = clock.phoenix_instant(2026, 10, 1, 11, 20)

    _step_until(
        controller,
        lambda: _technician_location_row(conn, 1)["location_state"] == "traveling",
    )
    _retime_pending_arrival(
        conn, appointment_id=first_id, technician_id=1, arrival_ms=late_arrival
    )

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT 1 FROM _appointment_events WHERE appointment_id = ? "
            "AND event_type = 'dispatched'",
            (second_id,),
        ).fetchone() is not None,
    )
    assert _appointment_status(conn, first_id) == "no_show"

    trip = _technician_location_row(conn, 1)
    departed_at = int(trip["trip_departed_at"])
    assert departed_at == late_arrival
    travel_ms = int(trip["trip_travel_minutes"]) * 60000
    now = departed_at + travel_ms // 3
    assert now < clock.phoenix_instant(2026, 10, 1, 13, 0)
    _set_clock(conn, now, WEEKDAY)

    request_id = privileged_request_id("assign", next(_request_seq))
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.assign(
            ctx, cfg, appointment_id=second_id, technician_id=3, artifact=ARTIFACT,
        ),
    )
    body = json.loads(response)
    assert body["ok"] is True, body

    first_point = execution.location_point(
        conn, execution._appointment_job_location_id(conn, first_id)[1]
    )
    home = execution.technician_home_point(conn, 1)
    outgoing = _technician_location_row(conn, 1)
    assert outgoing["location_state"] == "traveling"
    assert outgoing["trip_destination_latitude"] == first_point.latitude
    assert outgoing["trip_destination_longitude"] == first_point.longitude
    assert (
        outgoing["trip_destination_latitude"], outgoing["trip_destination_longitude"]
    ) != (home.latitude, home.longitude)

    elapsed_ms = now - departed_at
    ready_at = now + elapsed_ms
    return_home = conn.execute(
        "SELECT scheduled_at_ms, payload FROM _events WHERE kind = ? "
        "AND status = 'pending' AND entity_id = 1",
        (EventKind.TECHNICIAN_RETURN_HOME.value,),
    ).fetchone()
    assert return_home is not None
    assert return_home["scheduled_at_ms"] == ready_at
    payload = json.loads(return_home["payload"])
    assert payload["appointment_id"] == first_id
    assert payload["block_index"] == execution.block_index("B09_11")

    new_dispatch = _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, second_id)
    assert new_dispatch is not None
    assert json.loads(new_dispatch["payload"])["technician_id"] == 3

    status = controller.run()
    assert str(status) == "completed"
    assert _appointment_status(conn, second_id) == "completed"
    assert _technician_location_row(conn, 1)["location_state"] == "home"
    assert _day_state_row(conn, WEEKDAY)["unresolved_execution_count"] == 0


def test_clock_past_cutoff_arrival_appends_arrived_but_never_schedules_work_started(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    appointment_id = booked["appointment_id"]
    cutoff = clock.phoenix_instant(2026, 10, 1, 11, 0)
    late_clock = clock.phoenix_instant(2026, 10, 1, 11, 30)

    _step_until(
        controller,
        lambda: _technician_location_row(conn, 1)["location_state"] == "traveling",
    )
    trip = _technician_location_row(conn, 1)
    departed_at = int(trip["trip_departed_at"])
    arrival_ms = departed_at + int(trip["trip_travel_minutes"]) * 60000
    assert arrival_ms < cutoff
    _set_clock(conn, late_clock, WEEKDAY)

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT 1 FROM _appointment_events WHERE appointment_id = ? "
            "AND event_type = 'arrived'",
            (appointment_id,),
        ).fetchone() is not None,
    )
    arrived = conn.execute(
        "SELECT occurred_at FROM _appointment_events WHERE appointment_id = ? "
        "AND event_type = 'arrived'",
        (appointment_id,),
    ).fetchone()
    assert arrived["occurred_at"] == late_clock
    assert _technician_location_row(conn, 1)["location_state"] == "on_site"
    assert conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.WORK_STARTED.value, appointment_id),
    ).fetchone()["c"] == 0
    assert _guard_event(conn, appointment_id)["status"] == "pending"

    _step_until(controller, lambda: _appointment_status(conn, appointment_id) == "no_show")
    events = _appointment_event_rows(conn, appointment_id)
    assert [(e["event_type"], e["occurred_at"]) for e in events] == [
        ("dispatched", departed_at), ("no_show", cutoff), ("arrived", late_clock),
    ]

    return_home = conn.execute(
        "SELECT scheduled_at_ms, payload FROM _events WHERE kind = ? "
        "AND status = 'pending' AND entity_id = 1",
        (EventKind.TECHNICIAN_RETURN_HOME.value,),
    ).fetchone()
    assert return_home is not None
    assert return_home["scheduled_at_ms"] == late_clock
    payload = json.loads(return_home["payload"])
    assert payload["appointment_id"] == appointment_id
    assert payload["block_index"] == execution.block_index("B09_11")

    status = controller.run()
    assert str(status) == "completed"
    assert [e["event_type"] for e in _appointment_event_rows(conn, appointment_id)] == [
        "dispatched", "no_show", "arrived",
    ]
    assert conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.WORK_STARTED.value, appointment_id),
    ).fetchone()["c"] == 0
    assert _technician_location_row(conn, 1)["location_state"] == "home"
    day = _day_state_row(conn, WEEKDAY)
    assert day["unresolved_execution_count"] == 0
    assert day["closing_at"] == clock.phoenix_instant(2026, 10, 1, 19, 0)


_JOB_TERMINAL_LEGAL_PAIRS = frozenset({
    ("completed", "completed_paid"),
    ("completed", "completed_warranty"),
    ("failed", "failed_unsupported"),
    ("failed", "failed_unqualified"),
    ("no_show", "no_show_company"),
    ("cancelled", "cancelled_by_customer"),
})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "no_show", "cancelled"})


def _job_row(conn, job_id):
    return conn.execute(
        "SELECT status, outcome, completed_at, modified_at FROM _jobs WHERE id = ?", (job_id,)
    ).fetchone()


def test_unsupported_category_leg_burns_visit_minutes_then_fails_and_cascades(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    first = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=5, requested_category_id=UNSUPPORTED_CATEGORY_ID,
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    first_id = first["appointment_id"]
    second_id = second["appointment_id"]

    status = controller.run()
    assert str(status) == "completed"

    job = _job_row(conn, first["job_id"])
    assert job["status"] == "failed"
    assert job["outcome"] == "failed_unsupported"
    assert job["completed_at"] is None

    appt = conn.execute(
        "SELECT status, actual_start FROM _appointments WHERE id = ?", (first_id,)
    ).fetchone()
    assert appt["status"] == "failed"
    assert appt["actual_start"] is None

    events = _appointment_event_rows(conn, first_id)
    assert [e["event_type"] for e in events] == ["dispatched", "arrived", "work_failed"]
    arrived_at = events[1]["occurred_at"]
    failed_at = events[2]["occurred_at"]
    assert failed_at - arrived_at == cfg.get_int("execution.failed_visit_minutes") * 60000
    assert conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.WORK_STARTED.value, first_id),
    ).fetchone()["c"] == 0

    second_events = _appointment_event_rows(conn, second_id)
    second_dispatched = next(e for e in second_events if e["event_type"] == "dispatched")
    assert second_dispatched["occurred_at"] == failed_at

    movement = conn.execute(
        "SELECT COUNT(*) c FROM _technician_movements WHERE technician_id = 1"
    ).fetchone()
    assert movement["c"] == 1

    day = _day_state_row(conn, WEEKDAY)
    assert day["unresolved_execution_count"] == 0
    assert day["last_execution_completed_at"] == _latest_terminal_execution_at(conn, WEEKDAY)
    assert day["last_execution_completed_at"] > failed_at


def test_failed_visit_minutes_config_key_governs_the_work_failed_gap(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"execution.failed_visit_minutes": 45},
    )
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=5, requested_category_id=UNSUPPORTED_CATEGORY_ID,
    )
    appointment_id = booked["appointment_id"]

    status = controller.run()
    assert str(status) == "completed"

    events = _appointment_event_rows(conn, appointment_id)
    assert [e["event_type"] for e in events] == ["dispatched", "arrived", "work_failed"]
    assert events[2]["occurred_at"] - events[1]["occurred_at"] == 45 * 60000


def test_unqualified_technician_leg_fails_the_visit_using_the_true_categorys_skill(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=1, requested_category_id=PLUMBING_CATEGORY_ID,
    )
    appointment_id = booked["appointment_id"]

    status = controller.run()
    assert str(status) == "completed"

    job = _job_row(conn, booked["job_id"])
    assert job["status"] == "failed"
    assert job["outcome"] == "failed_unqualified"
    assert job["completed_at"] is None

    appt = conn.execute(
        "SELECT status, actual_start FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    assert appt["status"] == "failed"
    assert appt["actual_start"] is None

    events = _appointment_event_rows(conn, appointment_id)
    assert [e["event_type"] for e in events] == ["dispatched", "arrived", "work_failed"]
    assert events[2]["occurred_at"] - events[1]["occurred_at"] == (
        cfg.get_int("execution.failed_visit_minutes") * 60000
    )
    assert conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.WORK_STARTED.value, appointment_id),
    ).fetchone()["c"] == 0


def test_qualified_despite_misbooking_leg_completes_using_the_true_categorys_skill(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=3, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=1, requested_category_id=PLUMBING_CATEGORY_ID,
    )
    appointment_id = booked["appointment_id"]

    status = controller.run()
    assert str(status) == "completed"

    job = _job_row(conn, booked["job_id"])
    assert job["status"] == "completed"
    assert job["outcome"] == "completed_paid"
    assert job["completed_at"] is not None

    appt = conn.execute(
        "SELECT status, actual_start, actual_end FROM _appointments WHERE id = ?",
        (appointment_id,),
    ).fetchone()
    assert appt["status"] == "completed"
    assert appt["actual_start"] is not None
    assert appt["actual_end"] is not None

    events = _appointment_event_rows(conn, appointment_id)
    assert [e["event_type"] for e in events] == [
        "dispatched", "arrived", "work_started", "work_completed",
    ]


def test_cancel_during_a_failed_visit_cancels_the_pending_work_failed_and_replans(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=5, requested_category_id=UNSUPPORTED_CATEGORY_ID,
    )
    appointment_id = booked["appointment_id"]

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT 1 FROM _appointment_events WHERE appointment_id = ? AND event_type = 'arrived'",
            (appointment_id,),
        ).fetchone() is not None,
    )
    work_failed_pending = conn.execute(
        "SELECT event_id FROM _events WHERE kind = ? AND entity_id = ? AND status = 'pending'",
        (EventKind.WORK_FAILED.value, appointment_id),
    ).fetchone()
    assert work_failed_pending is not None

    now = clock.now(conn)
    result = _cancel_appointment(controller, cfg, appointment_id=appointment_id, effective_at=now)
    assert result["job_id"] == booked["job_id"]

    cancelled_event = conn.execute(
        "SELECT status FROM _events WHERE event_id = ?", (work_failed_pending["event_id"],)
    ).fetchone()
    assert cancelled_event["status"] == "cancelled"

    status = controller.run()
    assert str(status) == "completed"

    appt = conn.execute(
        "SELECT status FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    assert appt["status"] == "cancelled"

    job = _job_row(conn, booked["job_id"])
    assert job["status"] == "cancelled"
    assert job["outcome"] == "cancelled_by_customer"

    events = _appointment_event_rows(conn, appointment_id)
    assert [e["event_type"] for e in events] == ["dispatched", "arrived", "cancelled"]

    assert _technician_location_row(conn, 1)["location_state"] == "home"
    assert _day_state_row(conn, WEEKDAY)["unresolved_execution_count"] == 0


def test_reassignment_after_a_forked_arrival_cancels_the_stale_work_failed(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path,
        **{
            "travel.speed_by_time_band": _SLOW_MIDDAY_SPEED_OVERRIDE,
            "travel.noise.max_multiplier": 0.75,
        },
    )
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B13_15",
        job_type_id=5, requested_category_id=UNSUPPORTED_CATEGORY_ID,
    )
    appointment_id = booked["appointment_id"]

    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT 1 FROM _appointment_events WHERE appointment_id = ? AND event_type = 'arrived'",
            (appointment_id,),
        ).fetchone() is not None,
    )
    stale_work_failed = conn.execute(
        "SELECT event_id, payload FROM _events WHERE kind = ? AND entity_id = ? "
        "AND status = 'pending'",
        (EventKind.WORK_FAILED.value, appointment_id),
    ).fetchone()
    assert stale_work_failed is not None
    assert json.loads(stale_work_failed["payload"])["technician_id"] == 1

    request_id = privileged_request_id("assign", next(_request_seq))
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.assign(
            ctx, cfg, appointment_id=appointment_id, technician_id=3, artifact=ARTIFACT,
        ),
    )
    body = json.loads(response)
    assert body["ok"] is True, body

    cancelled = conn.execute(
        "SELECT status FROM _events WHERE event_id = ?", (stale_work_failed["event_id"],)
    ).fetchone()
    assert cancelled["status"] == "cancelled"

    new_dispatch = conn.execute(
        "SELECT payload FROM _events WHERE kind = ? AND entity_id = ? AND status = 'pending'",
        (EventKind.APPOINTMENT_DISPATCH.value, appointment_id),
    ).fetchone()
    assert new_dispatch is not None
    assert json.loads(new_dispatch["payload"])["technician_id"] == 3

    status = controller.run()
    assert str(status) == "completed"

    job = _job_row(conn, booked["job_id"])
    assert job["status"] == "failed"
    assert job["outcome"] == "failed_unsupported"

    events = _appointment_event_rows(conn, appointment_id)
    work_failed_events = [e for e in events if e["event_type"] == "work_failed"]
    assert len(work_failed_events) == 1
    assert work_failed_events[0]["technician_id"] == 3

    assert _day_state_row(conn, WEEKDAY)["unresolved_execution_count"] == 0


def test_company_no_show_cutoff_defers_to_a_pending_work_failed(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=5, requested_category_id=UNSUPPORTED_CATEGORY_ID,
    )
    appointment_id = booked["appointment_id"]
    cutoff = clock.phoenix_instant(2026, 10, 1, 11, 0)
    arrival_ms = cutoff - 15 * 60000

    _step_until(
        controller,
        lambda: _technician_location_row(conn, 1)["location_state"] == "traveling",
    )
    _retime_pending_arrival(
        conn, appointment_id=appointment_id, technician_id=1, arrival_ms=arrival_ms,
    )

    status = controller.run()
    assert str(status) == "completed"

    job = _job_row(conn, booked["job_id"])
    assert job["status"] == "failed"
    assert job["outcome"] == "failed_unsupported"

    appt = conn.execute(
        "SELECT status FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    assert appt["status"] == "failed"

    events = _appointment_event_rows(conn, appointment_id)
    assert [e["event_type"] for e in events] == ["dispatched", "arrived", "work_failed"]
    assert events[1]["occurred_at"] == arrival_ms
    assert events[2]["occurred_at"] == arrival_ms + (
        cfg.get_int("execution.failed_visit_minutes") * 60000
    )

    guard = conn.execute(
        "SELECT status FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.COMPANY_NO_SHOW.value, appointment_id),
    ).fetchone()
    assert guard["status"] == "applied"

    assert _day_state_row(conn, WEEKDAY)["unresolved_execution_count"] == 0


def test_classify_job_gatekeeper_rejects_double_classification(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    now = clock.now(conn)
    execution.classify_job(
        conn, job_id=booked["job_id"], status="completed", outcome="completed_paid",
        now_ms=now, completed_at_ms=now,
    )
    conn.commit()

    with pytest.raises(EngineError) as ei:
        execution.classify_job(
            conn, job_id=booked["job_id"], status="completed", outcome="completed_paid",
            now_ms=now, completed_at_ms=now,
        )
    assert ei.value.code is ErrorCode.INVARIANT_VIOLATED


def test_classify_job_gatekeeper_rejects_an_illegal_status_outcome_pair(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    now = clock.now(conn)

    with pytest.raises(EngineError) as ei:
        execution.classify_job(
            conn, job_id=booked["job_id"], status="completed", outcome="no_show_company",
            now_ms=now,
        )
    assert ei.value.code is ErrorCode.INVARIANT_VIOLATED

    row = _job_row(conn, booked["job_id"])
    assert row["status"] == "scheduled"
    assert row["outcome"] is None


def test_classify_job_sets_completed_at_only_for_a_completed_outcome(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    completed = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    failed = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    now = clock.now(conn)
    execution.classify_job(
        conn, job_id=completed["job_id"], status="completed", outcome="completed_paid",
        now_ms=now, completed_at_ms=now,
    )
    execution.classify_job(
        conn, job_id=failed["job_id"], status="failed", outcome="failed_unsupported", now_ms=now,
    )
    conn.commit()

    assert _job_row(conn, completed["job_id"])["completed_at"] == now
    assert _job_row(conn, failed["job_id"])["completed_at"] is None


def test_classify_job_gatekeeper_requires_completed_at_ms_for_a_completed_outcome(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    now = clock.now(conn)

    with pytest.raises(EngineError) as ei:
        execution.classify_job(
            conn, job_id=booked["job_id"], status="completed", outcome="completed_paid",
            now_ms=now,
        )
    assert ei.value.code is ErrorCode.INVARIANT_VIOLATED

    row = _job_row(conn, booked["job_id"])
    assert row["status"] == "scheduled"
    assert row["outcome"] is None


def test_classify_job_completed_at_tracks_completed_at_ms_while_modified_at_tracks_now_ms(
    tmp_path,
):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    completed_at_ms = clock.now(conn)
    now_ms = completed_at_ms + 5 * 60000

    execution.classify_job(
        conn, job_id=booked["job_id"], status="completed", outcome="completed_paid",
        now_ms=now_ms, completed_at_ms=completed_at_ms,
    )
    conn.commit()

    row = _job_row(conn, booked["job_id"])
    assert row["completed_at"] == completed_at_ms
    assert row["modified_at"] == now_ms
    assert row["completed_at"] != row["modified_at"]


def test_coherence_sweep_every_terminal_job_has_exactly_one_matching_outcome(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path, end_date=SECOND_WEEKDAY)

    completed = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    failed = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
        job_type_id=5, requested_category_id=UNSUPPORTED_CATEGORY_ID,
    )
    cancelled = _book_execution(
        controller, cfg, technician_id=3, service_date=WEEKDAY, first_block_code="B13_15",
    )
    _cancel_appointment(
        controller, cfg, appointment_id=cancelled["appointment_id"], effective_at=clock.now(conn),
    )
    no_show = _book_execution(
        controller, cfg, technician_id=4, service_date=WEEKDAY, first_block_code="B15_17",
    )
    future = _book_execution(
        controller, cfg, technician_id=1, service_date=SECOND_WEEKDAY, first_block_code="B09_11",
    )

    _step_until(
        controller,
        lambda: _pending_event(
            conn, EventKind.APPOINTMENT_DISPATCH, no_show["appointment_id"]
        ) is not None,
    )
    dispatch = _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, no_show["appointment_id"])
    guard = _guard_event(conn, no_show["appointment_id"])
    _move_pending_event(
        conn, int(dispatch["event_id"]), int(guard["scheduled_at_ms"]) + 30 * 60000,
    )

    settled_ids = {
        completed["appointment_id"], failed["appointment_id"], cancelled["appointment_id"],
        no_show["appointment_id"],
    }
    _step_until(
        controller,
        lambda: all(_appointment_status(conn, aid) in _TERMINAL_STATUSES for aid in settled_ids),
    )

    rows = conn.execute(
        "SELECT j.status AS job_status, j.outcome AS outcome, a.status AS appt_status "
        "FROM _jobs j JOIN _appointments a ON a.job_id = j.id"
    ).fetchall()
    assert len(rows) == 5
    for row in rows:
        if row["appt_status"] in _TERMINAL_STATUSES:
            assert (row["job_status"], row["outcome"]) in _JOB_TERMINAL_LEGAL_PAIRS
        else:
            assert row["outcome"] is None
            assert row["job_status"] not in _TERMINAL_STATUSES

    no_show_job = _job_row(conn, no_show["job_id"])
    assert no_show_job["status"] == "no_show"
    assert no_show_job["outcome"] == "no_show_company"
    assert no_show_job["completed_at"] is None

    assert _appointment_status(conn, future["appointment_id"]) == "scheduled"
    future_job = _job_row(conn, future["job_id"])
    assert future_job["status"] == "scheduled"
    assert future_job["outcome"] is None


def _twin_failure_bookings(controller, cfg):
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=5, requested_category_id=UNSUPPORTED_CATEGORY_ID,
    )
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )


def test_twin_determinism_through_the_failure_path_identical_jobs_and_dump(tmp_path):
    global _id_seq, _request_seq
    saved_id_seq, saved_request_seq = _id_seq, _request_seq
    try:
        (tmp_path / "a").mkdir()
        _id_seq, _request_seq = count(9500), count(1)
        conn_a, cfg_a, controller_a, _ = _build_run(tmp_path / "a")
        _twin_failure_bookings(controller_a, cfg_a)
        status_a = controller_a.run()
        assert str(status_a) == "completed"
        run_id_a = conn_a.execute("SELECT run_id FROM _run").fetchone()["run_id"]
        hash_a = dump_hash(conn_a, run_id_a)
        jobs_a = [
            tuple(row)
            for row in conn_a.execute(
                "SELECT id, status, outcome, completed_at FROM _jobs ORDER BY id"
            ).fetchall()
        ]

        (tmp_path / "b").mkdir()
        _id_seq, _request_seq = count(9500), count(1)
        conn_b, cfg_b, controller_b, _ = _build_run(tmp_path / "b")
        _twin_failure_bookings(controller_b, cfg_b)
        status_b = controller_b.run()
        assert str(status_b) == "completed"
        run_id_b = conn_b.execute("SELECT run_id FROM _run").fetchone()["run_id"]
        hash_b = dump_hash(conn_b, run_id_b)
        jobs_b = [
            tuple(row)
            for row in conn_b.execute(
                "SELECT id, status, outcome, completed_at FROM _jobs ORDER BY id"
            ).fetchall()
        ]

        assert jobs_a == jobs_b
        assert hash_a == hash_b
        assert any(row[2] == "failed_unsupported" for row in jobs_a)
    finally:
        _id_seq, _request_seq = saved_id_seq, saved_request_seq


_SETTLEMENT_ENTRY_TYPES = frozenset({
    "payroll_base", "payroll_off_hours", "payroll_off_hours_bonus", "operating_overhead",
})


def _invoice_row(conn, job_id):
    return conn.execute("SELECT * FROM _invoices WHERE job_id = ?", (job_id,)).fetchone()


def _ledger_rows(conn, entry_type):
    return conn.execute(
        "SELECT * FROM _ledger_entries WHERE entry_type = ? ORDER BY occurred_at, id",
        (entry_type,),
    ).fetchall()


def _job_finance_expectation(conn, cfg, job_id):
    row = conn.execute(
        "SELECT c.segment AS segment, jt.code AS code, j.completed_at AS completed_at "
        "FROM _jobs j JOIN _customers c ON c.id = j.customer_id "
        "JOIN _call_hidden ch ON ch.call_id = j.call_id "
        "JOIN _service_catalog sc ON sc.id = ch.requested_category_id "
        "JOIN _job_types jt ON jt.id = sc.supported_job_type_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    completion_date = clock.service_date_for_ms(int(row["completed_at"]))
    scope = origin_keys.of_job(conn, job_id)
    shadow = _shadow_rng()
    amounts = cfg.resolved["invoices.amount"][row["code"]][row["segment"]]
    subtotal = shadow.triangular_cents(
        KeyedStreamName.JOB_FINANCE.value, "job_origin", 1, scope, "invoice_amount",
        amounts["min_cents"], amounts["mode_cents"], amounts["max_cents"], 0,
    )
    percent_range = cfg.resolved["materials.percent_range"][row["code"]]
    basis_points = shadow.bounded_int(
        KeyedStreamName.JOB_FINANCE.value, "job_origin", 1, scope, "material_percentage",
        percent_range["min_basis_points"], percent_range["max_basis_points"], 0,
    )
    return {
        "completion_date": completion_date,
        "job_type_code": row["code"],
        "segment": row["segment"],
        "subtotal_cents": subtotal,
        "basis_points": basis_points,
        "material_cost_cents": basis_point_amount(subtotal, basis_points),
    }


def test_paid_completion_writes_invoice_item_payment_and_two_ledger_entries(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    job_id = booked["job_id"]

    status = controller.run()
    assert str(status) == "completed"

    expected = _job_finance_expectation(conn, cfg, job_id)
    completed_at = int(_job_row(conn, job_id)["completed_at"])

    invoice = _invoice_row(conn, job_id)
    assert invoice["invoice_number"] == f"INV-{invoice['id']:06d}"
    assert invoice["status"] == "paid"
    assert invoice["no_charge"] == 0
    assert invoice["issued_at"] == completed_at
    assert invoice["invoice_date"] == expected["completion_date"]
    assert invoice["subtotal_cents"] == expected["subtotal_cents"]
    assert invoice["total_cents"] == expected["subtotal_cents"]
    assert invoice["material_percent_basis_points"] == expected["basis_points"]
    assert invoice["material_cost_cents"] == expected["material_cost_cents"]
    percent_range = cfg.resolved["materials.percent_range"][expected["job_type_code"]]
    assert (
        percent_range["min_basis_points"]
        <= invoice["material_percent_basis_points"]
        <= percent_range["max_basis_points"]
    )
    amounts = cfg.resolved["invoices.amount"][expected["job_type_code"]][expected["segment"]]
    assert amounts["min_cents"] <= invoice["subtotal_cents"] <= amounts["max_cents"]
    assert invoice["business_unit_id"] == conn.execute(
        "SELECT business_unit_id FROM _job_types WHERE code = ?", (expected["job_type_code"],)
    ).fetchone()["business_unit_id"]

    items = conn.execute(
        "SELECT * FROM _invoice_items WHERE invoice_id = ? ORDER BY id", (invoice["id"],)
    ).fetchall()
    assert len(items) == 1
    assert items[0]["item_type"] == "service"
    assert items[0]["quantity"] == 1
    assert items[0]["amount_cents"] == invoice["subtotal_cents"]
    assert items[0]["description"] == conn.execute(
        "SELECT name FROM _job_types WHERE code = ?", (expected["job_type_code"],)
    ).fetchone()["name"]

    payments = conn.execute(
        "SELECT * FROM _payments WHERE invoice_id = ? ORDER BY id", (invoice["id"],)
    ).fetchall()
    assert len(payments) == 1
    assert payments[0]["payment_number"] == f"PAY-{payments[0]['id']:06d}"
    assert payments[0]["paid_at"] == invoice["issued_at"]
    assert payments[0]["amount_cents"] == invoice["total_cents"]
    assert payments[0]["payment_type"] == "immediate"
    assert payments[0]["status"] == "posted"

    revenue = _ledger_rows(conn, "revenue")
    materials = _ledger_rows(conn, "material_cost")
    assert len(revenue) == 1
    assert len(materials) == 1
    assert revenue[0]["amount_cents"] == invoice["total_cents"]
    assert revenue[0]["occurred_at"] == completed_at
    assert revenue[0]["service_date"] == expected["completion_date"]
    assert revenue[0]["job_id"] == job_id
    assert revenue[0]["invoice_id"] == invoice["id"]
    assert revenue[0]["payment_id"] == payments[0]["id"]
    assert materials[0]["amount_cents"] == -invoice["material_cost_cents"]
    assert materials[0]["occurred_at"] == completed_at
    assert materials[0]["job_id"] == job_id
    assert materials[0]["invoice_id"] == invoice["id"]

    finance_scope = origin_keys.of_job(conn, job_id)
    audit = conn.execute(
        "SELECT event_kind, scope, draw_index, simulated_ts_ms FROM _random_audit "
        "WHERE stream_name = ? AND entity_type = 'job_origin' AND entity_id = 1 AND scope = ? "
        "ORDER BY seq",
        (KeyedStreamName.JOB_FINANCE.value, finance_scope),
    ).fetchall()
    assert [tuple(row) for row in audit] == [
        ("invoice_amount", finance_scope, 0, completed_at),
        ("material_percentage", finance_scope, 0, completed_at),
    ]


def test_paid_completion_rows_are_readable_through_the_public_views(tmp_path):
    conn, cfg, controller, db_path = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    job_id = booked["job_id"]

    status = controller.run()
    assert str(status) == "completed"

    invoice = _invoice_row(conn, job_id)
    agent = open_agent_session(db_path)
    try:
        row = execute_gated(
            agent,
            "SELECT i.invoice_number AS invoice_number, i.subtotal_cents AS subtotal_cents, "
            "i.total_cents AS total_cents, i.material_cost_cents AS material_cost_cents, "
            "i.material_percent_basis_points AS basis_points, "
            "ii.description AS description, ii.amount_cents AS item_amount_cents, "
            "p.payment_number AS payment_number, p.amount_cents AS paid_cents, "
            "p.payment_type AS payment_type, p.status AS payment_status "
            "FROM invoices i JOIN invoice_items ii ON ii.invoice_id = i.id "
            f"JOIN payments p ON p.invoice_id = i.id WHERE i.job_id = {job_id}",
        ).fetchone()
        assert row["invoice_number"] == invoice["invoice_number"]
        assert row["subtotal_cents"] == invoice["subtotal_cents"]
        assert row["total_cents"] == invoice["total_cents"]
        assert row["material_cost_cents"] == invoice["material_cost_cents"]
        assert row["basis_points"] == invoice["material_percent_basis_points"]
        assert row["item_amount_cents"] == invoice["subtotal_cents"]
        assert row["paid_cents"] == invoice["total_cents"]
        assert row["payment_type"] == "immediate"
        assert row["payment_status"] == "posted"
        assert row["description"] == conn.execute(
            "SELECT name FROM _job_types WHERE id = 1"
        ).fetchone()["name"]
        with pytest.raises(EngineError):
            execute_gated(agent, "SELECT origin FROM invoices")
        with pytest.raises(EngineError):
            execute_gated(agent, "SELECT origin FROM payments")
    finally:
        agent.close()


def _disjoint_plumbing_invoice_amounts():
    amounts = json.loads(json.dumps(load_run_config().resolved["invoices.amount"]))
    amounts["plumbing"]["commercial"] = {
        "min_cents": 200000, "mode_cents": 210000, "max_cents": 220000,
    }
    return amounts


def test_paid_completion_prices_the_true_job_type_not_the_booked_one(tmp_path):
    amounts = _disjoint_plumbing_invoice_amounts()
    conn, cfg, controller, _ = _build_run(tmp_path, **{"invoices.amount": amounts})
    booked = _book_execution(
        controller, cfg, technician_id=3, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=1, requested_category_id=PLUMBING_CATEGORY_ID,
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    job_id = booked["job_id"]

    status = controller.run()
    assert str(status) == "completed"

    job = conn.execute(
        "SELECT outcome, job_type_id, business_unit_id FROM _jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert job["outcome"] == "completed_paid"
    assert int(job["job_type_id"]) == 1
    assert int(job["business_unit_id"]) == int(
        conn.execute("SELECT business_unit_id FROM _job_types WHERE id = 1").fetchone()[0]
    )

    plumbing = conn.execute(
        "SELECT id, name, business_unit_id FROM _job_types WHERE code = 'plumbing'"
    ).fetchone()
    assert int(plumbing["id"]) != 1
    expected = _job_finance_expectation(conn, cfg, job_id)
    assert expected["job_type_code"] == "plumbing"

    invoice = _invoice_row(conn, job_id)
    plumbing_band = amounts["plumbing"]["commercial"]
    hvac_band = amounts["hvac_service"]["commercial"]
    assert plumbing_band["min_cents"] > hvac_band["max_cents"]
    assert plumbing_band["min_cents"] <= invoice["subtotal_cents"] <= plumbing_band["max_cents"]
    assert not (
        hvac_band["min_cents"] <= invoice["subtotal_cents"] <= hvac_band["max_cents"]
    )
    assert invoice["subtotal_cents"] == expected["subtotal_cents"]
    assert int(invoice["business_unit_id"]) == int(plumbing["business_unit_id"])
    assert int(invoice["business_unit_id"]) != int(job["business_unit_id"])

    plumbing_range = cfg.resolved["materials.percent_range"]["plumbing"]
    assert invoice["material_percent_basis_points"] == expected["basis_points"]
    assert (
        plumbing_range["min_basis_points"]
        <= invoice["material_percent_basis_points"]
        <= plumbing_range["max_basis_points"]
    )
    assert invoice["material_cost_cents"] == expected["material_cost_cents"]

    item = conn.execute(
        "SELECT description FROM _invoice_items WHERE invoice_id = ?", (invoice["id"],)
    ).fetchone()
    assert item["description"] == plumbing["name"]

    revenue = _ledger_rows(conn, "revenue")
    assert [row["amount_cents"] for row in revenue] == [invoice["total_cents"]]


def test_paid_completion_ledger_balance_after_replays_in_occurred_at_and_id_order(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    _book_execution(
        controller, cfg, technician_id=3, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )

    status = controller.run()
    assert str(status) == "completed"

    rows = conn.execute(
        "SELECT amount_cents, balance_after_cents FROM _ledger_entries "
        "ORDER BY occurred_at, id"
    ).fetchall()
    running = 0
    for row in rows:
        running += int(row["amount_cents"])
        assert int(row["balance_after_cents"]) == running
    assert controller.services.ledger.cash_balance() == running


def test_booking_moves_no_cash_and_settlement_posts_only_payroll_and_overhead(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    starting_cash = cfg.get_int("finance.starting_cash")
    opening_entries = controller.services.ledger.entry_count()

    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    _book_execution(
        controller, cfg, technician_id=3, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    assert controller.services.ledger.entry_count() == opening_entries
    assert controller.services.ledger.cash_balance() == starting_cash
    assert conn.execute(
        "SELECT COUNT(*) c FROM _random_audit WHERE stream_name = ?",
        (KeyedStreamName.JOB_FINANCE.value,),
    ).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM _invoices").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM _payments").fetchone()["c"] == 0

    status = controller.run()
    assert str(status) == "completed"
    assert conn.execute(
        "SELECT COUNT(*) c FROM _random_audit WHERE stream_name = ?",
        (KeyedStreamName.JOB_FINANCE.value,),
    ).fetchone()["c"] == 4

    closing_at = int(
        conn.execute(
            "SELECT closing_at FROM _day_state WHERE service_date = ?", (WEEKDAY,)
        ).fetchone()["closing_at"]
    )
    settlement_types = {
        row["entry_type"]
        for row in conn.execute(
            "SELECT DISTINCT entry_type FROM _ledger_entries WHERE occurred_at = ?", (closing_at,)
        ).fetchall()
    }
    assert settlement_types
    assert settlement_types <= _SETTLEMENT_ENTRY_TYPES
    for entry_type in ("revenue", "material_cost"):
        assert all(int(row["occurred_at"]) < closing_at for row in _ledger_rows(conn, entry_type))


def test_non_paid_outcomes_create_no_invoice_payment_or_revenue(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    unsupported = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=5, requested_category_id=UNSUPPORTED_CATEGORY_ID,
    )
    unqualified = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        requested_category_id=PLUMBING_CATEGORY_ID,
    )
    cancelled = _book_execution(
        controller, cfg, technician_id=3, service_date=WEEKDAY, first_block_code="B15_17",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    no_show = _book_execution(
        controller, cfg, technician_id=4, service_date=WEEKDAY, first_block_code="B13_15",
    )
    _cancel_appointment(
        controller, cfg, appointment_id=cancelled["appointment_id"],
        effective_at=clock.phoenix_instant(2026, 10, 1, 7, 0),
    )
    _step_until(
        controller,
        lambda: _pending_event(
            conn, EventKind.APPOINTMENT_DISPATCH, no_show["appointment_id"]
        ) is not None,
    )
    dispatch = _pending_event(conn, EventKind.APPOINTMENT_DISPATCH, no_show["appointment_id"])
    _cancel_event(controller, int(dispatch["event_id"]))

    status = controller.run()
    assert str(status) == "completed"

    outcomes = {
        int(row["id"]): row["outcome"]
        for row in conn.execute("SELECT id, outcome FROM _jobs").fetchall()
    }
    assert outcomes[unsupported["job_id"]] == "failed_unsupported"
    assert outcomes[unqualified["job_id"]] == "failed_unqualified"
    assert outcomes[cancelled["job_id"]] == "cancelled_by_customer"
    assert outcomes[no_show["job_id"]] == "no_show_company"
    assert "completed_paid" not in outcomes.values()

    assert conn.execute("SELECT COUNT(*) c FROM _invoices").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM _invoice_items").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM _payments").fetchone()["c"] == 0
    assert _ledger_rows(conn, "revenue") == []
    assert _ledger_rows(conn, "material_cost") == []


def _finance_snapshot(directory, *, priority="normal", with_unrelated_activity=False):
    global _id_seq, _request_seq
    _id_seq, _request_seq = count(9500), count(1)
    conn, cfg, controller, db_path = _build_run(directory)
    if with_unrelated_activity:
        agent = open_agent_session(db_path)
        try:
            execute_gated(agent, "SELECT COUNT(*) AS c FROM jobs").fetchone()
        finally:
            agent.close()
        failed = privileged(
            controller.services, privileged_request_id("assign", next(_request_seq)),
            lambda ctx: appointments.assign(
                ctx, cfg, appointment_id=987654, technician_id=1, artifact=ARTIFACT,
            ),
        )
        assert json.loads(failed)["ok"] is False

    now = clock.now(conn)
    location_id = _customer_location_call(
        conn, now, WEEKDAY, address_catalog_id=DESTINATION_ADDRESS.address_id, config=cfg,
        requested_category_id=HVAC_SERVICE_CATEGORY_ID,
    )
    request_id = privileged_request_id("book", next(_request_seq))

    def book(ctx):
        return appointments.book(
            ctx, cfg, call_id=location_id, customer_id=location_id, location_id=location_id,
            job_type_id=1, requested_service_name="HVAC service", priority=priority, summary="x",
            technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        )

    response = privileged(controller.services, request_id, book)
    body = json.loads(response)
    assert body["ok"] is True, body
    if with_unrelated_activity:
        assert privileged(controller.services, request_id, book) == response
        other = _book_execution(
            controller, cfg, technician_id=3, service_date=WEEKDAY,
            first_block_code="B07_09", requested_category_id=PLUMBING_CATEGORY_ID,
            address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
        )

    status = controller.run()
    assert str(status) == "completed"
    job_id = body["result"]["job_id"]
    if with_unrelated_activity:
        assert _job_row(conn, other["job_id"])["outcome"] == "completed_paid"
        assert int(other["job_id"]) != int(job_id)
    invoice = _invoice_row(conn, job_id)
    finance_draws = int(
        conn.execute(
            "SELECT COUNT(*) c FROM _random_audit WHERE stream_name = ?",
            (KeyedStreamName.JOB_FINANCE.value,),
        ).fetchone()["c"]
    )
    return job_id, (
        int(invoice["subtotal_cents"]),
        int(invoice["total_cents"]),
        int(invoice["material_percent_basis_points"]),
        int(invoice["material_cost_cents"]),
    ), audit_count(conn), finance_draws


def test_unrelated_activity_never_shifts_the_job_finance_draws(tmp_path):
    global _id_seq, _request_seq
    saved_id_seq, saved_request_seq = _id_seq, _request_seq
    try:
        (tmp_path / "plain").mkdir()
        (tmp_path / "noisy").mkdir()
        plain_job_id, plain, plain_audit, plain_finance = _finance_snapshot(tmp_path / "plain")
        noisy_job_id, noisy, noisy_audit, noisy_finance = _finance_snapshot(
            tmp_path / "noisy", with_unrelated_activity=True
        )
        assert plain_job_id == noisy_job_id
        assert noisy_audit > plain_audit
        assert noisy_finance == plain_finance + 2
        assert plain == noisy
    finally:
        _id_seq, _request_seq = saved_id_seq, saved_request_seq


def test_urgent_and_normal_jobs_draw_the_same_invoice_amount(tmp_path):
    global _id_seq, _request_seq
    saved_id_seq, saved_request_seq = _id_seq, _request_seq
    try:
        (tmp_path / "normal").mkdir()
        (tmp_path / "urgent").mkdir()
        normal_job_id, normal, _, _ = _finance_snapshot(tmp_path / "normal", priority="normal")
        urgent_job_id, urgent, _, _ = _finance_snapshot(tmp_path / "urgent", priority="urgent")
        assert normal_job_id == urgent_job_id
        assert normal == urgent
    finally:
        _id_seq, _request_seq = saved_id_seq, saved_request_seq


def test_invoice_distribution_parameters_ignore_urgency(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    urgent = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        priority="urgent",
    )
    normal = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )

    status = controller.run()
    assert str(status) == "completed"

    urgent_expected = _job_finance_expectation(conn, cfg, urgent["job_id"])
    normal_expected = _job_finance_expectation(conn, cfg, normal["job_id"])
    priorities = {
        int(row["id"]): row["priority"]
        for row in conn.execute("SELECT id, priority FROM _jobs").fetchall()
    }
    assert priorities[urgent["job_id"]] == "urgent"
    assert priorities[normal["job_id"]] == "normal"
    assert (
        cfg.resolved["invoices.amount"][urgent_expected["job_type_code"]][
            urgent_expected["segment"]
        ]
        == cfg.resolved["invoices.amount"][normal_expected["job_type_code"]][
            normal_expected["segment"]
        ]
    )
    for job in (urgent, normal):
        invoice = _invoice_row(conn, job["job_id"])
        expected = _job_finance_expectation(conn, cfg, job["job_id"])
        assert invoice["subtotal_cents"] == expected["subtotal_cents"]


def test_revenue_and_material_entries_reconcile_with_the_paid_invoices(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    _book_execution(
        controller, cfg, technician_id=3, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )

    status = controller.run()
    assert str(status) == "completed"

    invoices = conn.execute(
        "SELECT id, job_id, total_cents, material_cost_cents FROM _invoices ORDER BY id"
    ).fetchall()
    assert len(invoices) == 2
    revenue = _ledger_rows(conn, "revenue")
    materials = _ledger_rows(conn, "material_cost")
    assert [(row["job_id"], row["amount_cents"]) for row in revenue] == [
        (row["job_id"], row["total_cents"]) for row in invoices
    ]
    assert [(row["job_id"], row["amount_cents"]) for row in materials] == [
        (row["job_id"], -row["material_cost_cents"]) for row in invoices
    ]
    for row in revenue:
        assert row["invoice_id"] is not None
        assert row["payment_id"] is not None
        assert row["technician_id"] is None
    for row in materials:
        assert row["invoice_id"] is not None
        assert row["payment_id"] is None
    realized_types = {
        row["entry_type"]
        for row in conn.execute("SELECT DISTINCT entry_type FROM _ledger_entries").fetchall()
    }
    assert realized_types <= {category.value for category in LedgerCategory}
    assert {"revenue", "material_cost"} <= realized_types


def test_the_smallest_accepted_price_still_posts_revenue_and_a_material_entry(tmp_path):
    amounts = json.loads(json.dumps(load_run_config().resolved["invoices.amount"]))
    for segment in ("residential", "apartment", "commercial"):
        amounts["hvac_service"][segment] = {
            "min_cents": 5, "mode_cents": 5, "max_cents": 5,
        }
    conn, cfg, controller, _ = _build_run(tmp_path, **{"invoices.amount": amounts})
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )

    status = controller.run()
    assert str(status) == "completed"

    invoice = _invoice_row(conn, booked["job_id"])
    assert invoice["subtotal_cents"] == 5
    assert invoice["total_cents"] == 5
    assert invoice["material_cost_cents"] == basis_point_amount(
        5, invoice["material_percent_basis_points"]
    )
    assert invoice["material_cost_cents"] >= 1
    assert [row["amount_cents"] for row in _ledger_rows(conn, "revenue")] == [5]
    assert [row["amount_cents"] for row in _ledger_rows(conn, "material_cost")] == [
        -invoice["material_cost_cents"]
    ]


def test_off_hours_settlement_pays_nothing_when_cancellation_precedes_the_block_start(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path, end_date=WEEKEND)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B09_11",
    )
    _cancel_appointment(
        controller, cfg, appointment_id=booked["appointment_id"],
        effective_at=clock.phoenix_instant(2026, 10, 3, 7, 30),
    )

    status = controller.run()
    assert str(status) == "completed"

    assert _appointment_status(conn, booked["appointment_id"]) == "cancelled"
    assert _ledger_off_hours_entries(conn, WEEKEND) == []


def test_off_hours_accrual_stays_with_the_technician_active_at_the_block_start(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path, end_date=WEEKEND)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B09_11",
        requested_category_id=PLUMBING_CATEGORY_ID,
    )
    _step_until(
        controller,
        lambda: conn.execute(
            "SELECT status FROM _events WHERE kind = ? AND entity_id = ?",
            (EventKind.OFF_HOURS_ACCRUAL_MARK.value, booked["appointment_id"]),
        ).fetchone()["status"] == "applied",
    )
    request_id = privileged_request_id("assign", next(_request_seq))
    response = privileged(
        controller.services, request_id,
        lambda ctx: appointments.assign(
            ctx, cfg, appointment_id=booked["appointment_id"], technician_id=3, artifact=ARTIFACT,
        ),
    )
    body = json.loads(response)
    assert body["ok"] is False
    assert body["error"]["code"] == ErrorCode.STATE_ILLEGAL.value

    wage1 = int(
        conn.execute("SELECT hourly_wage_cents FROM _technicians WHERE id = 1").fetchone()[0]
    )

    status = controller.run()
    assert str(status) == "completed"

    assert _job_row(conn, booked["job_id"])["outcome"] == "failed_unqualified"
    assert _ledger_off_hours_entries(conn, WEEKEND) == []
    assert (1, -12 * wage1) in _on_call_wage_entries(conn, WEEKEND)


def test_a_weekend_evening_job_earns_exactly_one_off_hours_share(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path, end_date=WEEKEND)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B17_19",
    )

    marks = conn.execute(
        "SELECT scheduled_at_ms FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.OFF_HOURS_ACCRUAL_MARK.value, booked["appointment_id"]),
    ).fetchall()
    assert len(marks) == 1
    assert int(marks[0]["scheduled_at_ms"]) == clock.phoenix_instant(2026, 10, 3, 17, 0)

    status = controller.run()
    assert str(status) == "completed"

    entries = _ledger_off_hours_entries(conn, WEEKEND)
    assert [(row["entry_type"], row["amount_cents"]) for row in entries] == [
        ("payroll_off_hours_bonus", -_off_hours_share_cents(conn, cfg, booked["job_id"])),
    ]


def test_the_off_hours_share_is_unchanged_by_an_overrun_which_is_paid_as_capped_overtime(
    tmp_path,
):
    conn, cfg, controller, _ = _build_run(
        tmp_path, end_date=WEEKEND, **{"appointments.min_actual_duration_minutes": 260}
    )
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B15_17",
    )
    wage = int(
        conn.execute("SELECT hourly_wage_cents FROM _technicians WHERE id = 1").fetchone()[0]
    )

    status = controller.run()
    assert str(status) == "completed"

    appointment = conn.execute(
        "SELECT scheduled_end, actual_end FROM _appointments WHERE id = ?",
        (booked["appointment_id"],),
    ).fetchone()
    assert int(appointment["actual_end"]) > int(appointment["scheduled_end"])
    entries = _ledger_off_hours_entries(conn, WEEKEND)
    assert [row["amount_cents"] for row in entries] == [
        -_off_hours_share_cents(conn, cfg, booked["job_id"])
    ]
    on_call_end = clock.phoenix_instant(2026, 10, 3, 19, 0)
    assert int(appointment["actual_end"]) > on_call_end
    overrun_minutes = (int(appointment["actual_end"]) - on_call_end) // 60000
    assert [row["amount_cents"] for row in _overtime_rows(conn, WEEKEND)] == [
        -_expected_paid_overtime_cents(conn, cfg, booked["job_id"], overrun_minutes, wage)
    ]


def test_off_hours_settlement_orders_entries_by_booking_origin_not_by_block_time(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path, end_date=WEEKEND)
    later_block = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B13_15",
    )
    earlier_block = _book_execution(
        controller, cfg, technician_id=3, service_date=WEEKEND, first_block_code="B09_11",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    assert later_block["appointment_id"] < earlier_block["appointment_id"]
    marks = conn.execute(
        "SELECT entity_id, scheduled_at_ms FROM _events WHERE kind = ? ORDER BY entity_id",
        (EventKind.OFF_HOURS_ACCRUAL_MARK.value,),
    ).fetchall()
    assert int(marks[0]["scheduled_at_ms"]) > int(marks[1]["scheduled_at_ms"])

    status = controller.run()
    assert str(status) == "completed"

    entries = conn.execute(
        "SELECT entry_type, job_id FROM _ledger_entries WHERE service_date = ? "
        "AND entry_type IN ('payroll_off_hours', 'payroll_off_hours_bonus') "
        "AND description NOT LIKE 'overtime wages%' AND description != 'on-call wages' "
        "ORDER BY id",
        (WEEKEND,),
    ).fetchall()
    assert [(row["entry_type"], row["job_id"]) for row in entries] == [
        ("payroll_off_hours_bonus", later_block["job_id"]),
        ("payroll_off_hours_bonus", earlier_block["job_id"]),
    ]


def test_a_failure_inside_the_completion_transaction_leaves_no_partial_money(tmp_path, monkeypatch):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    original_post = Ledger.post

    def refusing_post(self, entries):
        if any(entry.entry_type is LedgerCategory.REVENUE for entry in entries):
            raise EngineError(ErrorCode.STATE_ILLEGAL, "ledger post refused")
        return original_post(self, entries)

    with monkeypatch.context() as m:
        m.setattr(Ledger, "post", refusing_post)
        with pytest.raises(EngineError):
            controller.run()

    assert conn.execute("SELECT COUNT(*) c FROM _invoices").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM _invoice_items").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM _payments").fetchone()["c"] == 0
    assert _ledger_rows(conn, "revenue") == []
    assert _ledger_rows(conn, "material_cost") == []
    job = _job_row(conn, booked["job_id"])
    assert job["outcome"] is None
    assert _appointment_status(conn, booked["appointment_id"]) == "in_progress"


def test_a_second_invoice_or_payment_for_a_paid_completion_is_unreachable(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )

    status = controller.run()
    assert str(status) == "completed"

    invoice = _invoice_row(conn, booked["job_id"])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO _invoices (invoice_number, job_id, customer_id, location_id, "
            "business_unit_id, invoice_date, issued_at, status, no_charge, subtotal_cents, "
            "total_cents, material_cost_cents, material_percent_basis_points, created_at) "
            "VALUES ('INV-999999', ?, ?, ?, ?, ?, ?, 'paid', 0, 1, 1, 1, 1, ?)",
            (
                invoice["job_id"], invoice["customer_id"], invoice["location_id"],
                invoice["business_unit_id"], invoice["invoice_date"], invoice["issued_at"],
                invoice["issued_at"],
            ),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO _payments (payment_number, invoice_id, customer_id, paid_at, "
            "amount_cents, payment_type, status, created_at) "
            "VALUES ('PAY-999999', ?, ?, ?, 1, 'immediate', 'posted', ?)",
            (
                invoice["id"], invoice["customer_id"], invoice["issued_at"],
                invoice["issued_at"],
            ),
        )


def _overtime_rows(conn, service_date):
    return conn.execute(
        "SELECT amount_cents, technician_id, job_id, description FROM _ledger_entries "
        "WHERE service_date = ? AND entry_type = 'payroll_off_hours' "
        "AND description LIKE 'overtime wages%' ORDER BY id",
        (service_date,),
    ).fetchall()


def _bonus_rows_for_job(conn, job_id):
    return conn.execute(
        "SELECT amount_cents FROM _ledger_entries "
        "WHERE entry_type = 'payroll_off_hours_bonus' AND job_id = ?",
        (job_id,),
    ).fetchall()


def _wage(conn, technician_id):
    return int(
        conn.execute(
            "SELECT hourly_wage_cents FROM _technicians WHERE id = ?", (technician_id,)
        ).fetchone()[0]
    )


def _expected_overtime_cents(minutes, wage):
    return (minutes * wage + 30) // 60


def _expected_paid_overtime_cents(conn, cfg, job_id, minutes, wage):
    cap = basis_point_amount(
        int(_invoice_row(conn, job_id)["subtotal_cents"]),
        cfg.get_int("payroll.overtime_cap_basis_points"),
    )
    return min(_expected_overtime_cents(minutes, wage), cap)


def test_an_under_booked_installation_in_the_last_office_block_pays_overtime(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=INSTALL_TECHNICIAN_ID, service_date=WEEKDAY,
        first_block_code="B15_17", job_type_id=1,
        requested_category_id=HVAC_INSTALLATION_CATEGORY_ID,
    )

    assert str(controller.run()) == "completed"

    appt = _actual_window(conn, booked["appointment_id"])
    shift_end = clock.phoenix_instant(2026, 10, 1, 17, 0)
    minutes = (int(appt["actual_end"]) - shift_end) // 60000
    assert minutes >= 105
    rows = _overtime_rows(conn, WEEKDAY)
    assert [(r["technician_id"], r["job_id"], r["amount_cents"]) for r in rows] == [
        (
            INSTALL_TECHNICIAN_ID,
            booked["job_id"],
            -_expected_overtime_cents(minutes, _wage(conn, INSTALL_TECHNICIAN_ID)),
        )
    ]
    assert rows[0]["description"] == f"overtime wages ({minutes} min past paid hours)"
    assert _bonus_rows_for_job(conn, booked["job_id"]) == []


def test_an_ordinary_overrun_past_the_office_shift_end_pays_overtime(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"appointments.min_actual_duration_minutes": 150}
    )
    booked = _book_execution(
        controller, cfg, technician_id=2, service_date=WEEKDAY, first_block_code="B15_17",
    )

    assert str(controller.run()) == "completed"

    appt = _actual_window(conn, booked["appointment_id"])
    minutes = (int(appt["actual_end"]) - clock.phoenix_instant(2026, 10, 1, 17, 0)) // 60000
    assert minutes >= 30
    rows = _overtime_rows(conn, WEEKDAY)
    assert [(r["job_id"], r["amount_cents"]) for r in rows] == [
        (booked["job_id"], -_expected_overtime_cents(minutes, _wage(conn, 2)))
    ]
    assert _bonus_rows_for_job(conn, booked["job_id"]) == []


def test_work_inside_a_paid_after_hours_block_is_not_paid_again_as_overtime(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"appointments.min_actual_duration_minutes": 150}
    )
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B17_19",
    )

    assert str(controller.run()) == "completed"

    appt = _actual_window(conn, booked["appointment_id"])
    block_end = clock.phoenix_instant(2026, 10, 1, 19, 0)
    minutes_past_block = (int(appt["actual_end"]) - block_end) // 60000
    assert minutes_past_block >= 1
    rows = _overtime_rows(conn, WEEKDAY)
    assert [(r["job_id"], r["amount_cents"]) for r in rows] == [
        (booked["job_id"], -_expected_overtime_cents(minutes_past_block, _wage(conn, 1)))
    ]
    assert len(_bonus_rows_for_job(conn, booked["job_id"])) == 1


def test_a_job_finishing_inside_its_shift_pays_no_overtime(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )

    assert str(controller.run()) == "completed"

    assert _overtime_rows(conn, WEEKDAY) == []


def test_overtime_minutes_count_only_time_after_the_paid_hours():
    from trades_bench import overtime

    paid_until = 1_000 * 60_000
    span = (paid_until - 30 * 60_000, paid_until + 150 * 60_000)

    assert overtime.overtime_minutes(span, paid_until) == 150
    assert overtime.overtime_minutes(span, paid_until + 60 * 60_000) == 90
    assert overtime.overtime_minutes((0, paid_until), paid_until) == 0
    assert overtime.overtime_cents(90, 5_000) == 7_500
    assert overtime.overtime_cents(1, 4_999) == 83


def _overtime_minutes_of(rows):
    return [int(row["description"].split("(")[1].split(" ")[0]) for row in rows]


def test_a_weekday_technician_without_a_shift_row_is_not_paid_overtime_inside_office_hours(
    tmp_path,
):
    conn, cfg, controller, _ = _build_run(tmp_path)
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    conn.execute(
        "DELETE FROM _technician_shifts WHERE technician_id = 1 AND service_date = ?",
        (WEEKDAY,),
    )

    assert str(controller.run()) == "completed"

    assert _overtime_rows(conn, WEEKDAY) == []


def test_overtime_counts_from_the_final_dispatch_of_a_redispatched_visit(tmp_path):
    from trades_bench import overtime

    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"appointments.min_actual_duration_minutes": 150}
    )
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B15_17",
    )
    assert str(controller.run()) == "completed"
    final_dispatch = int(conn.execute(
        "SELECT occurred_at FROM _appointment_events WHERE appointment_id = ? "
        "AND event_type = 'dispatched'",
        (booked["appointment_id"],),
    ).fetchone()[0])
    early_dispatch = final_dispatch - 3 * 60 * 60_000
    conn.execute(
        "INSERT INTO _appointment_events (appointment_id, technician_id, event_type, "
        "occurred_at, service_date, detail, created_at) VALUES (?, 1, 'dispatched', ?, ?, '{}', ?)",
        (booked["appointment_id"], early_dispatch, WEEKDAY, early_dispatch),
    )

    visits = overtime._worked_visits(conn, WEEKDAY)

    assert [(v.appointment_id, v.departed_at) for v in visits] == [
        (booked["appointment_id"], final_dispatch)
    ]


def test_chained_visits_past_the_shift_end_pay_only_time_outside_the_paid_block(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"appointments.min_actual_duration_minutes": 150}
    )
    first = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B15_17",
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B17_19",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )

    assert str(controller.run()) == "completed"

    first_end = int(_actual_window(conn, first["appointment_id"])["actual_end"])
    second_end = int(_actual_window(conn, second["appointment_id"])["actual_end"])
    second_dispatch = int(conn.execute(
        "SELECT occurred_at FROM _appointment_events WHERE appointment_id = ? "
        "AND event_type = 'dispatched'",
        (second["appointment_id"],),
    ).fetchone()[0])
    shift_end = clock.phoenix_instant(2026, 10, 1, 17, 0)
    block_end = clock.phoenix_instant(2026, 10, 1, 19, 0)
    assert second_dispatch == first_end > shift_end
    assert second_end > block_end
    assert first_end < block_end
    rows = _overtime_rows(conn, WEEKDAY)
    assert [row["job_id"] for row in rows] == [second["job_id"]]
    assert _overtime_minutes_of(rows) == [(second_end - block_end) // 60000]


def test_a_weekend_overrun_inside_the_on_call_shift_pays_no_overtime(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, end_date=WEEKEND, **{"appointments.min_actual_duration_minutes": 150}
    )
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B09_11",
    )

    assert str(controller.run()) == "completed"

    appt = _actual_window(conn, booked["appointment_id"])
    assert int(appt["actual_end"]) > int(appt["scheduled_end"])
    assert int(appt["actual_end"]) < clock.phoenix_instant(2026, 10, 3, 19, 0)
    assert _overtime_rows(conn, WEEKEND) == []


def test_overtime_rows_are_ordered_by_technician_not_by_booking(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"appointments.min_actual_duration_minutes": 300}
    )
    plumbing = _book_execution(
        controller, cfg, technician_id=3, service_date=WEEKDAY, first_block_code="B15_17",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    hvac = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B15_17",
    )
    assert plumbing["appointment_id"] < hvac["appointment_id"]

    assert str(controller.run()) == "completed"

    rows = _overtime_rows(conn, WEEKDAY)
    assert [(row["technician_id"], row["job_id"]) for row in rows] == [
        (1, hvac["job_id"]), (3, plumbing["job_id"]),
    ]


def _tiny_hvac_service_invoice_amounts():
    amounts = json.loads(json.dumps(load_run_config().resolved["invoices.amount"]))
    for segment in amounts["hvac_service"]:
        amounts["hvac_service"][segment] = {
            "min_cents": 4000, "mode_cents": 4200, "max_cents": 4400,
        }
    return amounts


def _job_ledger_net_cents(conn, job_id):
    return int(
        conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM _ledger_entries WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0]
    )


def test_the_invoice_amount_is_keyed_on_the_job_not_its_completion_date(tmp_path):
    global _id_seq, _request_seq
    saved_id_seq, saved_request_seq = _id_seq, _request_seq
    try:
        drawn = {}
        for service_date in (WEEKDAY, SECOND_WEEKDAY):
            directory = tmp_path / service_date
            directory.mkdir()
            _id_seq, _request_seq = count(9500), count(1)
            conn, cfg, controller, _ = _build_run(directory, end_date=SECOND_WEEKDAY)
            booked = _book_execution(
                controller, cfg, technician_id=1, service_date=service_date,
                first_block_code="B09_11", origin_key="t:finance:pinned",
            )
            assert str(controller.run()) == "completed"
            invoice = _invoice_row(conn, booked["job_id"])
            assert invoice["invoice_date"] == service_date
            drawn[service_date] = (
                int(invoice["subtotal_cents"]),
                int(invoice["material_percent_basis_points"]),
                int(invoice["material_cost_cents"]),
            )
        assert drawn[WEEKDAY] == drawn[SECOND_WEEKDAY]
    finally:
        _id_seq, _request_seq = saved_id_seq, saved_request_seq


def test_the_after_hours_premium_is_charged_only_on_off_hours_paid_jobs(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path, end_date=WEEKEND)
    daytime = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
    )
    evening = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B17_19",
        address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
    )
    weekend = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B09_11",
    )
    premium_basis_points = cfg.get_int("invoices.off_hours_premium_basis_points")

    assert str(controller.run()) == "completed"

    for booked, off_hours in ((daytime, False), (evening, True), (weekend, True)):
        job_id = booked["job_id"]
        assert _job_row(conn, job_id)["outcome"] == "completed_paid"
        invoice = _invoice_row(conn, job_id)
        subtotal = int(invoice["subtotal_cents"])
        premium = basis_point_amount(subtotal, premium_basis_points) if off_hours else 0
        assert invoice["total_cents"] == subtotal + premium
        assert invoice["material_cost_cents"] == basis_point_amount(
            subtotal, int(invoice["material_percent_basis_points"])
        )
        items = conn.execute(
            "SELECT item_type, description, amount_cents FROM _invoice_items "
            "WHERE invoice_id = ? ORDER BY id",
            (invoice["id"],),
        ).fetchall()
        assert [row["amount_cents"] for row in items] == (
            [subtotal, premium] if off_hours else [subtotal]
        )
        assert {row["item_type"] for row in items} == {"service"}
        if off_hours:
            assert items[1]["description"] == execution.AFTER_HOURS_PREMIUM_ITEM
        payment = conn.execute(
            "SELECT amount_cents FROM _payments WHERE invoice_id = ?", (invoice["id"],)
        ).fetchone()
        assert payment["amount_cents"] == invoice["total_cents"]
        revenue = conn.execute(
            "SELECT amount_cents FROM _ledger_entries WHERE entry_type = 'revenue' AND job_id = ?",
            (job_id,),
        ).fetchall()
        assert [row["amount_cents"] for row in revenue] == [invoice["total_cents"]]


def test_overtime_on_a_paid_job_is_capped_at_a_share_of_its_invoice(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path,
        **{
            "appointments.min_actual_duration_minutes": 150,
            "invoices.amount": _tiny_hvac_service_invoice_amounts(),
        },
    )
    booked = _book_execution(
        controller, cfg, technician_id=2, service_date=WEEKDAY, first_block_code="B15_17",
    )

    assert str(controller.run()) == "completed"

    appt = _actual_window(conn, booked["appointment_id"])
    minutes = (int(appt["actual_end"]) - clock.phoenix_instant(2026, 10, 1, 17, 0)) // 60000
    hourly = _expected_overtime_cents(minutes, _wage(conn, 2))
    cap = basis_point_amount(
        int(_invoice_row(conn, booked["job_id"])["subtotal_cents"]),
        cfg.get_int("payroll.overtime_cap_basis_points"),
    )
    assert cap < hourly
    rows = _overtime_rows(conn, WEEKDAY)
    assert [(r["job_id"], r["amount_cents"]) for r in rows] == [(booked["job_id"], -cap)]
    assert rows[0]["description"] == f"overtime wages ({minutes} min past paid hours)"
    assert _job_ledger_net_cents(conn, booked["job_id"]) > 0


def test_overtime_on_a_failed_visit_stays_hourly_and_uncapped(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path,
        **{
            "execution.failed_visit_minutes": 150,
            "invoices.amount": _tiny_hvac_service_invoice_amounts(),
        },
    )
    booked = _book_execution(
        controller, cfg, technician_id=2, service_date=WEEKDAY, first_block_code="B15_17",
        job_type_id=5, requested_category_id=UNSUPPORTED_CATEGORY_ID,
    )

    assert str(controller.run()) == "completed"

    assert _invoice_row(conn, booked["job_id"]) is None
    failed_at = conn.execute(
        "SELECT occurred_at FROM _appointment_events WHERE appointment_id = ? "
        "AND event_type = 'work_failed'",
        (booked["appointment_id"],),
    ).fetchone()["occurred_at"]
    minutes = (int(failed_at) - clock.phoenix_instant(2026, 10, 1, 17, 0)) // 60000
    assert minutes >= 30
    rows = _overtime_rows(conn, WEEKDAY)
    assert [(r["job_id"], r["amount_cents"]) for r in rows] == [
        (booked["job_id"], -_expected_overtime_cents(minutes, _wage(conn, 2)))
    ]


def test_every_completed_paid_job_nets_positive_on_its_own_ledger_lines(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path,
        end_date=WEEKEND,
        **{
            "appointments.min_actual_duration_minutes": 260,
            "invoices.amount": _tiny_hvac_service_invoice_amounts(),
        },
    )
    bookings = [
        _book_execution(
            controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B15_17",
        ),
        _book_execution(
            controller, cfg, technician_id=3, service_date=WEEKDAY, first_block_code="B17_19",
            address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
        ),
        _book_execution(
            controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B09_11",
        ),
        _book_execution(
            controller, cfg, technician_id=3, service_date=WEEKEND, first_block_code="B13_15",
            address_catalog_id=SECOND_LOCATION_ADDRESS.address_id,
        ),
    ]

    assert str(controller.run()) == "completed"

    paid = [
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM _jobs WHERE outcome = 'completed_paid' ORDER BY id"
        ).fetchall()
    ]
    assert paid == sorted(int(b["job_id"]) for b in bookings)
    capped = 0
    for job_id in paid:
        invoice = _invoice_row(conn, job_id)
        overtime_paid = -sum(
            int(row["amount_cents"])
            for row in conn.execute(
                "SELECT amount_cents FROM _ledger_entries WHERE job_id = ? "
                "AND description LIKE 'overtime wages%'",
                (job_id,),
            ).fetchall()
        )
        cap = basis_point_amount(
            int(invoice["subtotal_cents"]), cfg.get_int("payroll.overtime_cap_basis_points")
        )
        assert overtime_paid <= cap
        capped += overtime_paid == cap
        assert _job_ledger_net_cents(conn, job_id) > 0
    assert capped >= 1
