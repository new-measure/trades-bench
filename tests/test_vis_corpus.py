from __future__ import annotations

import sqlite3

import pytest

from _helpers import GEO_ARTIFACT_DIR, geo_run_config, register_conn
from test_geography import assert_maps_distance_schema_is_closed
from test_schema_contract import assert_no_view_row_past_the_clock
from test_supportedness import assert_supportedness_privacy
from trades_bench import SPEC_VERSION, clock, db, demand, views
from trades_bench.clock import phoenix_instant
from trades_bench.enums import VisibilityClass
from trades_bench.errors import EngineError, ErrorCode
from trades_bench.rng import Rng
from trades_bench.run_init import initialize_run
from trades_bench.sql_gate import execute_gated, open_agent_session
from trades_bench.visibility import FIELD_REGISTRY, classify

_UNAUTHORIZED_RESPONSE = EngineError.unauthorized().to_response()

_TRAVEL_PRIVATE_TERMS = ("speed", "band", "multiplier", "noise")

NEW_DENIED_SQL: tuple[tuple[str, str], ...] = (
    ("smuggle-intersect", "SELECT id FROM customers INTERSECT SELECT id FROM _technician_true_skill"),
    ("smuggle-except", "SELECT id FROM customers EXCEPT SELECT id FROM _technician_true_skill"),
    ("smuggle-correlated", "SELECT id FROM customers c WHERE EXISTS (SELECT 1 FROM _technician_true_skill t WHERE t.technician_id = c.id)"),
    ("join-hidden", "SELECT * FROM customers c JOIN _skills s ON 1=1"),
    ("having-hidden", "SELECT segment, COUNT(*) FROM customers GROUP BY segment HAVING COUNT(*) > (SELECT COUNT(*) FROM _technician_true_skill)"),
    ("orderby-hidden-aggregate", "SELECT id FROM customers ORDER BY (SELECT max(base_skill) FROM _technician_true_skill)"),
)


@pytest.fixture
def warm_run(tmp_path):
    db_path = tmp_path / "run.db"
    initialize_run(
        db_path, master_seed=12_345, config=geo_run_config(), artifact_dir=GEO_ARTIFACT_DIR
    )
    engine = sqlite3.connect(str(db_path), isolation_level=None)
    engine.row_factory = sqlite3.Row
    register_conn(engine)
    agent = open_agent_session(db_path)
    register_conn(agent)
    return engine, agent, db_path


def _assert_uniform_denial(ei: pytest.ExceptionInfo[EngineError]) -> None:
    assert ei.value.code is ErrorCode.UNAUTHORIZED
    assert ei.value.message == "unauthorized"
    assert ei.value.to_response() == _UNAUTHORIZED_RESPONSE


@pytest.mark.parametrize(
    "sql", [sql for _, sql in NEW_DENIED_SQL], ids=[label for label, _ in NEW_DENIED_SQL]
)
def test_new_denied_shapes_surface_the_uniform_error(warm_run, sql):
    _, agent, _ = warm_run
    with pytest.raises(EngineError) as ei:
        execute_gated(agent, sql)
    _assert_uniform_denial(ei)


def test_vis07_sweep_every_whitelisted_view_mid_day(warm_run):
    engine, _, _ = warm_run
    noon = phoenix_instant(2026, 10, 1, 12, 0)
    db.begin(engine)
    clock.advance(engine, noon, "2026-10-01")
    db.commit(engine)
    assert_no_view_row_past_the_clock(engine, noon, "2026-10-01")


def test_pending_calls_generated_mid_day_leave_no_trace_in_any_whitelisted_view(warm_run):
    engine, agent, _ = warm_run
    service_date = "2026-10-01"
    now_ms = engine.execute("SELECT current_time_ms FROM _clock").fetchone()["current_time_ms"]

    rng = Rng(12_345, SPEC_VERSION, conn=engine)
    db.begin(engine)
    rng.enter_txn()
    demand.generate_for_date(engine, geo_run_config(), rng, service_date=service_date, now_ms=now_ms)
    rng.exit_txn()
    db.commit(engine)

    noon = phoenix_instant(2026, 10, 1, 12, 0)
    pending = engine.execute(
        "SELECT caller_name, caller_phone, arrival_at FROM _pending_calls WHERE service_date = ?",
        (service_date,),
    ).fetchall()
    assert pending
    assert any(row["arrival_at"] > noon for row in pending)
    leaked_names = {row["caller_name"] for row in pending}
    leaked_phones = {row["caller_phone"] for row in pending}
    sentinel_name = pending[0]["caller_name"]
    sentinel_phone = pending[0]["caller_phone"]

    db.begin(engine)
    clock.advance(engine, noon, service_date)
    db.commit(engine)

    assert_no_view_row_past_the_clock(engine, noon, service_date)

    sentinel_call_id = 2_000_000_000
    db.begin(engine)
    engine.execute(
        "INSERT INTO _calls (id, arrived_at, service_date, call_kind, status, caller_name, "
        "caller_phone, requested_service_name, urgent, created_at) VALUES "
        "(?, ?, ?, 'new_job', 'active', ?, ?, 'HVAC service', 0, ?)",
        (sentinel_call_id, noon, service_date, sentinel_name, sentinel_phone, noon),
    )
    db.commit(engine)
    assert execute_gated(agent, "SELECT COUNT(*) AS n FROM calls").fetchone()["n"] == 1
    sentinel_caught = any(
        isinstance(value, str) and (value in leaked_names or value in leaked_phones)
        for row in execute_gated(agent, "SELECT * FROM calls").fetchall()
        for value in row
    )
    assert sentinel_caught
    db.begin(engine)
    engine.execute("DELETE FROM _calls WHERE id = ?", (sentinel_call_id,))
    db.commit(engine)

    assert execute_gated(agent, "SELECT COUNT(*) AS n FROM calls").fetchone()["n"] == 0
    for spec in views.VIEWS:
        for row in execute_gated(agent, f"SELECT * FROM {spec.name}").fetchall():
            for value in row:
                if isinstance(value, str):
                    assert value not in leaked_names
                    assert value not in leaked_phones


