from __future__ import annotations

import inspect
import json
import re
from datetime import date

import pytest

from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    geo_artifact_sha256,
    register_conn,
)
from csr_bench import SPEC_VERSION, clock, db, demand, pools
from csr_bench._dump import dump_hash
from csr_bench.config import load_run_config, probability_threshold
from csr_bench.enums import EventKind
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.events import Events
from csr_bench.ledger import Ledger
from csr_bench.rng import Rng, chance
from csr_bench.run_init import initialize_run, open_run_db
from csr_bench.txn import Services

ZONE4_ONLY = [0, 0, 0, 1, 0, 0, 0, 0, 0, 0]
SERVICE_DATE = "2026-10-01"
SERVICE_DATE_NEXT = "2026-10-02"


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


def _create_customer(conn, *, customer_id, segment, zone_id, now_ms, propensity_micros=None):
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
            "(customer_id, propensity_micros, last_outcome, competitor_captured_at, updated_at) "
            "VALUES (?, ?, NULL, NULL, ?)",
            (customer_id, propensity_micros, now_ms),
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
    config = _zone4_scenario(**{"demand.base_new_caller_rate_per_day": 15})
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
    config = _zone4_scenario(**{"demand.base_new_caller_rate_per_day": 15})
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
    from csr_bench.weather import get_weather

    config = _zone4_scenario(**{"demand.base_new_caller_rate_per_day": 15})
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
    from csr_bench.weather import get_weather

    config = _zone4_scenario(**{"demand.base_new_caller_rate_per_day": 15})
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


