from __future__ import annotations

import json
import math
import statistics
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trades_bench import travel
from trades_bench.config import load_run_config
from trades_bench.enums import ConditionCode, KeyedStreamName
from trades_bench.errors import EngineError, ErrorCode
from trades_bench.rng import Rng
from trades_bench.rng import keyed_draw as rng_keyed_draw
from trades_bench.rng import stream_seed as rng_stream_seed
from trades_bench.rng import uniform01 as rng_uniform01

MASTER_SEED = 42
SPEC_VERSION = "1.0.0"

CONFIG = load_run_config()

DISTANCE_KM = 10.0

_INDEPENDENT_STANDARD_NORMAL = statistics.NormalDist()


def _rng() -> Rng:
    rng = Rng(MASTER_SEED, SPEC_VERSION)
    rng.enter_txn()
    return rng


def _independent_noise_multiplier(
    entity_type: str, entity_id: int, scope: str, event_kind: str, draw_index: int
) -> Decimal:
    seed = rng_stream_seed(MASTER_SEED, SPEC_VERSION, KeyedStreamName.TRAVEL_NOISE.value)
    u = rng_keyed_draw(seed, entity_type, entity_id, scope, event_kind, draw_index)
    p = rng_uniform01(u)
    z = _INDEPENDENT_STANDARD_NORMAL.inv_cdf(float(p))
    sigma = float(CONFIG.get_decimal("travel.noise.sigma"))
    raw = Decimal(math.exp(sigma * z))
    lo = CONFIG.get_decimal("travel.noise.min_multiplier")
    hi = CONFIG.get_decimal("travel.noise.max_multiplier")
    return min(max(raw, lo), hi)


def test_inbound_trip_noise_key_matches_the_exact_spec_key_tuple():
    key = travel.inbound_trip_noise_key(appointment_id=7, service_date="2026-10-01", trip_index=2)
    assert key == travel.TravelNoiseKey("appointment", 7, "2026-10-01", "travel_noise", 2)


def test_return_home_trip_noise_key_matches_the_exact_spec_key_tuple():
    key = travel.return_home_trip_noise_key(
        technician_id=3, service_date="2026-10-01", block_index=1
    )
    assert key == travel.TravelNoiseKey(
        "technician", 3, "2026-10-01", "return_home_travel", 1
    )


def test_det07a_identical_trips_at_identical_times_reproduce_identical_minutes():
    rng_a = _rng()
    rng_b = _rng()
    key = travel.inbound_trip_noise_key(1, "2026-10-01", 0)
    minutes_a = travel.actual_travel_minutes(
        rng_a,
        CONFIG,
        distance_km=DISTANCE_KM,
        day_type="weekday",
        minute_of_day=600,
        condition_code=ConditionCode.CLEAR,
        noise_key=key,
    )
    minutes_b = travel.actual_travel_minutes(
        rng_b,
        CONFIG,
        distance_km=DISTANCE_KM,
        day_type="weekday",
        minute_of_day=600,
        condition_code=ConditionCode.CLEAR,
        noise_key=key,
    )
    assert minutes_a == minutes_b


def test_det07b_an_outlying_destination_still_computes():
    rng = _rng()
    minutes = travel.actual_travel_minutes(
        rng,
        CONFIG,
        distance_km=25.0,
        day_type="weekend",
        minute_of_day=45,
        condition_code=ConditionCode.RAIN,
        noise_key=travel.return_home_trip_noise_key(1, "2026-10-03", 0),
    )
    assert isinstance(minutes, int)
    assert minutes > 0


def test_actual_travel_minutes_returns_a_bare_integer_with_no_decomposition():
    rng = _rng()
    result = travel.actual_travel_minutes(
        rng,
        CONFIG,
        distance_km=DISTANCE_KM,
        day_type="weekday",
        minute_of_day=600,
        condition_code=ConditionCode.CLEAR,
        noise_key=travel.inbound_trip_noise_key(1, "2026-10-01", 0),
    )
    assert type(result) is int


