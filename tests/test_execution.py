from __future__ import annotations

import json
import shutil
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
    travel,
)
from trades_bench._dump import dump_hash
from trades_bench.config import load_run_config
from trades_bench.controller import Controller
from trades_bench.enums import ConditionCode, EventKind
from trades_bench.errors import EngineError, ErrorCode
from trades_bench.events import Events
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
    requested_category_id: int = HVAC_SERVICE_CATEGORY_ID,
) -> int:
    row_id = next(_id_seq)
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at) "
        "VALUES (?, 'Acme', 'commercial', 'active', ?, ?)",
        (row_id, now_ms, now_ms),
    )
    if config is not None:
        demand.register_customer_repeat_state(
            conn, config, customer_id=row_id, segment="commercial", now_ms=now_ms
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
        "prospect_id, arrival_event_id, created_at) VALUES (?, 'commercial', ?, NULL, NULL, ?)",
        (row_id, requested_category_id, now_ms),
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
                     requested_category_id=None):
    now = clock.now(controller.conn)
    category_id = (
        requested_category_id
        if requested_category_id is not None
        else _TECHNICIAN_QUALIFIED_CATEGORY_ID[technician_id]
    )
    location_id = _customer_location_call(
        controller.conn, now, service_date, address_catalog_id=address_catalog_id, config=cfg,
        requested_category_id=category_id,
    )
    return _book(
        controller.services, cfg,
        call_id=location_id, customer_id=location_id, location_id=location_id,
        job_type_id=job_type_id, requested_service_name="HVAC service",
        priority="normal", summary="x",
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
    noise_key = travel.inbound_trip_noise_key(appointment_id, WEEKDAY, 0)
    actual_travel = travel.actual_travel_minutes(
        shadow, cfg, distance_km=distance_km, day_type="weekday",
        minute_of_day=departure_minute, condition_code=condition_code, noise_key=noise_key,
    )
    expected_arrival = expected_departure + actual_travel * 60000

    start_variation = execution.start_variation_minutes(shadow, appointment_id, WEEKDAY)
    expected_work_started = max(expected_arrival, block_start + start_variation * 60000)

    duration_variation = execution.duration_variation_minutes(shadow, appointment_id, WEEKDAY)
    multiplier = execution.execution_duration_multiplier(cfg)
    base_minutes = int((Decimal(120) * multiplier).to_integral_value(rounding=ROUND_HALF_UP))
    duration_minutes = max(
        base_minutes + duration_variation, cfg.get_int("appointments.min_actual_duration_minutes")
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
        "SELECT unresolved_execution_count, closing_at FROM _day_state WHERE service_date = ?",
        (WEEKDAY,),
    ).fetchone()
    assert day["unresolved_execution_count"] == 0
    assert day["closing_at"] == clock.phoenix_instant(2026, 10, 1, 19, 0)

    starting_cash = cfg.get_int("finance.starting_cash")
    overhead = cfg.get_int("finance.daily_operating_overhead")
    wage_total = _weekday_wage_total(conn)
    expected_cash = starting_cash - constants.WEEKDAY_PAY_MULTIPLIER * wage_total - overhead
    assert controller.services.ledger.cash_balance() == expected_cash

    home = execution.technician_home_point(conn, 1)
    loc = _technician_location_row(conn, 1)
    assert loc["location_state"] == "home"
    assert loc["latitude"] == home.latitude
    assert loc["longitude"] == home.longitude


def test_two_block_installation_duration_scales_with_reserved_block_count(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=2,
    )
    appointment_id = booked["appointment_id"]

    reserved_blocks = int(
        conn.execute(
            "SELECT COUNT(*) c FROM _appointment_blocks WHERE appointment_id = ?",
            (appointment_id,),
        ).fetchone()["c"]
    )
    assert reserved_blocks == 2

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
    noise_key = travel.inbound_trip_noise_key(appointment_id, WEEKDAY, 0)
    actual_travel = travel.actual_travel_minutes(
        shadow, cfg, distance_km=distance_km, day_type="weekday",
        minute_of_day=departure_minute, condition_code=condition_code, noise_key=noise_key,
    )
    expected_arrival = expected_departure + actual_travel * 60000

    start_variation = execution.start_variation_minutes(shadow, appointment_id, WEEKDAY)
    expected_work_started = max(expected_arrival, block_start + start_variation * 60000)

    duration_variation = execution.duration_variation_minutes(shadow, appointment_id, WEEKDAY)
    multiplier = execution.execution_duration_multiplier(cfg)
    base_minutes = int(
        (Decimal(120 * reserved_blocks) * multiplier).to_integral_value(rounding=ROUND_HALF_UP)
    )
    duration_minutes = max(
        base_minutes + duration_variation, cfg.get_int("appointments.min_actual_duration_minutes")
    )
    expected_completion = expected_work_started + duration_minutes * 60000

    status = controller.run()
    assert str(status) == "completed"

    appt = conn.execute(
        "SELECT actual_start, actual_end FROM _appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    assert appt["actual_start"] == expected_work_started
    assert appt["actual_end"] == expected_completion
    assert appt["actual_end"] - appt["actual_start"] >= 240 * 60000 - 15 * 60000


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
        "SELECT unresolved_execution_count, closing_at FROM _day_state WHERE service_date = ?",
        (WEEKDAY,),
    ).fetchone()
    assert day["unresolved_execution_count"] == 0
    assert day["closing_at"] == appt["actual_end"]


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
    noise_key = travel.inbound_trip_noise_key(second["appointment_id"], WEEKDAY, trip_index)
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


def test_idle_gap_returns_home_then_dispatches_post_idle(tmp_path):
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

    movement = conn.execute(
        "SELECT * FROM _technician_movements WHERE technician_id = 1"
    ).fetchone()
    assert movement is not None
    assert movement["movement_kind"] == "return_home"
    assert movement["departed_at"] == first_completed["occurred_at"]

    home = execution.technician_home_point(conn, 1)
    assert movement["destination_latitude"] == home.latitude
    assert movement["destination_longitude"] == home.longitude

    home_arrival_event = conn.execute(
        "SELECT scheduled_at_ms FROM _events WHERE kind = ? AND entity_id = 1 "
        "AND scheduled_at_ms >= ?",
        (EventKind.TECHNICIAN_HOME_ARRIVAL.value, first_completed["occurred_at"]),
    ).fetchone()
    assert home_arrival_event is not None
    assert home_arrival_event["scheduled_at_ms"] == movement["arrives_at"]

    second_dispatched = next(e for e in second_events if e["event_type"] == "dispatched")
    second_block_start = clock.phoenix_instant(2026, 10, 1, 13, 0)
    destination = execution.location_point(
        conn, execution._appointment_job_location_id(conn, second["appointment_id"])[1]
    )
    planned_from_home = execution.planned_travel_minutes(
        conn, ARTIFACT, cfg, origin=home, destination=destination,
        service_date=WEEKDAY, minute_of_day=13 * 60,
    )
    expected_dispatch = max(
        second_block_start - planned_from_home * 60000, movement["arrives_at"]
    )
    assert second_dispatched["occurred_at"] == expected_dispatch


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


def test_post_idle_dispatch_clamp_binds_when_home_arrival_is_the_later_bound(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"travel.speed_by_time_band": _SLOW_MIDDAY_LONG_RETURN_OVERRIDE}
    )
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        address_catalog_id=_FAR_ADDRESS_ID,
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B13_15",
        address_catalog_id=_NEAR_ADDRESS_ID,
    )
    status = controller.run()
    assert str(status) == "completed"

    second_events = _appointment_event_rows(conn, second["appointment_id"])
    second_dispatched = next(e for e in second_events if e["event_type"] == "dispatched")

    movement = conn.execute(
        "SELECT * FROM _technician_movements WHERE technician_id = 1"
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
        "ORDER BY id",
        (service_date,),
    ).fetchall()


