from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from _helpers import GEO_ARTIFACT_DIR, geo_run_config, register_conn
from csr_bench import SPEC_VERSION
from csr_bench._dump import dump_hash
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.rng import Rng
from csr_bench.run_init import initialize_run, open_run_db
from csr_bench.weather import _band_name, _bands_milli, get_weather

_ALLOWED_KEYS_WITH_STORM = frozenset(
    {
        "zone_id",
        "date",
        "kind",
        "horizon_days",
        "high_f",
        "low_f",
        "precipitation_probability",
        "storm",
        "storm_severity",
    }
)
_ALLOWED_KEYS_WITHOUT_STORM = _ALLOWED_KEYS_WITH_STORM - {"storm_severity"}


def _tenths(value) -> int:
    return int((Decimal(value) * 10).to_integral_value(rounding=ROUND_HALF_UP))


def _milli(value) -> int:
    return int((Decimal(value) * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def _whole(tenths: int) -> int:
    return int((Decimal(tenths) / Decimal(10)).to_integral_value(rounding=ROUND_HALF_UP))


def _span_days(cfg) -> int:
    start = date.fromisoformat(cfg.get_string("weather.precompute_start_date"))
    end = date.fromisoformat(cfg.get_string("weather.precompute_end_date"))
    return (end - start).days + 1


def _init(tmp_path, seed: int, name: str = "run.db"):
    cfg = geo_run_config()
    path = tmp_path / name
    run_id = initialize_run(path, master_seed=seed, config=cfg, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    return conn, run_id, cfg


def test_precompute_writes_expected_row_counts(tmp_path):
    conn, run_id, cfg = _init(tmp_path, 1)
    span_days = _span_days(cfg)
    assert span_days == 204
    assert cfg.get_string("weather.precompute_start_date") == "2026-04-01"
    assert cfg.get_string("weather.precompute_end_date") == "2026-10-21"

    daily_count = conn.execute("SELECT COUNT(*) c FROM _zone_weather_daily").fetchone()["c"]
    state_count = conn.execute("SELECT COUNT(*) c FROM _weather_process_state").fetchone()["c"]
    assert daily_count == span_days * 10
    assert state_count == 1

    state = conn.execute(
        "SELECT precompute_start_date, precompute_end_date FROM _weather_process_state"
    ).fetchone()
    assert state["precompute_start_date"] == "2026-04-01"
    assert state["precompute_end_date"] == "2026-10-21"

    per_zone_counts = conn.execute(
        "SELECT zone_id, COUNT(*) c FROM _zone_weather_daily GROUP BY zone_id"
    ).fetchall()
    assert len(per_zone_counts) == 10
    assert all(row["c"] == span_days for row in per_zone_counts)


def test_same_seed_reproduces_weather_and_counter_advances_exactly(tmp_path):
    conn_a, _rid_a, cfg = _init(tmp_path, 42, "a.db")
    conn_b, _rid_b, _cfg_b = _init(tmp_path, 42, "b.db")
    conn_c, _rid_c, _cfg_c = _init(tmp_path, 43, "c.db")

    def weather_rows(conn: sqlite3.Connection) -> list[tuple]:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT zone_id, local_date, high_f_tenths, low_f_tenths, "
                "precipitation_mm_tenths, wind_mph_tenths, storm_flag, storm_severity_milli, "
                "condition_code FROM _zone_weather_daily ORDER BY zone_id, local_date"
            ).fetchall()
        ]

    rows_a = weather_rows(conn_a)
    rows_b = weather_rows(conn_b)
    rows_c = weather_rows(conn_c)
    assert rows_a == rows_b
    assert rows_a != rows_c

    state_a = dict(conn_a.execute("SELECT * FROM _weather_process_state").fetchone())
    state_b = dict(conn_b.execute("SELECT * FROM _weather_process_state").fetchone())
    assert state_a == state_b

    span_days = _span_days(cfg)
    counter = conn_a.execute(
        "SELECT counter FROM _rng_state WHERE stream_name = 'weather_process'"
    ).fetchone()["counter"]
    assert counter == 2 + span_days


def test_dump_hash_equal_for_same_seed_runs(tmp_path):
    conn_a, rid_a, _cfg_a = _init(tmp_path, 55, "a.db")
    conn_b, rid_b, _cfg_b = _init(tmp_path, 55, "b.db")
    conn_c, rid_c, _cfg_c = _init(tmp_path, 56, "c.db")
    assert dump_hash(conn_a, rid_a) == dump_hash(conn_b, rid_b)
    assert dump_hash(conn_a, rid_a) != dump_hash(conn_c, rid_c)


def test_seasonal_and_zone_offset_shape(tmp_path):
    conn, _run_id, cfg = _init(tmp_path, 2)

    def month_avg_high(month: int) -> float:
        rows = conn.execute(
            "SELECT high_f_tenths FROM _zone_weather_daily WHERE zone_id = 1 "
            "AND local_date LIKE ?",
            (f"2026-{month:02d}-%",),
        ).fetchall()
        return sum(row[0] for row in rows) / len(rows)

    assert month_avg_high(7) > month_avg_high(4)

    diurnal_tenths = _tenths(cfg.get_decimal("weather.diurnal_range_f"))
    mismatched = conn.execute(
        "SELECT COUNT(*) c FROM _zone_weather_daily WHERE high_f_tenths - low_f_tenths != ?",
        (diurnal_tenths,),
    ).fetchone()["c"]
    assert mismatched == 0

    offsets = cfg.resolved["weather.zone_offset_f"]
    expected_delta = _tenths(offsets[9]) - _tenths(offsets[1])
    rows = conn.execute(
        "SELECT a.high_f_tenths - b.high_f_tenths AS delta FROM _zone_weather_daily a "
        "JOIN _zone_weather_daily b ON a.local_date = b.local_date "
        "WHERE a.zone_id = 10 AND b.zone_id = 2"
    ).fetchall()
    assert len(rows) == _span_days(cfg)
    assert all(row["delta"] == expected_delta for row in rows)


def test_storm_frequency_and_severity_invariants(tmp_path):
    conn, _run_id, cfg = _init(tmp_path, 7)

    def storm_rate(pattern: str) -> float:
        total = conn.execute(
            "SELECT COUNT(*) c FROM _zone_weather_daily WHERE local_date LIKE ?", (pattern,)
        ).fetchone()["c"]
        storms = conn.execute(
            "SELECT COUNT(*) c FROM _zone_weather_daily WHERE storm_flag = 1 "
            "AND local_date LIKE ?",
            (pattern,),
        ).fetchone()["c"]
        return storms / total

    in_window_rate = sum(storm_rate(f"2026-{m:02d}-%") for m in (6, 7, 8, 9)) / 4
    out_window_rate = sum(storm_rate(f"2026-{m:02d}-%") for m in (4, 5, 10)) / 3
    assert in_window_rate > out_window_rate

    mismatch = conn.execute(
        "SELECT COUNT(*) c FROM _zone_weather_daily WHERE "
        "(storm_flag = 1 AND storm_severity_milli IS NULL) OR "
        "(storm_flag = 0 AND storm_severity_milli IS NOT NULL)"
    ).fetchone()["c"]
    assert mismatch == 0

    codes = {row[0] for row in conn.execute("SELECT DISTINCT condition_code FROM _zone_weather_daily")}
    assert codes <= {"clear", "cool", "hot", "windy", "extreme_heat", "rain", "storm"}

    bands = cfg.resolved["weather.storm_severity_bands"]
    moderate_lo_milli = _milli(bands[1][1])
    storm_rows = conn.execute(
        "SELECT storm_severity_milli, condition_code FROM _zone_weather_daily WHERE storm_flag = 1"
    ).fetchall()
    assert storm_rows
    for row in storm_rows:
        if row["storm_severity_milli"] < moderate_lo_milli:
            assert row["condition_code"] == "rain"
        else:
            assert row["condition_code"] == "storm"


def test_weather_get_observed_matches_stored_row(tmp_path):
    conn, _run_id, cfg = _init(tmp_path, 4)
    rng = Rng(4, SPEC_VERSION)

    row = conn.execute(
        "SELECT * FROM _zone_weather_daily WHERE zone_id = 5 AND local_date = '2026-10-01'"
    ).fetchone()
    result = get_weather(conn, cfg, rng, zone_id=5, date="2026-10-01", today="2026-10-01")

    assert result["kind"] == "observed"
    assert result["horizon_days"] == 0
    assert result["high_f"] == _whole(row["high_f_tenths"])
    assert result["low_f"] == _whole(row["low_f_tenths"])
    assert result["storm"] == bool(row["storm_flag"])
    if row["storm_flag"]:
        assert "storm_severity" in result
    else:
        assert "storm_severity" not in result
    expected_prob = float(
        cfg.resolved["weather.precipitation_probability_by_condition"][row["condition_code"]]
    )
    assert result["precipitation_probability"] == expected_prob


def test_weather_get_validation_errors_are_stable(tmp_path):
    conn, _run_id, cfg = _init(tmp_path, 6)
    rng = Rng(6, SPEC_VERSION)
    rng.enter_txn()

    with pytest.raises(EngineError) as too_early:
        get_weather(conn, cfg, rng, zone_id=1, date="2026-09-30", today="2026-10-01")
    with pytest.raises(EngineError) as too_late:
        get_weather(conn, cfg, rng, zone_id=1, date="2026-10-09", today="2026-10-01")
    with pytest.raises(EngineError) as zone_zero:
        get_weather(conn, cfg, rng, zone_id=0, date="2026-10-01", today="2026-10-01")
    with pytest.raises(EngineError) as zone_eleven:
        get_weather(conn, cfg, rng, zone_id=11, date="2026-10-01", today="2026-10-01")
    with pytest.raises(EngineError) as zone_non_int:
        get_weather(conn, cfg, rng, zone_id="1", date="2026-10-01", today="2026-10-01")

    codes = {
        too_early.value.code,
        too_late.value.code,
        zone_zero.value.code,
        zone_eleven.value.code,
        zone_non_int.value.code,
    }
    assert codes == {ErrorCode.INVALID_ARGUMENT}
    rng.exit_txn()


def test_forecast_repeated_query_is_stable_and_consumes_no_sequential_state(tmp_path):
    conn, _run_id, cfg = _init(tmp_path, 21)
    rng = Rng(21, SPEC_VERSION)
    rng.enter_txn()

    before = conn.execute(
        "SELECT counter FROM _rng_state WHERE stream_name = 'weather_process'"
    ).fetchone()["counter"]

    first = get_weather(conn, cfg, rng, zone_id=3, date="2026-10-05", today="2026-10-01")
    second = get_weather(conn, cfg, rng, zone_id=3, date="2026-10-05", today="2026-10-01")

    after = conn.execute(
        "SELECT counter FROM _rng_state WHERE stream_name = 'weather_process'"
    ).fetchone()["counter"]

    assert first == second
    assert before == after
    assert conn.execute(
        "SELECT COUNT(*) c FROM _rng_state WHERE stream_name = 'weather_forecast'"
    ).fetchone()["c"] == 0

    other_target = get_weather(conn, cfg, rng, zone_id=3, date="2026-10-06", today="2026-10-01")
    other_zone = get_weather(conn, cfg, rng, zone_id=4, date="2026-10-05", today="2026-10-01")
    assert other_target != first
    assert other_zone != first
    rng.exit_txn()


def test_forecast_error_is_nondecreasing_in_horizon(tmp_path):
    conn, _run_id, cfg = _init(tmp_path, 11)
    rng = Rng(11, SPEC_VERSION)
    rng.enter_txn()

    max_horizon = cfg.get_int("weather.forecast_max_horizon_days")
    start = date.fromisoformat(cfg.get_string("weather.precompute_start_date"))
    end = date.fromisoformat(cfg.get_string("weather.precompute_end_date"))

    today_candidates = []
    d = start
    while d + timedelta(days=max_horizon) <= end:
        today_candidates.append(d)
        d += timedelta(days=5)

    absolute_errors: dict[int, list[int]] = {h: [] for h in range(1, max_horizon + 1)}
    for today in today_candidates:
        today_s = today.isoformat()
        for zone_id in range(1, 11):
            for horizon in range(1, max_horizon + 1):
                target = (today + timedelta(days=horizon)).isoformat()
                real_row = conn.execute(
                    "SELECT high_f_tenths FROM _zone_weather_daily "
                    "WHERE zone_id = ? AND local_date = ?",
                    (zone_id, target),
                ).fetchone()
                real_high = _whole(real_row["high_f_tenths"])
                forecast = get_weather(
                    conn, cfg, rng, zone_id=zone_id, date=target, today=today_s
                )
                assert forecast["kind"] == "forecast"
                absolute_errors[horizon].append(abs(forecast["high_f"] - real_high))

    means = [
        sum(absolute_errors[h]) / len(absolute_errors[h]) for h in range(1, max_horizon + 1)
    ]
    assert means[0] > 0
    assert all(means[i] <= means[i + 1] for i in range(len(means) - 1))
    rng.exit_txn()


def test_horizon_zero_bypasses_randomness_entirely(tmp_path):
    conn, _run_id, cfg = _init(tmp_path, 5)
    rng = Rng(5, SPEC_VERSION)

    result = get_weather(conn, cfg, rng, zone_id=1, date="2026-10-01", today="2026-10-01")
    assert result["kind"] == "observed"
    assert result["horizon_days"] == 0


def test_zone_weather_daily_is_append_only(tmp_path):
    conn, _run_id, _cfg = _init(tmp_path, 3)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE _zone_weather_daily SET high_f_tenths = 0 "
            "WHERE zone_id = 1 AND local_date = '2026-04-01'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM _zone_weather_daily WHERE zone_id = 1 AND local_date = '2026-04-01'"
        )