def test_ordinal_discipline_matches_hand_recomputation(tmp_path):
    config = _zone4_scenario(**{"demand.base_new_caller_rate_per_day": 15})
    conn, _run_id = _init(tmp_path, 1, config)
    now_ms = _now_ms(conn)
    _generate(conn, config, 1, SERVICE_DATE, now_ms=now_ms)

    row = conn.execute(
        "SELECT * FROM _pending_calls WHERE channel = 'new_caller' AND prospect_id IS NULL "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    zone_id = row["zone_id"]
    slot_index = row["slot_index"]
    scope = f"{SERVICE_DATE}:{slot_index}"

    rng = Rng(1, SPEC_VERSION)
    rng.enter_txn()

    def draw(ordinal):
        return rng.draw("demand", "zone", zone_id, scope, "new_call_attributes", ordinal)

    segment = demand._pick_segment(config, draw(0))
    catalog_rows = list(
        conn.execute(
            "SELECT id, name, supported_job_type_id FROM _service_catalog WHERE active = 1 ORDER BY id"
        ).fetchall()
    )
    weather_row = conn.execute(
        "SELECT high_f_tenths, low_f_tenths, storm_flag, storm_severity_milli "
        "FROM _zone_weather_daily WHERE zone_id = ? AND local_date = ?",
        (zone_id, SERVICE_DATE),
    ).fetchone()
    m_by_name = demand._weather_multipliers_by_name(config, weather_row)
    dist = demand._category_distribution(config, catalog_rows, segment, m_by_name)
    category_id, category_name = demand._weighted_choice(
        [((cid, name), demand._to_micros(mass)) for cid, name, mass in dist], draw(1)
    )
    want_outlying = chance(draw(2), config.get_prob_threshold("demand.outlying_address_share"))
    pool_ids = demand._address_pool_ids(conn, zone_id=zone_id, segment=segment, outlying=want_outlying)
    if not pool_ids:
        pool_ids = demand._address_pool_ids(
            conn, zone_id=zone_id, segment=segment, outlying=not want_outlying
        )
    u3 = draw(3)
    address_catalog_id = pool_ids[u3 % len(pool_ids)]
    hour = demand._pick_arrival_hour(config, draw(8))
    minute = draw(9) % 60
    caller_name, caller_phone = pools.compose_identity(
        draw(4), segment, config.get_string_list("customers.phone_prefix_pool")
    )
    m_category_micros = m_by_name.get(category_name, demand._to_micros(1))
    p_urgent = demand._urgency_probability(config, hour, m_category_micros)
    urgent = chance(draw(5), probability_threshold(p_urgent))
    rng.exit_txn()

    assert row["segment"] == segment
    assert row["requested_category_id"] == category_id
    assert row["address_catalog_id"] == address_catalog_id
    assert row["caller_name"] == caller_name
    assert row["caller_phone"] == caller_phone
    assert bool(row["urgent"]) == urgent
    y, m, d = date.fromisoformat(SERVICE_DATE).year, date.fromisoformat(SERVICE_DATE).month, date.fromisoformat(SERVICE_DATE).day
    assert row["arrival_at"] == clock.phoenix_instant(y, m, d, hour, minute)


def test_ordinal_discipline_matches_hand_recomputation_for_every_slot(tmp_path):
    config = _zone4_scenario(**{"demand.base_new_caller_rate_per_day": 15})
    conn, _run_id = _init(tmp_path, 1, config)
    now_ms = _now_ms(conn)
    _generate(conn, config, 1, SERVICE_DATE, now_ms=now_ms)

    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM _pending_calls WHERE channel = 'new_caller' ORDER BY slot_index"
        ).fetchall()
    ]
    assert len(rows) >= 10

    catalog_rows = list(
        conn.execute(
            "SELECT id, name, supported_job_type_id FROM _service_catalog WHERE active = 1 ORDER BY id"
        ).fetchall()
    )
    phone_prefixes = config.get_string_list("customers.phone_prefix_pool")
    m_by_name_by_zone: dict[int, dict[str, int]] = {}

    rng = Rng(1, SPEC_VERSION)
    rng.enter_txn()
    for row in rows:
        zone_id = row["zone_id"]
        slot_index = row["slot_index"]
        scope = f"{SERVICE_DATE}:{slot_index}"

        def draw(ordinal, zone_id=zone_id, scope=scope):
            return rng.draw("demand", "zone", zone_id, scope, "new_call_attributes", ordinal)

        if zone_id not in m_by_name_by_zone:
            weather_row = conn.execute(
                "SELECT high_f_tenths, low_f_tenths, storm_flag, storm_severity_milli "
                "FROM _zone_weather_daily WHERE zone_id = ? AND local_date = ?",
                (zone_id, SERVICE_DATE),
            ).fetchone()
            m_by_name_by_zone[zone_id] = demand._weather_multipliers_by_name(config, weather_row)
        m_by_name = m_by_name_by_zone[zone_id]

        segment = demand._pick_segment(config, draw(0))
        dist = demand._category_distribution(config, catalog_rows, segment, m_by_name)
        category_id, category_name = demand._weighted_choice(
            [((cid, name), demand._to_micros(mass)) for cid, name, mass in dist], draw(1)
        )
        want_outlying = chance(draw(2), config.get_prob_threshold("demand.outlying_address_share"))
        pool_ids = demand._address_pool_ids(conn, zone_id=zone_id, segment=segment, outlying=want_outlying)
        if not pool_ids:
            pool_ids = demand._address_pool_ids(
                conn, zone_id=zone_id, segment=segment, outlying=not want_outlying
            )
        u3 = draw(3)
        address_catalog_id = pool_ids[u3 % len(pool_ids)]

        hour = demand._pick_arrival_hour(config, draw(8))
        minute = draw(9) % 60
        arrival_at = clock.phoenix_instant(*demand._ymd(SERVICE_DATE), hour, minute)

        returning_flag = chance(
            draw(6), config.get_prob_threshold("demand.returning_prospect_probability")
        )
        prospect_ids = []
        if returning_flag:
            prospect_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM _prospect_state WHERE home_zone_id = ? AND segment = ? "
                    "ORDER BY id",
                    (zone_id, segment),
                ).fetchall()
            ]
        if returning_flag and prospect_ids:
            u7 = draw(7)
            prospect_row = conn.execute(
                "SELECT id, caller_name, caller_phone FROM _prospect_state WHERE id = ?",
                (prospect_ids[u7 % len(prospect_ids)],),
            ).fetchone()
            caller_name = prospect_row["caller_name"]
            caller_phone = prospect_row["caller_phone"]
            expected_prospect_id = prospect_row["id"]
        else:
            caller_name, caller_phone = pools.compose_identity(draw(4), segment, phone_prefixes)
            expected_prospect_id = None

        m_category_micros = m_by_name.get(category_name, demand._to_micros(1))
        p_urgent = demand._urgency_probability(config, hour, m_category_micros)
        urgent = chance(draw(5), probability_threshold(p_urgent))

        assert row["segment"] == segment, slot_index
        assert row["requested_category_id"] == category_id, slot_index
        assert row["address_catalog_id"] == address_catalog_id, slot_index
        assert row["arrival_at"] == arrival_at, slot_index
        assert row["prospect_id"] == expected_prospect_id, slot_index
        assert row["caller_name"] == caller_name, slot_index
        assert row["caller_phone"] == caller_phone, slot_index
        assert bool(row["urgent"]) == urgent, slot_index
    rng.exit_txn()