def test_off_hours_settlement_weekend_standard_job(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path, end_date=WEEKEND)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B09_11",
        job_type_id=1,
    )
    wage = int(
        conn.execute("SELECT hourly_wage_cents FROM _technicians WHERE id = 1").fetchone()[0]
    )
    bonus = cfg.get_int("payroll.off_hours_bonus_per_job")

    status = controller.run()
    assert str(status) == "completed"

    entries = _ledger_off_hours_entries(conn, WEEKEND)
    assert len(entries) == 2
    assert entries[0]["entry_type"] == "payroll_off_hours"
    assert entries[0]["amount_cents"] == -(2 * wage)
    assert entries[0]["technician_id"] == 1
    assert entries[0]["job_id"] == booked["job_id"]
    assert entries[1]["entry_type"] == "payroll_off_hours_bonus"
    assert entries[1]["amount_cents"] == -bonus
    assert entries[1]["technician_id"] == 1
    assert entries[1]["job_id"] == booked["job_id"]


def test_off_hours_settlement_weekend_installation_is_four_hours_one_bonus(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path, end_date=WEEKEND)
    _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKEND, first_block_code="B09_11",
        job_type_id=2,
    )
    wage = int(
        conn.execute("SELECT hourly_wage_cents FROM _technicians WHERE id = 1").fetchone()[0]
    )
    bonus = cfg.get_int("payroll.off_hours_bonus_per_job")

    status = controller.run()
    assert str(status) == "completed"

    entries = _ledger_off_hours_entries(conn, WEEKEND)
    assert len(entries) == 2
    assert entries[0]["amount_cents"] == -(4 * wage)
    assert entries[1]["amount_cents"] == -bonus


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

    wage = int(
        conn.execute("SELECT hourly_wage_cents FROM _technicians WHERE id = 1").fetchone()[0]
    )
    bonus = cfg.get_int("payroll.off_hours_bonus_per_job")

    status = controller.run()
    assert str(status) == "completed"

    entries = _ledger_off_hours_entries(conn, WEEKDAY)
    assert len(entries) == 2
    assert entries[0]["amount_cents"] == -(2 * wage)
    assert entries[1]["amount_cents"] == -bonus


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

    wage3 = int(
        conn.execute("SELECT hourly_wage_cents FROM _technicians WHERE id = 3").fetchone()[0]
    )
    bonus = cfg.get_int("payroll.off_hours_bonus_per_job")

    status = controller.run()
    assert str(status) == "completed"

    entries = _ledger_off_hours_entries(conn, WEEKEND)
    assert len(entries) == 2
    assert entries[0]["technician_id"] == 3
    assert entries[0]["amount_cents"] == -(2 * wage3)
    assert entries[1]["technician_id"] == 3
    assert entries[1]["amount_cents"] == -bonus


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
    original_noise_key = travel.inbound_trip_noise_key(appointment_id, WEEKDAY, 0)
    new_noise_key = travel.inbound_trip_noise_key(appointment_id, WEEKDAY, 1)
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
    )
    second = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B11_13",
        requested_category_id=PLUMBING_CATEGORY_ID,
    )
    first_id = first["appointment_id"]
    second_id = second["appointment_id"]

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
        "SELECT unresolved_execution_count, closing_at FROM _day_state WHERE service_date = ?",
        (service_date,),
    ).fetchone()


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
    assert _day_state_row(conn, WEEKDAY)["closing_at"] == clock.phoenix_instant(
        2026, 10, 1, 19, 0
    )


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


