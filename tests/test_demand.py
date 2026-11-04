from __future__ import annotations

import hashlib
import inspect
import json
import re
import sqlite3
from datetime import date

import pytest

from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    geo_artifact_sha256,
    register_conn,
)
from trades_bench import SPEC_VERSION, clock, db, demand, pools
from trades_bench._dump import dump_hash
from trades_bench.config import load_run_config
from trades_bench.enums import EventKind
from trades_bench.errors import EngineError, ErrorCode
from trades_bench.events import Events
from trades_bench.ledger import Ledger
from trades_bench.rng import Rng
from trades_bench.run_init import initialize_run, open_run_db
from trades_bench.txn import Services

ZONE4_ONLY = [0, 0, 0, 1, 0, 0, 0, 0, 0, 0]
SERVICE_DATE = "2026-10-01"
SERVICE_DATE_NEXT = "2026-10-02"


def _legacy_address_pool_ids(conn, *, zone_id, segment, outlying):
    zone_clause = "ac.zone_id IS NULL AND ac.effective_zone_id = ?" if outlying else "ac.zone_id = ?"
    sql = (
        "SELECT ac.id FROM _address_catalog ac WHERE ac.status = 'usable' "
        f"AND {zone_clause} "
        "AND (',' || ac.segment_eligibility || ',') LIKE ('%,' || ? || ',%') "
        "AND NOT EXISTS ("
        "SELECT 1 FROM _locations l WHERE l.address_catalog_id = ac.id AND l.active = 1"
        ") ORDER BY ac.id"
    )
    return [row["id"] for row in conn.execute(sql, (zone_id, segment)).fetchall()]


def _static_address_pool_ids(conn, *, zone_clause, zone_id, segment):
    sql = (
        "SELECT ac.id FROM _address_catalog ac WHERE ac.status = 'usable' "
        f"AND {zone_clause} "
        "AND (',' || ac.segment_eligibility || ',') LIKE ('%,' || ? || ',%') ORDER BY ac.id"
    )
    return [row["id"] for row in conn.execute(sql, (zone_id, segment)).fetchall()]


def _sub_hash(u, label):
    digest = hashlib.sha256(u.to_bytes(8, "big") + label.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _occupied_address_ids(conn):
    return {
        row["address_catalog_id"]
        for row in conn.execute("SELECT address_catalog_id FROM _locations WHERE active = 1")
    }


def _expected_available_address(conn, *, zone_clause, zone_id, segment, u):
    static_ids = _static_address_pool_ids(
        conn, zone_clause=zone_clause, zone_id=zone_id, segment=segment
    )
    if not static_ids:
        return None
    occupied = _occupied_address_ids(conn)
    for probe in range(64):
        candidate = static_ids[_sub_hash(u, f"address_probe:{probe}") % len(static_ids)]
        if candidate not in occupied:
            return candidate
    available = [address_id for address_id in static_ids if address_id not in occupied]
    if not available:
        return None
    return max(available, key=lambda address_id: _sub_hash(u, f"address:{address_id}"))


def _new_caller_zone_clause(outlying):
    return "ac.zone_id IS NULL AND ac.effective_zone_id = ?" if outlying else "ac.zone_id = ?"


def _expected_new_caller_address(conn, *, zone_id, segment, want_outlying, u):
    for outlying in (want_outlying, not want_outlying):
        address_id = _expected_available_address(
            conn, zone_clause=_new_caller_zone_clause(outlying), zone_id=zone_id,
            segment=segment, u=u,
        )
        if address_id is not None:
            return address_id
    raise AssertionError(f"no address for zone {zone_id} segment {segment}")


def _scenario(overrides: dict | None = None):
    keys = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        **(overrides or {}),
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def _zone4_scenario(**overrides):
    merged = {"demand.zone_weights": ZONE4_ONLY}
    merged.update(overrides)
    return _scenario(merged)


def _init(tmp_path, seed, config, name="run.db", *, register=True):
    path = tmp_path / name
    run_id = initialize_run(path, master_seed=seed, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)
    if register:
        register_conn(conn)
    return conn, run_id


def _now_ms(conn) -> int:
    return conn.execute("SELECT current_time_ms FROM _clock").fetchone()[0]


def _generate(conn, config, seed, service_date, now_ms=None):
    rng = Rng(seed, SPEC_VERSION, conn=conn)
    if now_ms is None:
        now_ms = _now_ms(conn)
    db.begin(conn)
    rng.enter_txn()
    try:
        summary = demand.generate_for_date(
            conn, config, rng, service_date=service_date, now_ms=now_ms
        )
    except Exception:
        rng.exit_txn()
        if conn.in_transaction:
            db.rollback(conn)
        raise
    else:
        rng.exit_txn()
        db.commit(conn)
    return summary


def _create_customer(
    conn, *, customer_id, segment, zone_id, now_ms, propensity_micros=None, origin_key=None
):
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, created_from_call_id, "
        "modified_at, origin) VALUES (?, ?, ?, 'active', ?, NULL, ?, 'run')",
        (customer_id, f"Customer {customer_id}", segment, now_ms, now_ms),
    )
    conn.execute(
        "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, postal_code, "
        "latitude, longitude, zone_id, active, created_at, modified_at, effective_zone_id, "
        "address_catalog_id, origin) "
        "VALUES (?, ?, 'Home', '1 Test St', '', 'Tempe', 'AZ', '85282', 33.4, -111.9, ?, 1, ?, ?, ?, NULL, 'run')",
        (customer_id, customer_id, zone_id, now_ms, now_ms, zone_id),
    )
    if propensity_micros is not None:
        conn.execute(
            "INSERT INTO _customer_repeat_state "
            "(customer_id, propensity_micros, last_outcome, competitor_captured_at, updated_at, "
            "origin_key) VALUES (?, ?, NULL, NULL, ?, ?)",
            (
                customer_id, propensity_micros, now_ms,
                origin_key if origin_key is not None else f"t:customer:{customer_id}",
            ),
        )


