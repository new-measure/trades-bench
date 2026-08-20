from __future__ import annotations

import json
import sqlite3

import pytest

from test_http_engine import _build_engine, _open_session, _request
from test_sql_gate import DENIED_SQL
from test_vis_corpus import NEW_DENIED_SQL
from trades_bench.db import connect, insert_run
from trades_bench.enums import RunStatus
from trades_bench.errors import EngineError
from trades_bench.migrations import migrate
from trades_bench.sql_gate import _authorize_function, _authorize_read, open_agent_session
from trades_bench.sql_static import (
    SqlRejected,
    check_static,
    open_shadow_connection,
    validate_relations,
)
from trades_bench.views import VIEWS, create_views
from trades_bench.wire import WireCode

CEO_BENCH_BYPASSES: tuple[tuple[str, str], ...] = (
    ("replace-into", "REPLACE INTO customers (id) VALUES (1)"),
    ("comment-drop", "/* x */ DROP TABLE customers"),
    ("comment-delete", "-- comment\nDELETE FROM customers"),
    ("with-update", "WITH x AS (SELECT 1) UPDATE customers SET id=id"),
    ("attach", "ATTACH DATABASE '/tmp/evil.db' AS evildb"),
)

_FULL_DENIED_CORPUS: tuple[tuple[str, str], ...] = DENIED_SQL + NEW_DENIED_SQL


@pytest.fixture
def gate(tmp_path):
    path = tmp_path / "run.db"
    conn = connect(path)
    migrate(conn)
    create_views(conn)
    conn.execute("BEGIN")
    insert_run(
        conn,
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
    conn.execute("COMMIT")
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
        "VALUES (1, 'Acme Corp', 'commercial', 'active', 500, 500, 'run')"
    )
    shadow = open_shadow_connection(path)
    try:
        yield shadow
    finally:
        shadow.close()
        conn.close()


def _full_pipeline(shadow, sql: str) -> None:
    check_static(sql)
    validate_relations(shadow, sql)


@pytest.mark.parametrize(
    "sql", [sql for _, sql in CEO_BENCH_BYPASSES], ids=[label for label, _ in CEO_BENCH_BYPASSES]
)
def test_ceo_bench_bypasses_rejected_by_full_pipeline(gate, sql):
    with pytest.raises(SqlRejected):
        _full_pipeline(gate, sql)


@pytest.mark.parametrize(
    "sql", [sql for _, sql in CEO_BENCH_BYPASSES], ids=[label for label, _ in CEO_BENCH_BYPASSES]
)
def test_ceo_bench_bypasses_rejected_by_layer_a_alone(sql):
    with pytest.raises(SqlRejected):
        check_static(sql)


@pytest.mark.parametrize(
    "sql", [sql for _, sql in CEO_BENCH_BYPASSES], ids=[label for label, _ in CEO_BENCH_BYPASSES]
)
def test_ceo_bench_bypasses_rejected_by_layer_b_alone(gate, sql):
    with pytest.raises(SqlRejected):
        validate_relations(gate, sql)


@pytest.mark.parametrize(
    "sql", [sql for _, sql in _FULL_DENIED_CORPUS], ids=[label for label, _ in _FULL_DENIED_CORPUS]
)
def test_full_denial_corpus_rejected_by_layer_a_alone(sql):
    with pytest.raises(SqlRejected):
        check_static(sql)


@pytest.mark.parametrize(
    "sql", [sql for _, sql in _FULL_DENIED_CORPUS], ids=[label for label, _ in _FULL_DENIED_CORPUS]
)
def test_full_denial_corpus_rejected_by_layer_b_alone(gate, sql):
    with pytest.raises(SqlRejected):
        validate_relations(gate, sql)


def test_every_whitelisted_view_passes_both_layers(gate):
    for spec in VIEWS:
        _full_pipeline(gate, f"SELECT * FROM {spec.name}")