def test_zone_independent_storms(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)

    storm_date = "2026-09-26"
    calm_date = "2026-05-02"
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

    catalog_rows = list(
        conn.execute(
            "SELECT id, name, supported_job_type_id FROM _service_catalog WHERE active = 1 ORDER BY id"
        ).fetchall()
    )
    m_storm = demand._weather_multipliers_by_name(config, zone4_storm)
    m_calm = demand._weather_multipliers_by_name(config, zone4_calm)
    assert m_storm == m_calm
    vol_storm = demand._volume_weather_multiplier_micros(config, catalog_rows, zone4_storm, m_storm)
    vol_calm = demand._volume_weather_multiplier_micros(config, catalog_rows, zone4_calm, m_calm)
    assert vol_storm == vol_calm

    weekday = date.fromisoformat(storm_date).weekday()
    dow_mult = config.resolved["demand.day_of_week_multipliers"][weekday]
    base_rate = config.get_decimal("demand.base_new_caller_rate_per_day")
    zone_weight = config.resolved["demand.zone_weights"][3]
    m_rep = demand._reputation_multiplier_micros(conn, config, _now_ms(conn))
    m_wom = demand._word_of_mouth_multiplier_micros(conn, config)
    m_comp = demand._competitor_multiplier_micros(conn, config)
    lambda_storm = demand._to_micros(
        base_rate
        * zone_weight
        * demand._from_micros(vol_storm)
        * dow_mult
        * demand._from_micros(m_rep)
        * demand._from_micros(m_wom)
        * demand._from_micros(m_comp)
    )
    lambda_calm = demand._to_micros(
        base_rate
        * zone_weight
        * demand._from_micros(vol_calm)
        * dow_mult
        * demand._from_micros(m_rep)
        * demand._from_micros(m_wom)
        * demand._from_micros(m_comp)
    )
    assert lambda_storm == lambda_calm


def test_timestamps_within_call_window_and_after_hours_urgent_share_higher(many_seed_dataset):
    rows = many_seed_dataset["rows"]
    from csr_bench import clock

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


def test_poisson_matches_lambda_direction_and_edge_cases():
    zero = demand.poisson(0, 12345)
    assert zero == 0

    rng = Rng(1, SPEC_VERSION)
    rng.enter_txn()
    us = [
        rng.draw("demand", "zone", 1, f"poisson-probe-{i}", "new_caller_count", 0)
        for i in range(300)
    ]
    rng.exit_txn()
    lambda_micros = demand._to_micros(2.7)
    ks = [demand.poisson(lambda_micros, u) for u in us]
    mean_k = sum(ks) / len(ks)
    assert 2.2 <= mean_k <= 3.2

    u_fixed = us[0]
    assert demand.poisson(lambda_micros, u_fixed) == demand.poisson(lambda_micros, u_fixed)