def _pending_rows(conn, service_date=None):
    if service_date is None:
        return [dict(r) for r in conn.execute("SELECT * FROM _pending_calls ORDER BY id").fetchall()]
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM _pending_calls WHERE service_date = ? ORDER BY id", (service_date,)
        ).fetchall()
    ]


def _content_tuple(row: dict) -> tuple:
    return tuple(
        row[k]
        for k in sorted(row)
        if k not in ("id", "event_id")
    )


def test_initialize_state_writes_expected_rows(tmp_path):
    config = _zone4_scenario()
    conn, run_id = _init(tmp_path, 1, config)

    acceptance = {
        row["urgency_class"]: dict(row)
        for row in conn.execute("SELECT * FROM _acceptance_parameters").fetchall()
    }
    assert set(acceptance) == {"standard", "urgent"}
    base_by_urgency = config.resolved["acceptance.base_probability_by_urgency"]
    decay_by_urgency = config.resolved["acceptance.lead_time_decay_by_urgency"]
    floor_by_urgency = config.resolved["acceptance.floor_probability_by_urgency"]
    for urgency in ("standard", "urgent"):
        assert acceptance[urgency]["base_micros"] == demand._to_micros(base_by_urgency[urgency])
        assert acceptance[urgency]["decay_micros"] == demand._to_micros(decay_by_urgency[urgency])
        assert acceptance[urgency]["floor_micros"] == demand._to_micros(floor_by_urgency[urgency])

    wom_rows = conn.execute("SELECT * FROM _word_of_mouth_state").fetchall()
    assert {row["segment"] for row in wom_rows} == {"residential", "commercial", "apartment"}
    assert all(row["wom_micros"] == 0 for row in wom_rows)

    competitor = conn.execute("SELECT * FROM _competitor_state").fetchone()
    assert competitor["share_micros"] == 0
    assert competitor["capture_count"] == 0
    assert competitor["drift_micros"] == 0

    assert db.get_run(conn, run_id)["status"] == "active"


def test_initialize_state_rejects_catalog_version_mismatch(tmp_path):
    config = _scenario({"demand.service_catalog_version": "9.9.9"})
    with pytest.raises(EngineError) as excinfo:
        initialize_run(
            tmp_path / "run.db", master_seed=1, config=config, artifact_dir=GEO_ARTIFACT_DIR
        )
    assert excinfo.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_initialize_state_rejects_person_pool_version_mismatch(tmp_path):
    config = _scenario({"customers.person_name_pool_version": "wrong-version"})
    with pytest.raises(EngineError) as excinfo:
        initialize_run(
            tmp_path / "run.db", master_seed=1, config=config, artifact_dir=GEO_ARTIFACT_DIR
        )
    assert excinfo.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_initialize_state_rejects_business_pool_version_mismatch(tmp_path):
    config = _scenario({"customers.business_name_pool_version": "wrong-version"})
    with pytest.raises(EngineError) as excinfo:
        initialize_run(
            tmp_path / "run.db", master_seed=1, config=config, artifact_dir=GEO_ARTIFACT_DIR
        )
    assert excinfo.value.code is ErrorCode.CONFIG_VALUE_INVALID


def test_determinism_same_seed_byte_identical(tmp_path):
    config = _zone4_scenario()
    conn_a, rid_a = _init(tmp_path, 42, config, "a.db")
    conn_b, rid_b = _init(tmp_path, 42, config, "b.db")
    conn_c, rid_c = _init(tmp_path, 43, config, "c.db")

    now_a = _now_ms(conn_a)
    now_b = _now_ms(conn_b)
    now_c = _now_ms(conn_c)
    _generate(conn_a, config, 42, SERVICE_DATE, now_ms=now_a)
    _generate(conn_b, config, 42, SERVICE_DATE, now_ms=now_b)
    _generate(conn_c, config, 43, SERVICE_DATE, now_ms=now_c)

    rows_a = _pending_rows(conn_a)
    rows_b = _pending_rows(conn_b)
    rows_c = _pending_rows(conn_c)
    assert rows_a == rows_b
    assert rows_a != rows_c

    events_a = [dict(r) for r in conn_a.execute("SELECT * FROM _events ORDER BY event_id").fetchall()]
    events_b = [dict(r) for r in conn_b.execute("SELECT * FROM _events ORDER BY event_id").fetchall()]
    assert events_a == events_b

    competitor_a = dict(conn_a.execute("SELECT * FROM _competitor_state").fetchone())
    competitor_b = dict(conn_b.execute("SELECT * FROM _competitor_state").fetchone())
    assert competitor_a == competitor_b

    rng_a = [dict(r) for r in conn_a.execute("SELECT * FROM _rng_state ORDER BY stream_name").fetchall()]
    rng_b = [dict(r) for r in conn_b.execute("SELECT * FROM _rng_state ORDER BY stream_name").fetchall()]
    assert rng_a == rng_b

    assert dump_hash(conn_a, rid_a) == dump_hash(conn_b, rid_b)
    assert dump_hash(conn_a, rid_a) != dump_hash(conn_c, rid_c)


