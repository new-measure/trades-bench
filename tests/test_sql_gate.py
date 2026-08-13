from __future__ import annotations

import sqlite3

import pytest

from csr_bench.db import connect, insert_run
from csr_bench.enums import RunStatus
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.migrations import migrate
from csr_bench.sql_gate import execute_gated, open_agent_session
from csr_bench.views import VIEWS, create_views

_UNAUTHORIZED_RESPONSE = EngineError.unauthorized().to_response()

DENIED_SQL: tuple[tuple[str, str], ...] = (
    ("hidden-unqualified", "SELECT * FROM _technician_true_skill"),
    ("hidden-quoted", 'SELECT * FROM "_customer_repeat_state"'),
    ("hidden-case", "SELECT * FROM _TECHNICIAN_TRUE_SKILL"),
    ("hidden-schema-qualified", "SELECT * FROM main._skills"),
    ("csr004-weather-process-state", "SELECT * FROM _weather_process_state"),
    ("csr004-zone-weather-daily", "SELECT * FROM _zone_weather_daily"),
    ("csr004-weather-forecast-cache", "SELECT * FROM _weather_forecast_cache"),
    ("csr004-pending-calls", "SELECT * FROM _pending_calls"),
    ("csr004-call-hidden", "SELECT * FROM _call_hidden"),
    ("csr004-customer-repeat-state", "SELECT * FROM _customer_repeat_state"),
    ("csr004-prospect-state", "SELECT * FROM _prospect_state"),
    ("csr004-word-of-mouth-state", "SELECT * FROM _word_of_mouth_state"),
    ("csr004-competitor-state", "SELECT * FROM _competitor_state"),
    ("csr004-acceptance-parameters", "SELECT * FROM _acceptance_parameters"),
    ("csr004-cancellation-schedule", "SELECT * FROM _cancellation_schedule"),
    ("csr004-join-hidden", "SELECT * FROM customers c JOIN _pending_calls p ON 1=1"),
    ("csr004-sqlite-schema-probe", "SELECT name FROM sqlite_schema WHERE name = '_call_hidden'"),
    ("smuggle-count", "SELECT COUNT(*) FROM (SELECT * FROM _technician_true_skill)"),
    ("smuggle-union", "SELECT * FROM customers UNION SELECT * FROM _technician_true_skill"),
    ("smuggle-exists", "SELECT * FROM customers WHERE EXISTS (SELECT 1 FROM _skills)"),
    ("smuggle-where", "SELECT * FROM customers WHERE id IN (SELECT technician_id FROM _technician_true_skill)"),
    ("smuggle-orderby", "SELECT * FROM customers ORDER BY (SELECT code FROM _skills LIMIT 1)"),
    ("groupby-hidden", "SELECT segment, COUNT(*) FROM customers GROUP BY (SELECT code FROM _skills LIMIT 1)"),
    ("pragma", "PRAGMA table_info(customers)"),
    ("pragma-tvf", "SELECT * FROM pragma_table_info('customers')"),
    ("sqlite-master", "SELECT * FROM sqlite_master"),
    ("sqlite-schema", "SELECT sql FROM sqlite_schema"),
    ("ddl-create", "CREATE TABLE x(a)"),
    ("ddl-create-temp", "CREATE TEMP TABLE t(x)"),
    ("ddl-drop", "DROP TABLE customers"),
    ("ddl-alter", "ALTER TABLE customers ADD COLUMN y"),
    ("ddl-alter-rename", "ALTER TABLE customers RENAME TO c2"),
    ("ddl-index-on-view", "CREATE INDEX ix ON customers(id)"),
    ("ddl-trigger-on-view", "CREATE TRIGGER trg AFTER INSERT ON customers BEGIN SELECT 1 END"),
    ("ddl-trigger-on-base", "CREATE TRIGGER trg AFTER INSERT ON _customers BEGIN SELECT 1 END"),
    ("dml-insert-view", "INSERT INTO customers (id, name) VALUES (9, 'x')"),
    ("dml-insert-base", "INSERT INTO _customers (id, name, segment, status, created_at, modified_at) VALUES (9, 'x', 'commercial', 'active', 0, 0)"),
    ("dml-update-view", "UPDATE customers SET name = 'x'"),
    ("dml-update-base", "UPDATE _customers SET name = 'x'"),
    ("dml-delete-view", "DELETE FROM customers"),
    ("attach", "ATTACH DATABASE ':memory:' AS x"),
    ("detach", "DETACH DATABASE x"),
    ("analyze", "ANALYZE"),
    ("explain", "EXPLAIN SELECT * FROM customers"),
    ("explain-query-plan", "EXPLAIN QUERY PLAN SELECT * FROM customers"),
    ("explain-comment-line", "EXPLAIN--c\nSELECT * FROM customers"),
    ("explain-comment-block", "EXPLAIN/*c*/SELECT * FROM customers"),
    ("multi-statement", "SELECT * FROM customers; SELECT * FROM _skills"),
    ("multi-statement-comment", "SELECT * FROM customers; -- sneak\nSELECT * FROM _skills"),
)