def test_poisson_is_monotone_in_lambda_for_a_fixed_draw():
    rng = Rng(1, SPEC_VERSION)
    rng.enter_txn()
    u = rng.draw("demand", "zone", 1, "poisson-monotone-probe", "new_caller_count", 0)
    rng.exit_txn()
    small = demand._to_micros(1)
    large = demand._to_micros(5)
    assert demand.poisson(small, u) <= demand.poisson(large, u)


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

    config = _scenario(
        {"demand.zone_weights": [1, 0, 0, 0, 0, 0, 0, 0, 0, 0], "demand.base_new_caller_rate_per_day": 200}
    )
    with pytest.raises(EngineError) as excinfo:
        _init(tmp_path, 1, config, name="empty_pool.db")
    assert excinfo.value.code is ErrorCode.INVARIANT_VIOLATED


def test_address_pool_queries_pin_an_explicit_deterministic_order():
    assert "ORDER BY ac.id" in inspect.getsource(demand._address_pool_ids)
    assert "ORDER BY ac.id" in inspect.getsource(demand._new_location_pool_ids)


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


def test_repeat_channel_hit_and_location_branches(tmp_path):
    config = _zone4_scenario(**{"demand.repeat_call_daily_base_probability": 0.5})
    conn, _run_id = _init(tmp_path, 1, config)
    now_ms = _now_ms(conn)
    db.begin(conn)
    _create_customer(conn, customer_id=999, segment="residential", zone_id=4, now_ms=now_ms, propensity_micros=3_000_000)
    db.commit(conn)

    summary = _generate(conn, config, 1, SERVICE_DATE, now_ms=now_ms)
    assert summary["repeat_calls"] == 1

    row = conn.execute("SELECT * FROM _pending_calls WHERE customer_id = 999").fetchone()
    assert row is not None
    assert row["channel"] == "existing_customer"
    assert row["location_id"] == 999
    assert row["address_catalog_id"] is None
    assert row["prospect_id"] is None

    rng = Rng(1, SPEC_VERSION)
    rng.enter_txn()
    u_hit = rng.draw("demand", "customer", 999, SERVICE_DATE, "repeat_call", 0)
    weather_row = conn.execute(
        "SELECT high_f_tenths, low_f_tenths, storm_flag, storm_severity_milli "
        "FROM _zone_weather_daily WHERE zone_id = 4 AND local_date = ?",
        (SERVICE_DATE,),
    ).fetchone()
    catalog_rows = list(
        conn.execute(
            "SELECT id, name, supported_job_type_id FROM _service_catalog WHERE active = 1 ORDER BY id"
        ).fetchall()
    )
    m_by_name = demand._weather_multipliers_by_name(config, weather_row)
    m_vol = demand._volume_weather_multiplier_micros(config, catalog_rows, weather_row, m_by_name)
    weekday = date.fromisoformat(SERVICE_DATE).weekday()
    dow_mult = config.resolved["demand.day_of_week_multipliers"][weekday]
    base_prob = config.get_decimal("demand.repeat_call_daily_base_probability")
    propensity = demand._from_micros(3_000_000)
    p_call = min(max(propensity * base_prob * demand._from_micros(m_vol) * dow_mult, 0), 1)
    assert chance(u_hit, probability_threshold(p_call)) is True
    rng.exit_txn()


def test_repeat_channel_new_location_branch(tmp_path):
    config = _zone4_scenario(
        **{
            "demand.repeat_call_daily_base_probability": 0.5,
            "demand.existing_customer_new_location_probability": 1.0,
        }
    )
    conn, _run_id = _init(tmp_path, 1, config)
    now_ms = _now_ms(conn)
    db.begin(conn)
    _create_customer(conn, customer_id=999, segment="residential", zone_id=4, now_ms=now_ms, propensity_micros=3_000_000)
    db.commit(conn)

    summary = _generate(conn, config, 1, SERVICE_DATE, now_ms=now_ms)
    assert summary["repeat_calls"] == 1
    row = conn.execute("SELECT * FROM _pending_calls WHERE customer_id = 999").fetchone()
    assert row["location_id"] is None
    assert row["address_catalog_id"] is not None