def test_draw_key_independence_across_prior_days(tmp_path):
    config = _zone4_scenario()
    conn_a, _rid_a = _init(tmp_path, 7, config, "a.db")
    conn_b, _rid_b = _init(tmp_path, 7, config, "b.db")

    now_a = _now_ms(conn_a)
    now_b = _now_ms(conn_b)
    _generate(conn_a, config, 7, SERVICE_DATE, now_ms=now_a)
    _generate(conn_a, config, 7, SERVICE_DATE_NEXT, now_ms=now_a)
    _generate(conn_b, config, 7, SERVICE_DATE_NEXT, now_ms=now_b)

    next_a = [_content_tuple(r) for r in _pending_rows(conn_a, SERVICE_DATE_NEXT)]
    next_b = [_content_tuple(r) for r in _pending_rows(conn_b, SERVICE_DATE_NEXT)]
    assert next_a == next_b
    assert next_a


def test_weather_get_queries_do_not_perturb_generation(tmp_path):
    from trades_bench.weather import get_weather

    config = _zone4_scenario()
    conn_a, _rid_a = _init(tmp_path, 9, config, "a.db")
    conn_b, _rid_b = _init(tmp_path, 9, config, "b.db")

    rng_b_probe = Rng(9, SPEC_VERSION)
    rng_b_probe.enter_txn()
    for zone_id in range(1, 11):
        get_weather(conn_b, config, rng_b_probe, zone_id=zone_id, date=SERVICE_DATE, today=SERVICE_DATE)
        get_weather(conn_b, config, rng_b_probe, zone_id=zone_id, date=SERVICE_DATE, today=SERVICE_DATE)
    rng_b_probe.exit_txn()

    now_a = _now_ms(conn_a)
    now_b = _now_ms(conn_b)
    _generate(conn_a, config, 9, SERVICE_DATE, now_ms=now_a)
    _generate(conn_b, config, 9, SERVICE_DATE, now_ms=now_b)

    assert _pending_rows(conn_a) == _pending_rows(conn_b)


def test_weather_state_is_unaffected_by_extra_activity_on_one_run(tmp_path):
    from trades_bench.weather import get_weather

    config = _zone4_scenario()
    conn_a, _rid_a = _init(tmp_path, 9, config, "a.db")
    conn_b, _rid_b = _init(tmp_path, 9, config, "b.db")

    def weather_daily(conn):
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM _zone_weather_daily ORDER BY zone_id, local_date"
            ).fetchall()
        ]

    def process_state(conn):
        return dict(conn.execute("SELECT * FROM _weather_process_state").fetchone())

    baseline_daily = weather_daily(conn_b)
    baseline_state = process_state(conn_b)
    counter_a = conn_a.execute(
        "SELECT counter FROM _rng_state WHERE stream_name = 'weather_process'"
    ).fetchone()["counter"]

    forecast_target_date = "2026-10-04"
    cache_count_before = conn_b.execute(
        "SELECT COUNT(*) c FROM _weather_forecast_cache"
    ).fetchone()["c"]
    rng_probe = Rng(9, SPEC_VERSION)
    rng_probe.enter_txn()
    for zone_id in range(1, 11):
        get_weather(
            conn_b, config, rng_probe, zone_id=zone_id, date=forecast_target_date, today=SERVICE_DATE
        )
    rng_probe.exit_txn()
    cache_count_after = conn_b.execute(
        "SELECT COUNT(*) c FROM _weather_forecast_cache"
    ).fetchone()["c"]
    assert cache_count_after == cache_count_before + 10

    now_b = _now_ms(conn_b)
    _generate(conn_b, config, 9, SERVICE_DATE, now_ms=now_b)
    _generate(conn_b, config, 9, SERVICE_DATE_NEXT, now_ms=now_b)

    assert weather_daily(conn_b) == baseline_daily
    assert process_state(conn_b) == baseline_state
    assert weather_daily(conn_a) == weather_daily(conn_b)
    assert process_state(conn_a) == process_state(conn_b)
    counter_b = conn_b.execute(
        "SELECT counter FROM _rng_state WHERE stream_name = 'weather_process'"
    ).fetchone()["counter"]
    assert counter_b == counter_a