@pytest.fixture
def run_pair(tmp_path):
    path = tmp_path / "run.db"
    engine = connect(path)
    migrate(engine)
    create_views(engine)
    engine.execute("BEGIN")
    insert_run(
        engine,
        run_id="r-1",
        spec_version="1.0.0",
        config_version="1.0.0",
        config_hash="h",
        master_seed=1,
        start_at_ms=1_000,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        status=RunStatus.ACTIVE,
    )
    engine.execute("COMMIT")
    engine.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
        "VALUES (1, 'Acme Corp', 'commercial', 'active', 500, 500, 'run')"
    )
    agent = open_agent_session(path)
    try:
        yield agent, engine
    finally:
        agent.close()
        engine.close()


def _assert_uniform_denial(excinfo: pytest.ExceptionInfo[EngineError]) -> bytes:
    err = excinfo.value
    assert err.code is ErrorCode.UNAUTHORIZED
    assert err.message == "unauthorized"
    assert err.details == {}
    assert str(err) == "unauthorized"
    return err.to_response()


def test_legitimate_reads_work(run_pair):
    agent, _ = run_pair
    rows = execute_gated(agent, "SELECT * FROM customers").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == 1
    assert rows[0]["name"] == "Acme Corp"
    assert rows[0]["segment"] == "commercial"

    row = execute_gated(agent, "SELECT COUNT(*) AS n FROM customers").fetchone()
    assert row["n"] == 1

    row = execute_gated(
        agent,
        "SELECT service_date, as_of_ms, cash_balance_cents, run_status "
        "FROM company_state",
    ).fetchone()
    assert row["service_date"] == "2026-10-01"
    assert row["as_of_ms"] == 1_000
    assert row["cash_balance_cents"] == 0
    assert row["run_status"] == "active"

    rows = execute_gated(
        agent, "WITH c AS (SELECT id, name FROM customers) SELECT * FROM c"
    ).fetchall()
    assert [r["name"] for r in rows] == ["Acme Corp"]


@pytest.mark.parametrize(
    "sql", [sql for _, sql in DENIED_SQL], ids=[label for label, _ in DENIED_SQL]
)
def test_denied_shapes_surface_one_uniform_error(run_pair, sql):
    agent, _ = run_pair
    with pytest.raises(EngineError) as ei:
        execute_gated(agent, sql)
    assert _assert_uniform_denial(ei) == _UNAUTHORIZED_RESPONSE


def test_all_denials_are_byte_identical_to_each_other(run_pair):
    agent, _ = run_pair
    responses = set()
    for sql in (sql for _, sql in DENIED_SQL):
        with pytest.raises(EngineError) as ei:
            execute_gated(agent, sql)
        responses.add(_assert_uniform_denial(ei))
    assert responses == {_UNAUTHORIZED_RESPONSE}