def test_det07d_an_unrelated_entitys_draw_does_not_shift_this_trips_noise():
    rng = _rng()
    key = travel.inbound_trip_noise_key(1, "2026-10-01", 0)

    minutes_before = travel.actual_travel_minutes(
        rng,
        CONFIG,
        distance_km=DISTANCE_KM,
        day_type="weekday",
        minute_of_day=600,
        condition_code=ConditionCode.CLEAR,
        noise_key=key,
    )

    travel.noise_multiplier(
        rng, CONFIG, travel.return_home_trip_noise_key(99, "2026-10-01", 5)
    )

    minutes_after = travel.actual_travel_minutes(
        rng,
        CONFIG,
        distance_km=DISTANCE_KM,
        day_type="weekday",
        minute_of_day=600,
        condition_code=ConditionCode.CLEAR,
        noise_key=key,
    )
    assert minutes_before == minutes_after


def test_peak_band_yields_strictly_more_minutes_than_off_peak_at_fixed_distance():
    rng = _rng()
    key = travel.inbound_trip_noise_key(1, "2026-10-01", 0)
    peak_minute = 400
    off_peak_minute = 600
    assert travel.band_for_minute(CONFIG, peak_minute) == "morning_peak"
    assert travel.band_for_minute(CONFIG, off_peak_minute) == "midday"

    peak = travel.actual_travel_minutes(
        rng,
        CONFIG,
        distance_km=DISTANCE_KM,
        day_type="weekday",
        minute_of_day=peak_minute,
        condition_code=ConditionCode.CLEAR,
        noise_key=key,
    )
    off_peak = travel.actual_travel_minutes(
        rng,
        CONFIG,
        distance_km=DISTANCE_KM,
        day_type="weekday",
        minute_of_day=off_peak_minute,
        condition_code=ConditionCode.CLEAR,
        noise_key=key,
    )
    assert peak > off_peak


def test_storm_yields_strictly_more_minutes_than_clear_at_fixed_distance():
    rng = _rng()
    key = travel.inbound_trip_noise_key(1, "2026-10-01", 0)

    storm = travel.actual_travel_minutes(
        rng,
        CONFIG,
        distance_km=DISTANCE_KM,
        day_type="weekday",
        minute_of_day=600,
        condition_code=ConditionCode.STORM,
        noise_key=key,
    )
    clear = travel.actual_travel_minutes(
        rng,
        CONFIG,
        distance_km=DISTANCE_KM,
        day_type="weekday",
        minute_of_day=600,
        condition_code=ConditionCode.CLEAR,
        noise_key=key,
    )
    assert storm > clear


def test_weather_multiplier_table_missing_a_condition_code_is_rejected():
    weather = {
        k: float(v) for k, v in CONFIG.resolved["travel.weather_travel_multipliers"].items()
    }
    del weather["storm"]
    with pytest.raises(EngineError) as ei:
        load_run_config(
            scenario_json=json.dumps({"keys": {"travel.weather_travel_multipliers": weather}})
        )
    assert ei.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_noise_multiplier_is_clamped_and_strictly_positive():
    rng = _rng()
    lo = CONFIG.get_decimal("travel.noise.min_multiplier")
    hi = CONFIG.get_decimal("travel.noise.max_multiplier")
    for entity_id in range(1, 30):
        key = travel.inbound_trip_noise_key(entity_id, "2026-10-01", 0)
        multiplier = travel.noise_multiplier(rng, CONFIG, key)
        assert lo <= multiplier <= hi
        assert multiplier > 0


@given(st.integers(min_value=1, max_value=10_000))
@settings(max_examples=100, deadline=None)
def test_noise_multiplier_property_always_within_configured_clamp(entity_id):
    rng = _rng()
    lo = CONFIG.get_decimal("travel.noise.min_multiplier")
    hi = CONFIG.get_decimal("travel.noise.max_multiplier")
    key = travel.inbound_trip_noise_key(entity_id, "2026-10-01", 0)
    multiplier = travel.noise_multiplier(rng, CONFIG, key)
    assert lo <= multiplier <= hi


def test_actual_travel_minutes_floors_at_the_configured_minimum():
    rng = _rng()
    floor = CONFIG.get_int("travel.min_travel_minutes")
    minutes = travel.actual_travel_minutes(
        rng,
        CONFIG,
        distance_km=0.0001,
        day_type="weekday",
        minute_of_day=600,
        condition_code=ConditionCode.CLEAR,
        noise_key=travel.inbound_trip_noise_key(1, "2026-10-01", 0),
    )
    assert minutes == floor