def test_weather_get_response_key_set_is_exact(tmp_path):
    conn, _run_id, cfg = _init(tmp_path, 7)
    rng = Rng(7, SPEC_VERSION)
    rng.enter_txn()

    storm_row = conn.execute(
        "SELECT zone_id, local_date FROM _zone_weather_daily WHERE storm_flag = 1 "
        "ORDER BY zone_id, local_date LIMIT 1"
    ).fetchone()
    clear_row = conn.execute(
        "SELECT zone_id, local_date FROM _zone_weather_daily WHERE storm_flag = 0 "
        "ORDER BY zone_id, local_date LIMIT 1"
    ).fetchone()
    assert storm_row is not None
    assert clear_row is not None

    observed_storm = get_weather(
        conn,
        cfg,
        rng,
        zone_id=storm_row["zone_id"],
        date=storm_row["local_date"],
        today=storm_row["local_date"],
    )
    assert observed_storm["storm"] is True
    assert set(observed_storm) == _ALLOWED_KEYS_WITH_STORM

    observed_clear = get_weather(
        conn,
        cfg,
        rng,
        zone_id=clear_row["zone_id"],
        date=clear_row["local_date"],
        today=clear_row["local_date"],
    )
    assert observed_clear["storm"] is False
    assert set(observed_clear) == _ALLOWED_KEYS_WITHOUT_STORM

    found_storm_forecast = False
    found_clear_forecast = False
    today = "2026-04-01"
    today_d = date.fromisoformat(today)
    for zone_id in range(1, 11):
        for offset in range(1, 8):
            target = (today_d + timedelta(days=offset)).isoformat()
            response = get_weather(conn, cfg, rng, zone_id=zone_id, date=target, today=today)
            if response["storm"]:
                assert set(response) == _ALLOWED_KEYS_WITH_STORM
                found_storm_forecast = True
            else:
                assert set(response) == _ALLOWED_KEYS_WITHOUT_STORM
                found_clear_forecast = True

    assert found_storm_forecast
    assert found_clear_forecast
    rng.exit_txn()