def test_zone_independent_storms(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 2, config)

    storm_date = "2026-09-07"
    calm_date = "2026-05-11"
    assert date.fromisoformat(storm_date).weekday() == date.fromisoformat(calm_date).weekday()

    zone4_storm = dict(
        conn.execute(
            "SELECT * FROM _zone_weather_daily WHERE zone_id = 4 AND local_date = ?", (storm_date,)
        ).fetchone()
    )
    zone4_calm = dict(
        conn.execute(
            "SELECT * FROM _zone_weather_daily WHERE zone_id = 4 AND local_date = ?", (calm_date,)
        ).fetchone()
    )
    assert zone4_storm["high_f_tenths"] == zone4_calm["high_f_tenths"]
    assert zone4_storm["low_f_tenths"] == zone4_calm["low_f_tenths"]
    assert zone4_storm["storm_flag"] == zone4_calm["storm_flag"] == 0

    zone1_storm = conn.execute(
        "SELECT storm_flag FROM _zone_weather_daily WHERE zone_id = 1 AND local_date = ?", (storm_date,)
    ).fetchone()["storm_flag"]
    zone1_calm = conn.execute(
        "SELECT storm_flag FROM _zone_weather_daily WHERE zone_id = 1 AND local_date = ?", (calm_date,)
    ).fetchone()["storm_flag"]
    assert zone1_storm == 1
    assert zone1_calm == 0

    m_storm = demand._weather_multipliers_by_name(config, zone4_storm)
    m_calm = demand._weather_multipliers_by_name(config, zone4_calm)
    assert m_storm == m_calm


def test_timestamps_within_call_window_and_after_hours_urgent_share_higher(many_seed_dataset):
    rows = many_seed_dataset["rows"]

    assert rows
    for row in rows:
        local = clock.phoenix_local(row["arrival_at"])
        minutes = local.hour * 60 + local.minute
        assert 7 * 60 <= minutes < 19 * 60
        assert local.second == 0

    after_hours = [r for r in rows if clock.phoenix_local(r["arrival_at"]).hour >= 17]
    office_hours = [r for r in rows if clock.phoenix_local(r["arrival_at"]).hour < 17]
    assert len(after_hours) >= 20
    assert len(office_hours) >= 20
    after_share = sum(r["urgent"] for r in after_hours) / len(after_hours)
    office_share = sum(r["urgent"] for r in office_hours) / len(office_hours)
    assert after_share > office_share


def test_address_pool_eligibility_and_empty_pool_invariant(many_seed_dataset, tmp_path):
    conn = many_seed_dataset["conns"][0]
    rows = [r for r in many_seed_dataset["rows"] if r["run_index"] == 0]
    assert rows
    for row in rows:
        catalog_row = conn.execute(
            "SELECT status, segment_eligibility, zone_id, effective_zone_id FROM _address_catalog "
            "WHERE id = ?",
            (row["address_catalog_id"],),
        ).fetchone()
        assert catalog_row is not None
        assert catalog_row["status"] == "usable"
        tokens = catalog_row["segment_eligibility"].split(",")
        assert row["segment"] in tokens
        assert catalog_row["zone_id"] == row["zone_id"] or (
            catalog_row["zone_id"] is None and catalog_row["effective_zone_id"] == row["zone_id"]
        )
        held = conn.execute(
            "SELECT 1 FROM _locations WHERE address_catalog_id = ? AND active = 1",
            (row["address_catalog_id"],),
        ).fetchone()
        assert held is None

    config = _scenario({"demand.zone_weights": [1, 0, 0, 0, 0, 0, 0, 0, 0, 0]})
    with pytest.raises(EngineError) as excinfo:
        _init(tmp_path, 1, config, name="empty_pool.db")
    assert excinfo.value.code is ErrorCode.INVARIANT_VIOLATED


def test_address_pool_queries_pin_an_explicit_deterministic_order():
    assert "ORDER BY ac.id" in inspect.getsource(demand._AddressPool.member)
    assert "ORDER BY ac.id" in inspect.getsource(demand._AddressPool.available_members)