def test_legitimate_shapes_pass_both_layers(gate):
    for sql in (
        "SELECT COUNT(*) AS n FROM customers",
        "WITH recent AS (SELECT id, name FROM customers) SELECT * FROM recent",
        "SELECT service_date, as_of_ms, cash_balance_cents, run_status FROM company_state",
        "SELECT * FROM customers WHERE name LIKE 'Acme%'",
        "SELECT COUNT(*) FROM customers a, customers b, customers c, customers d",
    ):
        _full_pipeline(gate, sql)


def test_underscore_strings_in_value_positions_are_allowed(gate):
    for sql in (
        "SELECT id, name FROM customers WHERE name = '_x'",
        "SELECT id, name FROM customers WHERE name LIKE '_c%'",
        "SELECT id FROM customers WHERE name IN ('_a', '_b')",
        "SELECT '_customers' AS label FROM customers",
    ):
        _full_pipeline(gate, sql)


def test_string_relation_positions_missed_by_layer_a_are_blocked_by_layer_b(gate):
    for sql in (
        "SELECT k.current_time_ms FROM customers, '_clock' k",
        "SELECT id FROM customers CROSS JOIN '_clock'",
    ):
        with pytest.raises(SqlRejected) as ei:
            _full_pipeline(gate, sql)
        assert ei.value.wire_code is WireCode.UNKNOWN_RELATION


def test_alias_cannot_smuggle_a_hidden_column(gate):
    with pytest.raises(SqlRejected):
        _full_pipeline(gate, "SELECT trip_origin_latitude AS stolen FROM _technician_locations")
    with pytest.raises(SqlRejected):
        _full_pipeline(gate, "SELECT effective_zone_id AS stolen FROM locations")


def test_hidden_relation_and_nonexistent_relation_are_byte_indistinguishable(gate):
    codes = set()
    for sql in (
        "SELECT * FROM _technician_true_skill",
        "SELECT * FROM zzz_definitely_not_a_view",
        "SELECT * FROM custoemrs",
    ):
        with pytest.raises(SqlRejected) as ei:
            validate_relations(gate, sql)
        codes.add(ei.value.wire_code)
    assert codes == {WireCode.UNKNOWN_RELATION}


def test_hidden_column_and_nonexistent_column_are_byte_indistinguishable(gate):
    codes = set()
    for sql in (
        "SELECT effective_zone_id FROM locations",
        "SELECT nonexistent_col_xyz FROM locations",
    ):
        with pytest.raises(SqlRejected) as ei:
            validate_relations(gate, sql)
        codes.add(ei.value.wire_code)
    assert codes == {WireCode.UNKNOWN_COLUMN}


@pytest.mark.parametrize(
    "sql", ["SELECT random()", "SELECT randomblob(4)", "SELECT load_extension('x')"]
)
def test_dangerous_functions_are_rejected(gate, sql):
    with pytest.raises(SqlRejected) as ei:
        _full_pipeline(gate, sql)
    assert ei.value.wire_code is WireCode.SQL_NOT_PERMITTED


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM customers",
        "SELECT sum(1) FROM customers",
        "SELECT date(a.scheduled_start / 1000, 'unixepoch', '-7 hours') FROM appointments a",
    ],
)
def test_standard_functions_are_accepted(gate, sql):
    _full_pipeline(gate, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT date('now')",
        "SELECT datetime('now')",
        "SELECT julianday('now')",
        "SELECT strftime('%s','now')",
        "SELECT unixepoch('now')",
        "SELECT date('localtime')",
        "SELECT date('utc')",
        "SELECT date('NOW')",
        "SELECT date()",
        "SELECT datetime()",
        "SELECT time()",
        "SELECT julianday()",
        "SELECT unixepoch()",
        "SELECT strftime('%s')",
        "SELECT timediff('now', '2000-01-01')",
        "SELECT datetime('no'||'w')",
    ],
)
def test_wall_clock_modifiers_are_rejected(gate, sql):
    with pytest.raises(SqlRejected) as ei:
        _full_pipeline(gate, sql)
    assert ei.value.wire_code is WireCode.SQL_NOT_PERMITTED