def test_repeat_channel_missing_state_raises_invariant_violated(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    now_ms = _now_ms(conn)
    db.begin(conn)
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, created_from_call_id, "
        "modified_at, origin) VALUES (999, 'Test', 'residential', 'active', ?, NULL, ?, 'run')",
        (now_ms, now_ms),
    )
    conn.execute(
        "INSERT INTO _locations (id, customer_id, name, street, unit, city, state, postal_code, "
        "latitude, longitude, zone_id, active, created_at, modified_at, effective_zone_id, "
        "address_catalog_id, origin) "
        "VALUES (999, 999, 'Home', '1 Test St', '', 'Tempe', 'AZ', '85282', 33.4, -111.9, 4, 1, ?, ?, 4, NULL, 'run')",
        (now_ms, now_ms),
    )
    db.commit(conn)

    with pytest.raises(EngineError) as excinfo:
        _generate(conn, config, 1, SERVICE_DATE, now_ms=now_ms)
    assert excinfo.value.code is ErrorCode.INVARIANT_VIOLATED


def test_prospect_continuity_and_returning_pickup_and_transfer(tmp_path):
    config = _zone4_scenario(**{"demand.returning_prospect_probability": 1.0})
    conn, _run_id = _init(tmp_path, 7, config)
    now_ms = _now_ms(conn)

    rng0 = Rng(7, SPEC_VERSION, conn=conn)
    db.begin(conn)
    rng0.enter_txn()
    prospect_id = demand.apply_rejection_consequence(
        conn,
        config,
        now_ms=now_ms,
        prospect={
            "caller_name": "Jane Doe",
            "caller_phone": "480-555-0199",
            "segment": "residential",
            "home_zone_id": 4,
        },
    )
    rng0.exit_txn()
    db.commit(conn)

    initial_expected = demand._to_micros(
        config.resolved["demand.repeat_propensity_initial_by_segment"]["residential"]
        * config.get_decimal("demand.rejection_repeat_propensity_multiplier")
    )
    prospect_row = conn.execute(
        "SELECT propensity_micros FROM _prospect_state WHERE id = ?", (prospect_id,)
    ).fetchone()
    assert prospect_row["propensity_micros"] == initial_expected
    assert initial_expected < demand._to_micros(
        config.resolved["demand.repeat_propensity_initial_by_segment"]["residential"]
    )

    _generate(conn, config, 7, SERVICE_DATE, now_ms=now_ms)
    picked_up = conn.execute(
        "SELECT COUNT(*) c FROM _pending_calls WHERE prospect_id = ? AND caller_name = 'Jane Doe' "
        "AND caller_phone = '480-555-0199'",
        (prospect_id,),
    ).fetchone()["c"]
    assert picked_up >= 1

    db.begin(conn)
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, created_from_call_id, "
        "modified_at, origin) VALUES (5000, 'Jane Doe', 'residential', 'active', ?, NULL, ?, 'run')",
        (now_ms, now_ms),
    )
    demand.transfer_prospect_to_customer(conn, prospect_id=prospect_id, customer_id=5000, now_ms=now_ms)
    db.commit(conn)
    transferred = conn.execute(
        "SELECT propensity_micros, last_outcome FROM _customer_repeat_state WHERE customer_id = 5000"
    ).fetchone()
    assert transferred["propensity_micros"] == initial_expected
    assert transferred["last_outcome"] == "rejected"
    still_present = conn.execute(
        "SELECT 1 FROM _prospect_state WHERE id = ?", (prospect_id,)
    ).fetchone()
    assert still_present is not None