def test_install_no_show_fires_at_first_block_end_releases_both_holds_and_draws_nothing(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller, cfg, technician_id=1, service_date=WEEKDAY, first_block_code="B09_11",
        job_type_id=2,
    )
    appointment_id = booked["appointment_id"]
    cutoff = clock.phoenix_instant(2026, 10, 1, 11, 0)

    assert _guard_event(conn, appointment_id)["scheduled_at_ms"] == cutoff
    assert _hold_count(conn, appointment_id) == 2

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
    assert [b["block_code"] for b in blocks] == ["B09_11", "B11_13"]
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


def test_off_hours_no_show_keeps_the_accrual_mark_and_still_pays(tmp_path):
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
    bonus = cfg.get_int("payroll.off_hours_bonus_per_job")

    status = controller.run()
    assert str(status) == "completed"

    assert _appointment_status(conn, appointment_id) == "no_show"
    mark = conn.execute(
        "SELECT status, payload FROM _events WHERE kind = ? AND entity_id = ?",
        (EventKind.OFF_HOURS_ACCRUAL_MARK.value, appointment_id),
    ).fetchone()
    assert mark["status"] == "applied"
    assert json.loads(mark["payload"])["technician_id"] == 1

    entries = _ledger_off_hours_entries(conn, WEEKEND)
    assert len(entries) == 2
    assert entries[0]["entry_type"] == "payroll_off_hours"
    assert entries[0]["amount_cents"] == -(2 * wage)
    assert entries[0]["technician_id"] == 1
    assert entries[0]["job_id"] == booked["job_id"]
    assert entries[1]["entry_type"] == "payroll_off_hours_bonus"
    assert entries[1]["amount_cents"] == -bonus

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
    assert payload["block_index"] == execution.block_index("B11_13")

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