def test_functions_used_inside_a_view_definition_are_not_agent_calls(gate):
    _full_pipeline(gate, "SELECT * FROM technician_locations")
    _full_pipeline(gate, "SELECT * FROM company_state")


def test_cte_name_cannot_shadow_a_protected_view(gate):
    sql = (
        "WITH technician_locations AS (SELECT trip_origin_latitude AS latitude "
        "FROM _technician_locations) SELECT latitude FROM technician_locations"
    )
    with pytest.raises(SqlRejected):
        check_static(sql)
    with pytest.raises(SqlRejected):
        validate_relations(gate, sql)


def test_nested_cte_shadow_is_also_rejected(gate):
    sql = (
        "SELECT * FROM (WITH technician_locations AS (SELECT canonical_json FROM _config) "
        "SELECT canonical_json FROM technician_locations)"
    )
    with pytest.raises(SqlRejected):
        check_static(sql)
    with pytest.raises(SqlRejected):
        validate_relations(gate, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, name FROM _customers",
        "SELECT current_time_ms FROM _clock",
    ],
)
def test_disabling_layer_a_still_blocks_hidden_reads_via_layer_b(gate, sql):
    with pytest.raises(SqlRejected) as ei:
        validate_relations(gate, sql)
    assert ei.value.wire_code is WireCode.UNKNOWN_RELATION


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM _technician_true_skill",
        'SELECT * FROM "_technician_true_skill"',
        "SELECT * FROM '_technician_true_skill'",
        "SELECT id, name FROM '_customers'",
        "SELECT id, name FROM main.'_customers'",
    ],
)
def test_disabling_layer_b_still_blocks_hidden_reads_via_layer_a(sql):
    with pytest.raises(SqlRejected) as ei:
        check_static(sql)
    assert ei.value.wire_code is WireCode.UNKNOWN_RELATION