def test_no_weather_view_and_no_address_catalog_view(warm_run):
    _, agent, _ = warm_run
    for sql in ("SELECT * FROM weather", "SELECT * FROM address_catalog"):
        with pytest.raises(EngineError) as ei:
            execute_gated(agent, sql)
        _assert_uniform_denial(ei)


def test_no_travel_speed_band_or_weather_multiplier_view(warm_run):
    _, agent, _ = warm_run
    for sql in (
        "SELECT * FROM travel_speeds",
        "SELECT * FROM speed_by_time_band",
        "SELECT * FROM time_of_day_bands",
        "SELECT * FROM weather_travel_multipliers",
        "SELECT * FROM travel_noise",
        "SELECT * FROM routing_anchors",
        "SELECT * FROM anchor_distance_matrix",
    ):
        with pytest.raises(EngineError) as ei:
            execute_gated(agent, sql)
        _assert_uniform_denial(ei)


def test_no_speed_band_multiplier_or_noise_column_is_public_in_the_visibility_registry():
    for field, vis_class in FIELD_REGISTRY.items():
        if vis_class not in VisibilityClass.public():
            continue
        lowered = field.column.lower()
        for term in _TRAVEL_PRIVATE_TERMS:
            assert term not in lowered, field


def test_det07c_maps_distance_value_contract_stays_closed():
    assert_maps_distance_schema_is_closed()


def test_vis08_supportedness_privacy(warm_run):
    _, agent, _ = warm_run
    assert_supportedness_privacy(agent)


def test_vis09_every_stored_view_column_classifies_public(warm_run):
    engine, _, _ = warm_run
    for spec in views.VIEWS:
        stored = [row["name"] for row in engine.execute(f"PRAGMA table_info({spec.name})")]
        assert stored == [column.name for column in spec.columns], spec.name
        for column in spec.columns:
            if column.source is not None:
                assert classify(column.source) in VisibilityClass.public(), column
            else:
                assert column.visibility_class in VisibilityClass.public(), column


def test_vis11_post_run_only_surface(warm_run):
    engine, agent, _ = warm_run
    for spec in views.VIEWS:
        assert all(column.name != "master_seed" for column in spec.columns)
        assert all(column.source is None or column.source.table != "_invocations" for column in spec.columns)
    for sql in ("SELECT * FROM _run", "SELECT master_seed FROM _run", "SELECT * FROM _invocations"):
        with pytest.raises(EngineError) as ei:
            execute_gated(agent, sql)
        _assert_uniform_denial(ei)
    seed = engine.execute("SELECT master_seed FROM _run").fetchone()["master_seed"]
    assert seed == 12_345
    assert engine.execute("SELECT COUNT(*) c FROM _invocations").fetchone()["c"] == 0


def test_vis12_no_private_value_in_any_view_result(warm_run):
    engine, agent, _ = warm_run
    private_floats = {
        row["base_skill"]
        for row in engine.execute("SELECT base_skill FROM _technician_true_skill")
    } | {row["strain_value"] for row in engine.execute("SELECT strain_value FROM _technician_strain")}
    master_seed = engine.execute("SELECT master_seed FROM _run").fetchone()["master_seed"]
    assert master_seed not in {0}
    for spec in views.VIEWS:
        for row in execute_gated(agent, f"SELECT * FROM {spec.name}").fetchall():
            for value in row:
                if isinstance(value, float):
                    assert value not in private_floats, (
                        f"{spec.name} exposes private float {value}"
                    )
                elif isinstance(value, int) and value == master_seed:
                    assert False, f"{spec.name} exposes the run's master_seed"