def test_band_name_boundaries_belong_to_the_upper_band(tmp_path):
    _conn, _run_id, cfg = _init(tmp_path, 8)
    bands_milli = _bands_milli(cfg)

    first_name, first_lo, _first_hi = bands_milli[0]
    assert _band_name(bands_milli, first_lo) == first_name

    for i in range(len(bands_milli) - 1):
        _lower_name, _lower_lo, lower_hi = bands_milli[i]
        upper_name, upper_lo, _upper_hi = bands_milli[i + 1]
        assert upper_lo == lower_hi
        assert _band_name(bands_milli, lower_hi) == upper_name

    last_name, _last_lo, last_hi = bands_milli[-1]
    assert _band_name(bands_milli, last_hi) == last_name


def test_forecast_scope_distinguishes_query_dates_for_same_target(tmp_path):
    conn, _run_id, cfg = _init(tmp_path, 13)
    scopes: list[str] = []

    def capture(**kw):
        if kw["event_kind"] == "forecast_temp_noise" and kw["draw_index"] == 0:
            scopes.append(kw["scope"])

    rng = Rng(13, SPEC_VERSION, audit_recorder=capture)
    rng.enter_txn()

    target = "2026-10-10"
    first = get_weather(conn, cfg, rng, zone_id=2, date=target, today="2026-10-05")
    second = get_weather(conn, cfg, rng, zone_id=2, date=target, today="2026-10-08")

    assert len(scopes) == 2
    assert scopes[0] != scopes[1]
    assert first["horizon_days"] != second["horizon_days"]
    rng.exit_txn()


def test_get_weather_zone_results_independent_of_query_order_and_omission(tmp_path):
    conn_forward, _rid_f, cfg_f = _init(tmp_path, 30, "forward.db")
    conn_reverse, _rid_r, cfg_r = _init(tmp_path, 30, "reverse.db")
    conn_partial, _rid_p, cfg_p = _init(tmp_path, 30, "partial.db")

    today = "2026-10-01"
    targets = {1: "2026-10-01", 2: "2026-10-04", 3: "2026-10-01", 4: "2026-10-06"}

    def query(conn, cfg, zone_order):
        rng = Rng(30, SPEC_VERSION)
        rng.enter_txn()
        results = {
            zone_id: get_weather(conn, cfg, rng, zone_id=zone_id, date=targets[zone_id], today=today)
            for zone_id in zone_order
        }
        rng.exit_txn()
        return results

    forward = query(conn_forward, cfg_f, [1, 2, 3, 4])
    reverse = query(conn_reverse, cfg_r, [4, 3, 2, 1])
    partial = query(conn_partial, cfg_p, [2])

    assert forward == reverse
    assert partial[2] == forward[2]