def test_hidden_and_nonexistent_are_byte_indistinguishable(run_pair):
    agent, _ = run_pair
    responses = set()
    for sql in (
        "SELECT * FROM _technician_true_skill",
        'SELECT * FROM "_customer_repeat_state"',
        "SELECT * FROM main._skills",
        "SELECT * FROM nonexistent_view",
        "SELECT * FROM definitely_not_a_view",
    ):
        with pytest.raises(EngineError) as ei:
            execute_gated(agent, sql)
        responses.add(_assert_uniform_denial(ei))
    assert len(responses) == 1
    assert responses == {_UNAUTHORIZED_RESPONSE}

    row = execute_gated(agent, "SELECT COUNT(*) AS n FROM customers").fetchone()
    assert row["n"] == 1


def test_non_view_column_rejected_even_on_whitelisted_table(run_pair):
    agent, _ = run_pair
    spec = next(s for s in VIEWS if s.name == "locations")
    view_columns = {column.name for column in spec.columns}
    assert "effective_zone_id" not in view_columns
    assert "zone_id" in view_columns
    with pytest.raises(EngineError) as ei:
        execute_gated(agent, "SELECT effective_zone_id FROM locations")
    assert _assert_uniform_denial(ei) == _UNAUTHORIZED_RESPONSE


def test_engine_connection_can_still_write_private_tables(run_pair):
    agent, engine = run_pair
    engine.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
        "VALUES (2, 'Engine Row', 'residential', 'active', 600, 600, 'run')"
    )
    rows = execute_gated(agent, "SELECT name FROM customers ORDER BY id").fetchall()
    assert [r["name"] for r in rows] == ["Acme Corp", "Engine Row"]


def test_syntax_error_is_a_stable_invalid_query(run_pair):
    agent, _ = run_pair
    with pytest.raises(EngineError) as ei:
        execute_gated(agent, "SELEC * FROM customers")
    err = ei.value
    assert err.code is ErrorCode.INVALID_ARGUMENT
    assert err.message == "invalid query"
    assert err.details == {}
    assert str(err) == "invalid query"
    assert "SELEC" not in err.message
    assert "customers" not in err.message
    assert err.to_response() != _UNAUTHORIZED_RESPONSE


def test_explain_check_allows_legitimate_first_tokens(run_pair):
    agent, _ = run_pair
    rows = execute_gated(agent, "SELECT * FROM customers").fetchall()
    assert [r["name"] for r in rows] == ["Acme Corp"]
    rows = execute_gated(agent, "-- leading comment\nSELECT * FROM customers").fetchall()
    assert [r["name"] for r in rows] == ["Acme Corp"]
    row = execute_gated(
        agent, "WITH c AS (SELECT 1 AS explain) SELECT explain FROM c"
    ).fetchone()
    assert row["explain"] == 1


def test_semicolon_inside_a_string_literal_does_not_trip_the_scanner(run_pair):
    agent, _ = run_pair
    row = execute_gated(agent, "SELECT ';' AS x FROM customers").fetchone()
    assert row["x"] == ";"


def test_authorizer_defends_the_raw_connection_too(run_pair):
    agent, _ = run_pair
    with pytest.raises(sqlite3.DatabaseError):
        agent.execute("SELECT * FROM _skills").fetchall()


def test_begin_on_gated_session_is_denied_and_engine_writes_still_work(run_pair):
    agent, engine = run_pair
    with pytest.raises(EngineError) as ei:
        execute_gated(agent, "BEGIN")
    assert _assert_uniform_denial(ei) == _UNAUTHORIZED_RESPONSE
    engine.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
        "VALUES (3, 'Still Writable', 'residential', 'active', 700, 700, 'run')"
    )
    rows = execute_gated(agent, "SELECT name FROM customers ORDER BY id").fetchall()
    assert [r["name"] for r in rows] == ["Acme Corp", "Still Writable"]


@pytest.mark.parametrize("sql", ["SELECT 1;; ;", "SELECT 1; ;"])
def test_trailing_separators_are_invalid_query_not_unauthorized(run_pair, sql):
    agent, _ = run_pair
    with pytest.raises(EngineError) as ei:
        execute_gated(agent, sql)
    err = ei.value
    assert err.code is ErrorCode.INVALID_ARGUMENT
    assert err.message == "invalid query"
    assert err.details == {}
    assert "one statement" not in err.message
    assert err.to_response() != _UNAUTHORIZED_RESPONSE