def test_propensity_never_reaches_zero_after_many_iterations(tmp_path):
    import random

    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    now_ms = _now_ms(conn)
    db.begin(conn)
    _create_customer(conn, customer_id=42, segment="residential", zone_id=4, now_ms=now_ms, propensity_micros=1_500_000)
    db.commit(conn)

    floor = config.get_decimal("customers.repeat_propensity_floor")
    floor_micros = demand._to_micros(floor)
    rand = random.Random(0)
    for _ in range(10_000):
        if rand.random() < 0.5:
            demand.apply_rejection_consequence(conn, config, now_ms=now_ms, customer_id=42)
        else:
            demand.apply_competitor_capture(conn, config, now_ms=now_ms, customer_id=42)
        propensity = conn.execute(
            "SELECT propensity_micros FROM _customer_repeat_state WHERE customer_id = 42"
        ).fetchone()["propensity_micros"]
        assert propensity > 0
        assert propensity >= floor_micros


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
    config = _zone4_scenario(**{"demand.base_new_caller_rate_per_day": 15})
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


def test_competitor_background_advances_once_per_generate_call(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 3, config)
    now_ms = _now_ms(conn)

    counter_before = conn.execute(
        "SELECT counter FROM _rng_state WHERE stream_name = 'competitor_background'"
    ).fetchone()
    assert counter_before is None

    _generate(conn, config, 3, SERVICE_DATE, now_ms=now_ms)
    counter_1 = conn.execute(
        "SELECT counter FROM _rng_state WHERE stream_name = 'competitor_background'"
    ).fetchone()["counter"]
    assert counter_1 == 1

    _generate(conn, config, 3, SERVICE_DATE_NEXT, now_ms=now_ms)
    counter_2 = conn.execute(
        "SELECT counter FROM _rng_state WHERE stream_name = 'competitor_background'"
    ).fetchone()["counter"]
    assert counter_2 == 2


def test_competitor_background_drift_deterministic_same_seed(tmp_path):
    config = _zone4_scenario()
    conn_a, _ra = _init(tmp_path, 11, config, "a.db")
    conn_b, _rb = _init(tmp_path, 11, config, "b.db")
    now_a = _now_ms(conn_a)
    now_b = _now_ms(conn_b)
    _generate(conn_a, config, 11, SERVICE_DATE, now_ms=now_a)
    _generate(conn_b, config, 11, SERVICE_DATE, now_ms=now_b)
    drift_a = conn_a.execute("SELECT drift_micros FROM _competitor_state").fetchone()["drift_micros"]
    drift_b = conn_b.execute("SELECT drift_micros FROM _competitor_state").fetchone()["drift_micros"]
    assert drift_a == drift_b


def test_competitor_share_untouched_by_default_and_decremented_when_recovery_enabled(tmp_path):
    config_off = _zone4_scenario()
    conn_off, _r1 = _init(tmp_path, 1, config_off, "off.db")
    now_off = _now_ms(conn_off)
    conn_off.execute("UPDATE _competitor_state SET share_micros = 100000 WHERE singleton = 1")
    _generate(conn_off, config_off, 1, SERVICE_DATE, now_ms=now_off)
    share_off = conn_off.execute("SELECT share_micros FROM _competitor_state").fetchone()["share_micros"]
    assert share_off == 100000

    config_on = _zone4_scenario(**{"competitors.share_recovery_enabled": True})
    conn_on, _r2 = _init(tmp_path, 1, config_on, "on.db")
    now_on = _now_ms(conn_on)
    conn_on.execute("UPDATE _competitor_state SET share_micros = 100000 WHERE singleton = 1")
    _generate(conn_on, config_on, 1, SERVICE_DATE, now_ms=now_on)
    share_on = conn_on.execute("SELECT share_micros FROM _competitor_state").fetchone()["share_micros"]
    expected_decay = demand._to_micros(config_on.get_decimal("competitors.share_decay_per_day"))
    assert share_on == 100000 - expected_decay


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


def test_apply_competitor_capture_share_caps_at_one(tmp_path):
    config = _zone4_scenario()
    conn, _run_id = _init(tmp_path, 1, config)
    now_ms = _now_ms(conn)
    db.begin(conn)
    _create_customer(conn, customer_id=1, segment="residential", zone_id=4, now_ms=now_ms, propensity_micros=1_000_000)
    db.commit(conn)
    conn.execute("UPDATE _competitor_state SET share_micros = 999_999 WHERE singleton = 1")
    demand.apply_competitor_capture(conn, config, now_ms=now_ms, customer_id=1)
    share = conn.execute("SELECT share_micros, capture_count FROM _competitor_state").fetchone()
    assert share["share_micros"] == 1_000_000
    assert share["capture_count"] == 1