def test_the_pool_size_member_and_available_list_reproduce_the_list_queries(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    zone_ids = [row["id"] for row in conn.execute("SELECT id FROM _zones ORDER BY id").fetchall()]
    combinations = 0

    for zone_id in zone_ids:
        for segment in sorted(demand._SEGMENTS):
            for outlying in (False, True):
                pool = demand._new_caller_pool(zone_id, segment, outlying=outlying)
                static_ids = _static_address_pool_ids(
                    conn, zone_clause=_new_caller_zone_clause(outlying), zone_id=zone_id,
                    segment=segment,
                )
                size = pool.size(conn)
                assert size == len(static_ids)
                assert [pool.member(conn, index) for index in range(size)] == static_ids
                assert pool.available_members(conn) == _legacy_address_pool_ids(
                    conn, zone_id=zone_id, segment=segment, outlying=outlying
                )
                combinations += 1

    assert combinations == len(zone_ids) * len(demand._SEGMENTS) * 2


def test_a_new_caller_slot_falls_back_to_the_other_outlying_flag_when_its_pool_is_empty(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    conn.execute("UPDATE _address_catalog SET zone_id = NULL WHERE effective_zone_id = 4")
    for segment in sorted(demand._SEGMENTS):
        assert demand._new_caller_pool(4, segment, outlying=False).size(conn) == 0
        assert demand._new_caller_pool(4, segment, outlying=True).size(conn) > 0

    _generate(conn, config, 1, SERVICE_DATE)

    chosen = [
        row["address_catalog_id"]
        for row in _pending_rows(conn, SERVICE_DATE)
        if row["channel"] == "new_caller"
    ]
    outlying_ids = {
        catalog_id
        for segment in sorted(demand._SEGMENTS)
        for catalog_id in _legacy_address_pool_ids(
            conn, zone_id=4, segment=segment, outlying=True
        )
    }
    assert chosen
    assert set(chosen) <= outlying_ids


def test_an_empty_pool_on_both_outlying_flags_fails_the_address_floor_check(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    conn.execute("UPDATE _address_catalog SET status = 'unusable' WHERE effective_zone_id = 4")
    for segment in sorted(demand._SEGMENTS):
        assert demand._new_caller_pool(4, segment, outlying=False).size(conn) == 0
        assert demand._new_caller_pool(4, segment, outlying=True).size(conn) == 0

    with pytest.raises(EngineError) as excinfo:
        demand._validate_address_pool_floors(conn, config)

    assert excinfo.value.code is ErrorCode.INVARIANT_VIOLATED


class _RecordingConnection:

    def __init__(self, conn):
        self._conn = conn
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        return self._conn.execute(sql, params)


def _plan_details(conn, sql, params):
    return [row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def _static_pool_statements(conn):
    recorder = _RecordingConnection(conn)
    for outlying in (False, True):
        demand._new_caller_pool(4, "residential", outlying=outlying).size(recorder)
    demand._new_caller_pool(4, "residential", outlying=False).member(recorder, 0)
    demand._new_location_pool(4, "commercial").size(recorder)
    demand._new_location_pool(4, "commercial").member(recorder, 0)
    return recorder.statements


STATIC_POOL_SCAN_INDEXES = (
    "idx_address_pool_zone_residential",
    "idx_address_pool_eff_residential",
    "idx_address_pool_zone_residential",
    "idx_address_pool_eff_commercial",
    "idx_address_pool_eff_commercial",
)
LOCATION_PROBE_INDEX = "idx_locations_address_catalog"


def test_every_static_pool_statement_plans_its_scan_on_the_partial_index(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    statements = _static_pool_statements(conn)

    assert len(statements) == len(STATIC_POOL_SCAN_INDEXES)
    for (sql, params), scan_index in zip(statements, STATIC_POOL_SCAN_INDEXES, strict=True):
        details = _plan_details(conn, sql, params)
        assert any(
            f"USING INDEX {scan_index}" in detail for detail in details
        ), (scan_index, details)
        assert not any("SCAN _address_catalog" in detail for detail in details), details


def test_the_occupancy_probe_and_the_available_list_use_the_location_index(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    recorder = _RecordingConnection(conn)
    demand._address_occupied(recorder, 1)
    demand._new_caller_pool(4, "residential", outlying=False).available_members(recorder)
    (probe_sql, probe_params), (available_sql, available_params) = recorder.statements

    probe_details = _plan_details(conn, probe_sql, probe_params)
    assert any(f"USING INDEX {LOCATION_PROBE_INDEX}" in d for d in probe_details), probe_details
    available_details = _plan_details(conn, available_sql, available_params)
    assert any(
        "USING INDEX idx_address_pool_zone_residential" in d for d in available_details
    ), available_details
    assert any(
        f"USING INDEX {LOCATION_PROBE_INDEX}" in d for d in available_details
    ), available_details
    assert not any("SCAN l" in d for d in available_details), available_details


def test_a_bound_segment_parameter_would_lose_the_partial_index_plan(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    literal_sql, literal_params = _static_pool_statements(conn)[0]
    bound_sql = literal_sql.replace("'%,residential,%'", "('%,' || ? || ',%')")
    bound_params = (*literal_params, "residential")

    assert bound_sql != literal_sql
    assert any(
        "USING INDEX idx_address_pool_zone_residential" in detail
        for detail in _plan_details(conn, literal_sql, literal_params)
    )
    assert not any(
        "idx_address_pool_zone_residential" in detail
        for detail in _plan_details(conn, bound_sql, bound_params)
    )


def test_the_pool_queries_reject_a_segment_outside_the_frozen_set(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)

    with pytest.raises(EngineError) as excinfo:
        demand._new_caller_pool(4, "industrial", outlying=False).size(conn)

    assert excinfo.value.code is ErrorCode.INVARIANT_VIOLATED


def test_identity_pools_and_phone_format(many_seed_dataset):
    rows = many_seed_dataset["rows"]
    config = many_seed_dataset["config"]
    prefixes = config.get_string_list("customers.phone_prefix_pool")
    prefix_areas = {p[:3] for p in prefixes}
    phone_re = re.compile(r"^(\d{3})-555-01\d{2}$")

    first_names = set(pools.FIRST_NAMES)
    last_names = set(pools.LAST_NAMES)

    saw_residential = False
    saw_business = False
    for row in rows:
        match = phone_re.match(row["caller_phone"])
        assert match, row["caller_phone"]
        assert match.group(1) in prefix_areas

        if row["segment"] == "residential":
            first, last = row["caller_name"].split(" ", 1)
            assert first in first_names
            assert last in last_names
            saw_residential = True
        else:
            saw_business = True
            suffixes = pools._COMMERCIAL_SUFFIXES if row["segment"] == "commercial" else pools._APARTMENT_SUFFIXES
            assert any(row["caller_name"].endswith(suffix) for suffix in suffixes)

    assert saw_residential
    assert saw_business


def test_call_invocation_events_scheduled_correctly(many_seed_dataset):
    conn = many_seed_dataset["conns"][0]
    rows = [r for r in many_seed_dataset["rows"] if r["run_index"] == 0]
    assert rows
    for row in rows:
        event = conn.execute(
            "SELECT * FROM _events WHERE event_id = ?", (row["event_id"],)
        ).fetchone()
        assert event is not None
        assert EventKind(event["kind"]) is EventKind.CALL_INVOCATION
        assert event["priority"] == 910
        assert event["scheduled_at_ms"] == row["arrival_at"]
        assert event["entity_type"] == "pending_call"
        assert event["entity_id"] == row["id"]
        assert event["status"] == "pending"

    invocation_events = conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = 'call_invocation'"
    ).fetchone()["c"]
    assert invocation_events == len(rows)


def test_demand_generation_handler_validates_and_delegates(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    now_ms = _now_ms(conn)
    rng = Rng(1, SPEC_VERSION, conn=conn)
    services = Services(conn=conn, rng=rng, ledger=Ledger(conn), invocations=None)
    events = Events(conn, rng)

    db.begin(conn)
    rng.enter_txn()
    event_id = events.schedule(
        EventKind.DEMAND_GENERATION, now_ms, payload={"service_date": SERVICE_DATE}
    )
    rng.exit_txn()
    db.commit(conn)

    handler = demand.demand_generation_handler(config)
    response = json.loads(events.apply(event_id, handler, services=services))
    assert response["ok"] is True
    assert conn.execute("SELECT COUNT(*) c FROM _pending_calls").fetchone()["c"] > 0
    assert conn.execute(
        "SELECT status FROM _events WHERE event_id = ?", (event_id,)
    ).fetchone()["status"] == "applied"


def test_demand_generation_handler_rejects_non_canonical_service_date(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    now_ms = _now_ms(conn)
    rng = Rng(1, SPEC_VERSION, conn=conn)
    services = Services(conn=conn, rng=rng, ledger=Ledger(conn), invocations=None)
    events = Events(conn, rng)

    db.begin(conn)
    rng.enter_txn()
    event_id = events.schedule(
        EventKind.DEMAND_GENERATION, now_ms, payload={"service_date": "2026-1-1"}
    )
    rng.exit_txn()
    db.commit(conn)

    handler = demand.demand_generation_handler(config)
    response = json.loads(events.apply(event_id, handler, services=services))
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_argument"
    assert conn.execute(
        "SELECT status FROM _events WHERE event_id = ?", (event_id,)
    ).fetchone()["status"] == "pending"
    assert conn.execute("SELECT COUNT(*) c FROM _pending_calls").fetchone()["c"] == 0


def test_update_word_of_mouth_positive_negative_asymmetry(tmp_path):
    from decimal import Decimal

    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    now_ms = _now_ms(conn)

    demand.update_word_of_mouth(
        conn, config, segment="residential", satisfaction_delta=Decimal("1"), now_ms=now_ms
    )
    positive_wom = conn.execute(
        "SELECT wom_micros FROM _word_of_mouth_state WHERE segment = 'residential'"
    ).fetchone()["wom_micros"]

    conn.execute("UPDATE _word_of_mouth_state SET wom_micros = 0 WHERE segment = 'commercial'")
    demand.update_word_of_mouth(
        conn, config, segment="commercial", satisfaction_delta=Decimal("-1"), now_ms=now_ms
    )
    negative_wom = conn.execute(
        "SELECT wom_micros FROM _word_of_mouth_state WHERE segment = 'commercial'"
    ).fetchone()["wom_micros"]

    assert positive_wom > 0
    assert negative_wom < 0
    assert abs(negative_wom) > abs(positive_wom)


@pytest.fixture(scope="module")
def many_seed_dataset(tmp_path_factory):
    n_seeds = 40
    config = _zone4_scenario(**{"demand.level_start": 30, "demand.existing_customer_call_share": 0})
    rows = []
    conns = []
    for run_index, seed in enumerate(range(1, n_seeds + 1)):
        tmp_path = tmp_path_factory.mktemp(f"many_seed_{seed}")
        conn, _run_id = _init(tmp_path, seed, config, register=False)
        now_ms = _now_ms(conn)
        _generate(conn, config, seed, SERVICE_DATE, now_ms=now_ms)
        for row in _pending_rows(conn):
            row["run_index"] = run_index
            row["seed"] = seed
            rows.append(row)
        conns.append(conn)
    yield {"rows": rows, "conns": conns, "config": config}
    for conn in conns:
        try:
            conn.close()
        except Exception:
            pass


def _new_caller_rows_by_origin(conn, service_date):
    return {
        row["origin_key"]: row
        for row in _pending_rows(conn, service_date)
        if row["channel"] == "new_caller"
    }


SLOT_ATTRIBUTES = (
    "zone_id", "arrival_at", "segment", "requested_category_id", "urgent",
    "caller_name", "caller_phone", "prospect_id", "slot_index",
)


def test_registering_a_location_moves_only_the_slots_whose_first_available_address_it_took(
    tmp_path,
):
    config = _zone4_scenario(**{"demand.level_start": 30, "demand.existing_customer_call_share": 0})
    clean, _run_id = _init(tmp_path, 1, config, name="clean.db")
    _generate(clean, config, 1, SERVICE_DATE)
    clean_rows = _new_caller_rows_by_origin(clean, SERVICE_DATE)
    assert len(clean_rows) >= 10
    taken_address = clean_rows[min(clean_rows)]["address_catalog_id"]

    occupied, _run_id = _init(tmp_path, 1, config, name="occupied.db")
    now_ms = _now_ms(occupied)
    db.begin(occupied)
    _create_customer(
        occupied, customer_id=7001, segment="residential", zone_id=4, now_ms=now_ms,
        propensity_micros=0,
    )
    occupied.execute(
        "UPDATE _locations SET address_catalog_id = ? WHERE id = 7001", (taken_address,)
    )
    db.commit(occupied)
    _generate(occupied, config, 1, SERVICE_DATE)
    occupied_rows = _new_caller_rows_by_origin(occupied, SERVICE_DATE)

    assert occupied_rows.keys() == clean_rows.keys()
    moved = {
        origin_key
        for origin_key, row in clean_rows.items()
        if row["address_catalog_id"] != occupied_rows[origin_key]["address_catalog_id"]
    }
    assert moved == {
        origin_key
        for origin_key, row in clean_rows.items()
        if row["address_catalog_id"] == taken_address
    }
    assert taken_address not in {row["address_catalog_id"] for row in occupied_rows.values()}
    for origin_key, row in clean_rows.items():
        assert {a: row[a] for a in SLOT_ATTRIBUTES} == {
            a: occupied_rows[origin_key][a] for a in SLOT_ATTRIBUTES
        }




def test_exhausted_probes_fall_back_to_rendezvous_over_the_available_addresses(
    tmp_path, monkeypatch
):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    pool = demand._new_caller_pool(4, "residential", outlying=False)
    u = 0x0123456789ABCDEF
    monkeypatch.setattr(demand, "_ADDRESS_PROBE_LIMIT", 0)
    available = pool.available_members(conn)

    expected = max(available, key=lambda address_id: _sub_hash(u, f"address:{address_id}"))
    assert demand._pick_available_address(conn, pool, u) == expected

    now_ms = _now_ms(conn)
    db.begin(conn)
    _create_customer(
        conn, customer_id=7002, segment="residential", zone_id=4, now_ms=now_ms,
        propensity_micros=0,
    )
    conn.execute("UPDATE _locations SET address_catalog_id = ? WHERE id = 7002", (expected,))
    db.commit(conn)
    runner_up = max(
        (address_id for address_id in available if address_id != expected),
        key=lambda address_id: _sub_hash(u, f"address:{address_id}"),
    )
    assert demand._pick_available_address(conn, pool, u) == runner_up






def _appointment_history(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE _appointments (scheduled_start INTEGER, status TEXT, origin TEXT)")
    for local_date, status, count, *origin in rows:
        d = date.fromisoformat(local_date)
        start = clock.phoenix_instant(d.year, d.month, d.day, 9, 0)
        conn.executemany(
            "INSERT INTO _appointments VALUES (?, ?, ?)",
            [(start, status, origin[0] if origin else "run")] * count,
        )
    return conn


DEFAULT_CONFIG = load_run_config()


def _level(rows, service_date):
    conn = _appointment_history(rows)
    try:
        return demand.demand_level_micros(conn, DEFAULT_CONFIG, service_date)
    finally:
        conn.close()


def test_demand_level_starts_at_level_start():
    assert _level([], "2026-10-01") == 18_000_000
    assert _level([], "2026-10-05") == 18_000_000


def test_demand_level_gains_per_completed_visit_on_the_previous_date():
    rows = [("2026-10-01", "completed", 10), ("2026-10-02", "completed", 5)]
    assert _level(rows, "2026-10-02") == 19_000_000
    assert _level(rows, "2026-10-03") == 19_500_000


def test_demand_level_loses_per_no_show_or_failed_visit_and_ignores_others():
    rows = [
        ("2026-10-01", "no_show", 1),
        ("2026-10-01", "failed", 1),
        ("2026-10-01", "cancelled", 4),
        ("2026-10-01", "scheduled", 4),
        ("2026-10-01", "no_show", 3, "warm_start"),
    ]
    assert _level(rows, "2026-10-02") == 12_000_000


def test_demand_level_clamps_at_level_max():
    assert _level([("2026-10-01", "completed", 200)], "2026-10-02") == 30_000_000


def test_demand_level_floors_at_zero_and_recovers_only_through_completions():
    rows = [("2026-10-01", "no_show", 6)]
    assert _level(rows, "2026-10-02") == 0
    assert _level(rows, "2026-10-04") == 0
    rows.append(("2026-10-03", "completed", 1))
    assert _level(rows, "2026-10-04") == 100_000


@pytest.mark.parametrize(
    ("level", "expected"),
    [(16_000_000, 11_200_000), (12_000_000, 10_000_000), (10_000_000, 10_000_000),
     (9_000_000, 6_300_000), (0, 0)],
)
def test_weekend_call_target_scales_the_level_with_a_floor_from_ten(level, expected):
    assert date.fromisoformat("2026-10-03").weekday() == 5
    assert demand.daily_call_target_micros(DEFAULT_CONFIG, level, "2026-10-03") == expected
    assert demand.daily_call_target_micros(DEFAULT_CONFIG, level, "2026-10-04") == expected


def test_weekday_call_target_is_the_level():
    for service_date in ("2026-10-01", "2026-10-02", "2026-10-05"):
        assert demand.daily_call_target_micros(DEFAULT_CONFIG, 13_700_000, service_date) == 13_700_000


def _mixes(seed, target_micros, dates):
    rng = Rng(seed, SPEC_VERSION)
    rng.enter_txn()
    mixes = [
        demand.daily_call_mix(DEFAULT_CONFIG, rng, service_date=d, target_micros=target_micros)
        for d in dates
    ]
    rng.exit_txn()
    return mixes


DATES = [date.fromordinal(date(2026, 10, 1).toordinal() + k).isoformat() for k in range(400)]


def test_fractional_target_adds_one_call_with_keyed_probability_of_the_fraction():
    whole = [total for total, _i, _s in _mixes(3, 12_000_000, DATES)]
    assert set(whole) == {12}
    half = [total for total, _i, _s in _mixes(3, 12_500_000, DATES)]
    assert set(half) == {12, 13}
    assert 150 <= half.count(13) <= 250
    assert half == [total for total, _i, _s in _mixes(3, 12_500_000, DATES)]


def test_invalid_and_install_counts_are_seed_keyed_and_capped_by_the_total():
    high = _mixes(5, 30_000_000, DATES)
    assert high == _mixes(5, 30_000_000, DATES)
    assert [(i, s) for _t, i, s in high] == [(i, s) for _t, i, s in _mixes(5, 20_000_000, DATES)]
    assert {i for _t, i, _s in high} == {2, 3, 4, 5}
    assert {s for _t, _i, s in high} == {0, 1, 2}
    for (_t, invalid, install), (total, small_invalid, small_install) in zip(
        high, _mixes(5, 3_000_000, DATES), strict=True
    ):
        assert total == 3
        assert small_invalid == min(invalid, 3)
        assert small_install == min(install, 3 - small_invalid)
    assert _mixes(5, 0, DATES[:5]) == [(0, 0, 0)] * 5


def test_a_lower_level_sees_a_subset_of_the_same_callers(tmp_path):
    rows_by_level = {}
    for level in (20, 12):
        config = _zone4_scenario(**{"demand.level_start": level})
        conn, _run_id = _init(tmp_path, 4, config, name=f"level_{level}.db")
        summary = _generate(conn, config, 4, SERVICE_DATE)
        assert summary["total_calls"] == level
        rows_by_level[level] = {row["origin_key"]: _content_tuple(row) for row in _pending_rows(conn)}
        assert sorted(row["slot_index"] for row in _pending_rows(conn)) == list(range(level))
    high, low = rows_by_level[20], rows_by_level[12]
    assert low.keys() < high.keys()
    for origin_key, content in low.items():
        assert high[origin_key] == content


def test_slot_kinds_set_categories_and_installs_are_never_urgent(tmp_path):
    config = _zone4_scenario(**{
        "demand.level_start": 30,
        "demand.office_hours_urgent_probability": 1,
        "demand.after_hours_urgent_probability": 1,
    })
    unsupported = set(config.get_string_list("demand.unsupported_categories"))
    installs = 0
    for seed in range(1, 6):
        conn, _run_id = _init(tmp_path, seed, config, name=f"kinds_{seed}.db", register=False)
        summary = _generate(conn, config, seed, SERVICE_DATE)
        names = {
            row["id"]: row["name"]
            for row in conn.execute("SELECT id, name FROM _service_catalog").fetchall()
        }
        invalid, install = summary["invalid_calls"], summary["install_calls"]
        installs += install
        for row in _pending_rows(conn):
            name = names[row["requested_category_id"]]
            if row["slot_index"] < invalid:
                assert name in unsupported
                assert row["urgent"] == 1
            elif row["slot_index"] < invalid + install:
                assert name == "HVAC installation"
                assert row["urgent"] == 0
            else:
                assert name in {"HVAC service", "Plumbing", "Electrical"}
                assert row["urgent"] == 1
        conn.close()
    assert installs > 0


def test_existing_customer_slots_never_pick_a_lost_customer(tmp_path):
    config = _zone4_scenario(**{"demand.existing_customer_call_share": 1})
    conn, _run_id = _init(tmp_path, 1, config)
    now_ms = _now_ms(conn)
    db.begin(conn)
    _create_customer(
        conn, customer_id=901, segment="residential", zone_id=4, now_ms=now_ms,
        propensity_micros=0,
    )
    _create_customer(
        conn, customer_id=902, segment="commercial", zone_id=4, now_ms=now_ms,
        propensity_micros=1_500_000,
    )
    db.commit(conn)
    summary = _generate(conn, config, 1, SERVICE_DATE)
    existing = [row for row in _pending_rows(conn) if row["channel"] == "existing_customer"]
    assert summary["repeat_calls"] == summary["service_calls"] == len(existing) > 0
    assert {row["customer_id"] for row in existing} == {902}

    lost_only, _run_id = _init(tmp_path, 1, config, name="lost.db")
    db.begin(lost_only)
    _create_customer(
        lost_only, customer_id=901, segment="residential", zone_id=4, now_ms=now_ms,
        propensity_micros=0,
    )
    db.commit(lost_only)
    _generate(lost_only, config, 1, SERVICE_DATE)
    assert {row["channel"] for row in _pending_rows(lost_only)} == {"new_caller"}