@pytest.fixture
def agent_db_path(tmp_path):
    path = tmp_path / "run.db"
    conn = connect(path)
    migrate(conn)
    create_views(conn)
    conn.execute("BEGIN")
    insert_run(
        conn,
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
    conn.execute("COMMIT")
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
        "VALUES (1, 'Acme Corp', 'commercial', 'active', 500, 500, 'run')"
    )
    conn.close()
    return path


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, name FROM _customers",
        "SELECT id, name FROM '_customers'",
    ],
)
def test_disabling_layer_a_still_blocks_hidden_reads_via_the_runtime_gate(agent_db_path, sql):
    agent = open_agent_session(agent_db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            agent.execute(sql)
    finally:
        agent.close()


def test_cte_collision_beyond_technician_locations_is_blocked_on_the_real_agent_connection(
    agent_db_path,
):
    sql = "WITH customers AS (SELECT id, name FROM _customers) SELECT * FROM customers"
    agent = open_agent_session(agent_db_path)
    try:
        with pytest.raises(EngineError):
            agent.execute(sql)
    finally:
        agent.close()


def test_runtime_authorizer_alone_rejects_a_public_column_under_the_wrong_views_trigger_name():
    assert _authorize_read("_clock", "current_time_ms", "customers") == sqlite3.SQLITE_OK
    assert _authorize_read("_run", "status", "customers") == sqlite3.SQLITE_DENY
    assert _authorize_read("_customers", "id", "technician_locations") == sqlite3.SQLITE_DENY
    assert _authorize_read("_ledger_entries", "amount_cents", "customers") == sqlite3.SQLITE_DENY
    assert _authorize_read("_ledger_entries", "amount_cents", "company_state") == sqlite3.SQLITE_OK


def test_runtime_authorizer_alone_rejects_entropy_functions_under_any_trigger_name():
    assert _authorize_function("random", None) == sqlite3.SQLITE_DENY
    assert _authorize_function("random", "customers") == sqlite3.SQLITE_DENY
    assert _authorize_function("randomblob", "technician_locations") == sqlite3.SQLITE_DENY
    assert _authorize_function("json_extract", "technician_locations") == sqlite3.SQLITE_OK
    assert _authorize_function("json_extract", "not_a_real_view") == sqlite3.SQLITE_DENY
    assert _authorize_function("json_extract", None) == sqlite3.SQLITE_DENY


@pytest.mark.parametrize(
    "sql",
    [
        "WITH foo AS (SELECT random() AS r) SELECT * FROM foo",
        "WITH foo AS (SELECT randomblob(4) AS r) SELECT hex(r) FROM foo",
    ],
)
def test_entropy_functions_wrapped_in_a_cte_are_rejected_by_the_full_pipeline(gate, sql):
    with pytest.raises(SqlRejected) as ei:
        _full_pipeline(gate, sql)
    assert ei.value.wire_code is WireCode.SQL_NOT_PERMITTED


@pytest.mark.parametrize(
    "sql",
    [
        "WITH foo AS (SELECT random() AS r) SELECT * FROM foo",
        "WITH foo AS (SELECT randomblob(4) AS r) SELECT hex(r) FROM foo",
    ],
)
def test_entropy_functions_wrapped_in_a_cte_are_rejected_by_the_real_agent_connection(
    agent_db_path, sql
):
    agent = open_agent_session(agent_db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            agent.execute(sql)
    finally:
        agent.close()


_TIME_FILTER_BASE_TABLE_SPELLINGS: tuple[str, ...] = (
    "SELECT id, name FROM _customers",
    'SELECT id, name FROM "_customers"',
    "SELECT id, name FROM '_customers'",
    "SELECT id, name FROM main.'_customers'",
    "SELECT id, name FROM '_customers' WHERE created_at > 1000000",
)


@pytest.fixture
def time_filter_db_path(tmp_path):
    path = tmp_path / "run.db"
    conn = connect(path)
    migrate(conn)
    create_views(conn)
    conn.execute("BEGIN")
    insert_run(
        conn,
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
    conn.execute("COMMIT")
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
        "VALUES (1, 'Visible Now', 'commercial', 'active', 500, 500, 'run')"
    )
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
        "VALUES (2, 'Secret Future Customer', 'commercial', 'active', 9000000, 9000000, 'run')"
    )
    conn.close()
    return path


def test_time_filter_view_hides_the_future_row(time_filter_db_path):
    shadow = open_shadow_connection(time_filter_db_path)
    try:
        validate_relations(shadow, "SELECT id, name FROM customers")
        rows = shadow.execute("SELECT id, name FROM customers").fetchall()
        assert [tuple(row) for row in rows] == [(1, "Visible Now")]
    finally:
        shadow.close()


@pytest.mark.parametrize("sql", _TIME_FILTER_BASE_TABLE_SPELLINGS)
def test_time_filter_layer_a_alone_blocks_every_spelling_of_the_base_table(sql):
    with pytest.raises(SqlRejected) as ei:
        check_static(sql)
    assert ei.value.wire_code is WireCode.UNKNOWN_RELATION


@pytest.mark.parametrize("sql", _TIME_FILTER_BASE_TABLE_SPELLINGS)
def test_time_filter_layer_b_alone_blocks_every_spelling_of_the_base_table(time_filter_db_path, sql):
    shadow = open_shadow_connection(time_filter_db_path)
    try:
        with pytest.raises(SqlRejected) as ei:
            validate_relations(shadow, sql)
        assert ei.value.wire_code is WireCode.UNKNOWN_RELATION
    finally:
        shadow.close()


def test_http_sql_row_truncation(tmp_path):
    eng, conn = _build_engine(
        tmp_path / "rowcap",
        scenario_overrides={"interface.sql_max_rows": 2},
        with_slow_customers=True,
    )
    eng.start()
    try:
        _open_session(eng)
        status, body, _ = _request(
            eng.port,
            "POST",
            "/v1/sql",
            token=eng.tokens.agent,
            body={"sql": "SELECT id FROM customers ORDER BY id"},
        )
        assert status == 200
        assert body["data"]["truncated"] is True
        assert body["data"]["row_count"] == 2
    finally:
        eng.stop()
        conn.close()


def test_http_sql_byte_truncation(tmp_path):
    eng, conn = _build_engine(
        tmp_path / "bytecap",
        scenario_overrides={"interface.sql_max_response_bytes": 400},
        with_slow_customers=True,
    )
    eng.start()
    try:
        _open_session(eng)
        status, body, _ = _request(
            eng.port,
            "POST",
            "/v1/sql",
            token=eng.tokens.agent,
            body={"sql": "SELECT id, name FROM customers ORDER BY id"},
        )
        assert status == 200
        assert body["data"]["truncated"] is True
        assert len(json.dumps(body["data"]).encode("utf-8")) <= 500
    finally:
        eng.stop()
        conn.close()


def test_http_sql_new_wire_codes_end_to_end(tmp_path):
    eng, conn = _build_engine(tmp_path / "wirecodes")
    eng.start()
    try:
        _open_session(eng)

        status, body, _ = _request(
            eng.port,
            "POST",
            "/v1/sql",
            token=eng.tokens.agent,
            body={"sql": "SELECT * FROM zzz_not_a_view"},
        )
        assert status == 422
        assert body["error"]["code"] == "unknown_relation"

        status, body, _ = _request(
            eng.port,
            "POST",
            "/v1/sql",
            token=eng.tokens.agent,
            body={"sql": "SELECT effective_zone_id FROM locations"},
        )
        assert status == 422
        assert body["error"]["code"] == "unknown_column"

        status, body, _ = _request(
            eng.port,
            "POST",
            "/v1/sql",
            token=eng.tokens.agent,
            body={"sql": "DROP TABLE customers"},
        )
        assert status == 422
        assert body["error"]["code"] == "sql_not_permitted"

        status, body, _ = _request(
            eng.port,
            "POST",
            "/v1/sql",
            token=eng.tokens.agent,
            body={"sql": "SELECT random()"},
        )
        assert status == 422
        assert body["error"]["code"] == "sql_not_permitted"

        status, body, _ = _request(
            eng.port,
            "POST",
            "/v1/sql",
            token=eng.tokens.agent,
            body={"sql": "SELECT * FROM customers"},
        )
        assert status == 200
    finally:
        eng.stop()
        conn.close()


def test_time_filter_survives_every_spelling_of_the_base_table(tmp_path):
    eng, conn = _build_engine(tmp_path / "timefilter")
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
        "VALUES (1, 'Visible Now', 'commercial', 'active', 500, 500, 'run')"
    )
    conn.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
        "VALUES (2, 'Secret Future Customer', 'commercial', 'active', 9000000, 9000000, 'run')"
    )
    conn.execute("COMMIT")
    eng.start()
    try:
        _open_session(eng, 1_000_000)

        status, body, _ = _request(
            eng.port,
            "POST",
            "/v1/sql",
            token=eng.tokens.agent,
            body={"sql": "SELECT id, name FROM customers"},
        )
        assert status == 200, body
        assert body["data"]["rows"] == [[1, "Visible Now"]]

        for sql in (
            "SELECT id, name FROM _customers",
            'SELECT id, name FROM "_customers"',
            "SELECT id, name FROM '_customers'",
            "SELECT id, name FROM main.'_customers'",
            "SELECT id, name FROM '_customers' WHERE created_at > 1000000",
        ):
            status, body, _ = _request(
                eng.port,
                "POST",
                "/v1/sql",
                token=eng.tokens.agent,
                body={"sql": sql},
            )
            assert status == 422, (sql, body)
            assert body["error"]["code"] == "unknown_relation", (sql, body)
    finally:
        eng.stop()
        conn.close()