def test_band_for_minute_covers_the_default_band_boundaries():
    assert travel.band_for_minute(CONFIG, 0) == "night"
    assert travel.band_for_minute(CONFIG, 359) == "night"
    assert travel.band_for_minute(CONFIG, 360) == "morning_peak"
    assert travel.band_for_minute(CONFIG, 1439) == "night_late"


def test_band_for_minute_rejects_out_of_range_minute():
    with pytest.raises(EngineError) as ei:
        travel.band_for_minute(CONFIG, 1440)
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT


def test_actual_travel_minutes_rejects_an_invalid_day_type():
    rng = _rng()
    with pytest.raises(EngineError) as ei:
        travel.actual_travel_minutes(
            rng,
            CONFIG,
            distance_km=DISTANCE_KM,
            day_type="holiday",
            minute_of_day=600,
            condition_code=ConditionCode.CLEAR,
            noise_key=travel.inbound_trip_noise_key(1, "2026-10-01", 0),
        )
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT


def test_noise_multiplier_inbound_key_matches_an_independently_built_uniform01_draw():
    rng = _rng()
    appointment_id = 4
    service_date = "2026-10-01"
    trip_index = 1
    key = travel.inbound_trip_noise_key(appointment_id, service_date, trip_index)
    expected = _independent_noise_multiplier(
        "appointment", appointment_id, service_date, "travel_noise", trip_index
    )
    assert travel.noise_multiplier(rng, CONFIG, key) == expected


def test_noise_multiplier_return_home_key_matches_an_independently_built_uniform01_draw():
    rng = _rng()
    technician_id = 3
    service_date = "2026-10-01"
    block_index = 2
    key = travel.return_home_trip_noise_key(technician_id, service_date, block_index)
    expected = _independent_noise_multiplier(
        "technician", technician_id, service_date, "return_home_travel", block_index
    )
    assert travel.noise_multiplier(rng, CONFIG, key) == expected


def test_noise_multiplier_is_repeatable_for_the_same_key():
    rng = _rng()
    key = travel.inbound_trip_noise_key(4, "2026-10-01", 1)
    first = travel.noise_multiplier(rng, CONFIG, key)
    second = travel.noise_multiplier(rng, CONFIG, key)
    assert first == second


def test_noise_multiplier_from_uniform01_stays_finite_and_clamped_at_the_top_of_the_u64_range():
    lo = CONFIG.get_decimal("travel.noise.min_multiplier")
    hi = CONFIG.get_decimal("travel.noise.max_multiplier")
    for offset in range(4):
        p = rng_uniform01((1 << 64) - 1 - offset)
        multiplier = travel._noise_multiplier_from_uniform01(p, CONFIG)
        assert math.isfinite(float(multiplier))
        assert lo <= multiplier <= hi
        assert multiplier > 0


def test_noise_multiplier_from_uniform01_stays_finite_and_clamped_at_the_bottom_of_the_u64_range():
    lo = CONFIG.get_decimal("travel.noise.min_multiplier")
    hi = CONFIG.get_decimal("travel.noise.max_multiplier")
    for u in range(4):
        p = rng_uniform01(u)
        multiplier = travel._noise_multiplier_from_uniform01(p, CONFIG)
        assert math.isfinite(float(multiplier))
        assert lo <= multiplier <= hi
        assert multiplier > 0


def test_reassignment_style_trip_index_bump_draws_fresh_noise():
    rng = _rng()
    original = travel.noise_multiplier(rng, CONFIG, travel.inbound_trip_noise_key(1, "2026-10-01", 0))
    redispatched = travel.noise_multiplier(
        rng, CONFIG, travel.inbound_trip_noise_key(1, "2026-10-01", 1)
    )
    unrelated_appointment = travel.noise_multiplier(
        rng, CONFIG, travel.inbound_trip_noise_key(2, "2026-10-01", 0)
    )
    assert len({original, redispatched, unrelated_appointment}) == 3