def test_directional_reputation_effect_raises_new_callers_via_generate_for_date(tmp_path):
    config = _zone4_scenario()
    conn_good, _rid_good = _init(tmp_path, 7, config, "good.db")
    conn_bad, _rid_bad = _init(tmp_path, 7, config, "bad.db")

    for conn, stars in ((conn_good, 5), (conn_bad, 1)):
        now_ms = _now_ms(conn)
        conn.execute(
            "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
            "VALUES (1, 'Reviewer', 'residential', 'active', ?, ?, 'run')",
            (now_ms, now_ms),
        )
        for review_id in range(1, 11):
            conn.execute(
                "INSERT INTO _reviews (id, job_id, appointment_id, customer_id, technician_id, "
                "published_at, service_date, stars, source, created_at) VALUES "
                "(?, NULL, NULL, 1, NULL, ?, ?, ?, 'google', ?)",
                (review_id, now_ms, SERVICE_DATE, stars, now_ms),
            )

    now_good = _now_ms(conn_good)
    now_bad = _now_ms(conn_bad)
    m_rep_good = demand._reputation_multiplier_micros(conn_good, config, now_good)
    m_rep_bad = demand._reputation_multiplier_micros(conn_bad, config, now_bad)
    assert m_rep_bad < demand._to_micros(1) < m_rep_good

    summary_good = _generate(conn_good, config, 7, SERVICE_DATE, now_ms=now_good)
    summary_bad = _generate(conn_bad, config, 7, SERVICE_DATE, now_ms=now_bad)
    assert summary_bad["new_callers"] < summary_good["new_callers"]


def test_directional_word_of_mouth_effect_raises_new_callers_via_generate_for_date(tmp_path):
    config = _zone4_scenario()
    conn_good, _rid_good = _init(tmp_path, 7, config, "good.db")
    conn_bad, _rid_bad = _init(tmp_path, 7, config, "bad.db")

    conn_good.execute(
        "UPDATE _word_of_mouth_state SET wom_micros = ? WHERE segment = 'residential'",
        (300_000,),
    )
    conn_bad.execute(
        "UPDATE _word_of_mouth_state SET wom_micros = ? WHERE segment = 'residential'",
        (-300_000,),
    )
    m_wom_good = demand._word_of_mouth_multiplier_micros(conn_good, config)
    m_wom_bad = demand._word_of_mouth_multiplier_micros(conn_bad, config)
    assert m_wom_bad < m_wom_good

    now_good = _now_ms(conn_good)
    now_bad = _now_ms(conn_bad)
    summary_good = _generate(conn_good, config, 7, SERVICE_DATE, now_ms=now_good)
    summary_bad = _generate(conn_bad, config, 7, SERVICE_DATE, now_ms=now_bad)
    assert summary_bad["new_callers"] < summary_good["new_callers"]


def test_directional_competitor_share_lowers_multiplier_and_new_callers(tmp_path):
    from decimal import Decimal

    config = _zone4_scenario()
    conn_zero, _rid_zero = _init(tmp_path, 1, config, "zero.db")
    conn_raised, _rid_raised = _init(tmp_path, 1, config, "raised.db")

    m_zero = demand._competitor_multiplier_micros(conn_zero, config)
    conn_raised.execute(
        "UPDATE _competitor_state SET share_micros = ? WHERE singleton = 1",
        (demand._to_micros(Decimal("0.6")),),
    )
    m_raised = demand._competitor_multiplier_micros(conn_raised, config)
    assert m_raised < m_zero

    now_zero = _now_ms(conn_zero)
    now_raised = _now_ms(conn_raised)
    summary_zero = _generate(conn_zero, config, 1, SERVICE_DATE, now_ms=now_zero)
    summary_raised = _generate(conn_raised, config, 1, SERVICE_DATE, now_ms=now_raised)
    assert summary_raised["new_callers"] < summary_zero["new_callers"]


def test_directional_repeat_propensity_raises_hit_rate_via_generate_for_date(tmp_path):
    config = _zone4_scenario(**{"demand.repeat_call_daily_base_probability": 0.5})
    conn_high, _rid_high = _init(tmp_path, 5, config, "high.db")
    conn_low, _rid_low = _init(tmp_path, 5, config, "low.db")

    now_high = _now_ms(conn_high)
    now_low = _now_ms(conn_low)
    high_propensity = demand._to_micros(config.get_decimal("customers.repeat_propensity_max"))
    low_propensity = demand._to_micros(config.get_decimal("customers.repeat_propensity_floor"))

    db.begin(conn_high)
    for customer_id in range(1, 101):
        _create_customer(
            conn_high,
            customer_id=customer_id,
            segment="residential",
            zone_id=4,
            now_ms=now_high,
            propensity_micros=high_propensity,
        )
    db.commit(conn_high)

    db.begin(conn_low)
    for customer_id in range(1, 101):
        _create_customer(
            conn_low,
            customer_id=customer_id,
            segment="residential",
            zone_id=4,
            now_ms=now_low,
            propensity_micros=low_propensity,
        )
    db.commit(conn_low)

    summary_high = _generate(conn_high, config, 5, SERVICE_DATE, now_ms=now_high)
    summary_low = _generate(conn_low, config, 5, SERVICE_DATE, now_ms=now_low)

    assert summary_high["repeat_calls"] > summary_low["repeat_calls"]


def test_directional_storm_and_heat_raise_lambda(tmp_path_factory):
    hot_mild_date = "2026-10-10"
    storm_date = "2026-07-01"
    config = _zone4_scenario()

    storm_vols, calm_vols = [], []
    hot_vols, mild_vols = [], []
    for seed in range(1, 61):
        tmp_path = tmp_path_factory.mktemp(f"storm_hot_{seed}")
        conn, _run_id = _init(tmp_path, seed, config)
        catalog_rows = list(
            conn.execute(
                "SELECT id, name, supported_job_type_id FROM _service_catalog WHERE active = 1 "
                "ORDER BY id"
            ).fetchall()
        )
        storm_row = conn.execute(
            "SELECT high_f_tenths, low_f_tenths, storm_flag, storm_severity_milli "
            "FROM _zone_weather_daily WHERE zone_id = 4 AND local_date = ?",
            (storm_date,),
        ).fetchone()
        hot_row = conn.execute(
            "SELECT high_f_tenths, low_f_tenths, storm_flag, storm_severity_milli "
            "FROM _zone_weather_daily WHERE zone_id = 4 AND local_date = ?",
            (hot_mild_date,),
        ).fetchone()

        m_storm = demand._weather_multipliers_by_name(config, storm_row)
        vol_storm = demand._volume_weather_multiplier_micros(config, catalog_rows, storm_row, m_storm)
        (storm_vols if storm_row["storm_flag"] else calm_vols).append(vol_storm)

        m_hot = demand._weather_multipliers_by_name(config, hot_row)
        vol_hot = demand._volume_weather_multiplier_micros(config, catalog_rows, hot_row, m_hot)
        (hot_vols if hot_row["high_f_tenths"] > 850 else mild_vols).append(vol_hot)
        conn.close()

    assert storm_vols and calm_vols
    assert sum(storm_vols) / len(storm_vols) > sum(calm_vols) / len(calm_vols)
    assert hot_vols and mild_vols
    assert sum(hot_vols) / len(hot_vols) > sum(mild_vols) / len(mild_vols)


@pytest.fixture(scope="module")
def many_seed_dataset(tmp_path_factory):
    n_seeds = 40
    config = _zone4_scenario(**{"demand.base_new_caller_rate_per_day": 15})
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
