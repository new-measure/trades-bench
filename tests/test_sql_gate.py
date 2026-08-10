"""Tests for the in-process SQL authorization gate (spec 09 VIS-01..06;
CSR-002 decision 13): the agent-session connection, the authorizer rule
table, and the uniform sanitized error surface.

The gate is tested against a real warm-ish run database built through the
real machinery — migration runner, view registry, run record + clock row —
plus one committed public row, then opened through
:func:`csr_bench.sql_gate.open_agent_session` as a *separate* connection, the
way the agent would use it.

The byte-identity assertions compare ``EngineError.to_response()`` across
attack shapes: VIS-05 requires that hidden, nonexistent, column-level,
schema-inspection, and DML attacks all surface one identical error.
"""

from __future__ import annotations

import sqlite3

import pytest

from csr_bench.db import connect, insert_run
from csr_bench.enums import RunStatus
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.migrations import migrate
from csr_bench.sql_gate import execute_gated, open_agent_session
from csr_bench.views import VIEWS, create_views

#: The one error every denial must surface, byte for byte (VIS-05).
_UNAUTHORIZED_RESPONSE = EngineError.unauthorized().to_response()

#: (label, sql) pairs that must all be denied with the uniform error.
DENIED_SQL: tuple[tuple[str, str], ...] = (
    # -- VIS-01 direct and aliased access: unquoted, quoted, case, schema --
    ("hidden-unqualified", "SELECT * FROM _technician_true_skill"),
    ("hidden-quoted", 'SELECT * FROM "_customer_repeat_state"'),
    ("hidden-case", "SELECT * FROM _TECHNICIAN_TRUE_SKILL"),
    ("hidden-schema-qualified", "SELECT * FROM main._skills"),
    # -- VIS-02 CTEs, subqueries, set operations -----------------------------
    ("smuggle-count", "SELECT COUNT(*) FROM (SELECT * FROM _technician_true_skill)"),
    ("smuggle-union", "SELECT * FROM customers UNION SELECT * FROM _technician_true_skill"),
    ("smuggle-exists", "SELECT * FROM customers WHERE EXISTS (SELECT 1 FROM _skills)"),
    ("smuggle-where", "SELECT * FROM customers WHERE id IN (SELECT technician_id FROM _technician_true_skill)"),
    ("smuggle-orderby", "SELECT * FROM customers ORDER BY (SELECT code FROM _skills LIMIT 1)"),
    # -- VIS-03 aggregates / predicates referencing hidden tables ------------
    ("groupby-hidden", "SELECT segment, COUNT(*) FROM customers GROUP BY (SELECT code FROM _skills LIMIT 1)"),
    # -- VIS-04 schema inspection, DDL, DML, ATTACH, EXPLAIN, multi-statement --
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
    # No semicolon in the body: with one, the multi-statement scanner would
    # refuse the payload before SQLite ever saw it, so the no-semicolon form
    # is the one that pins the DDL-on-view refusal path; the `_customers`
    # variant reaches the authorizer itself (SQLITE_CREATE_TRIGGER is denied).
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
    """A real run database plus a gated agent session on a separate connection.

    Engine connection on a file, migrated and view-created through the real
    open sequence, with the run record + clock row and one committed public
    customer row. The gate opens its own connection to the same file.
    """
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


# ---------------------------------------------------------------------------
# Test 1 — legitimate reads work
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Test 2 — every attack shape is denied with the one uniform error
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Test 3 — hidden and nonexistent objects are byte-indistinguishable (VIS-05)
# ---------------------------------------------------------------------------


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

    # the connection stays usable after denials
    row = execute_gated(agent, "SELECT COUNT(*) AS n FROM customers").fetchone()
    assert row["n"] == 1


# ---------------------------------------------------------------------------
# Test 6 — column-level: private base columns are not view columns (VIS-03)
# ---------------------------------------------------------------------------


def test_non_view_column_rejected_even_on_whitelisted_table(run_pair):
    agent, _ = run_pair
    spec = next(s for s in VIEWS if s.name == "locations")
    view_columns = {column.name for column in spec.columns}
    assert "effective_zone_id" not in view_columns  # private base column
    assert "zone_id" in view_columns  # the view's own column
    with pytest.raises(EngineError) as ei:
        execute_gated(agent, "SELECT effective_zone_id FROM locations")
    assert _assert_uniform_denial(ei) == _UNAUTHORIZED_RESPONSE


# ---------------------------------------------------------------------------
# Test 7 — the gate is per-connection: the engine side is unaffected
# ---------------------------------------------------------------------------


def test_engine_connection_can_still_write_private_tables(run_pair):
    agent, engine = run_pair
    engine.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
        "VALUES (2, 'Engine Row', 'residential', 'active', 600, 600, 'run')"
    )
    rows = execute_gated(agent, "SELECT name FROM customers ORDER BY id").fetchall()
    assert [r["name"] for r in rows] == ["Acme Corp", "Engine Row"]


# ---------------------------------------------------------------------------
# Syntax errors: stable INVALID_ARGUMENT, not a denial, no SQL echo
# ---------------------------------------------------------------------------


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
    assert err.to_response() != _UNAUTHORIZED_RESPONSE  # a query failure, not a probe


# ---------------------------------------------------------------------------
# The payload pre-scan must not false-trip on legitimate payloads
# ---------------------------------------------------------------------------


def test_explain_check_allows_legitimate_first_tokens(run_pair):
    agent, _ = run_pair
    # plain SELECT: the first token is not EXPLAIN
    rows = execute_gated(agent, "SELECT * FROM customers").fetchall()
    assert [r["name"] for r in rows] == ["Acme Corp"]
    # a leading comment is skipped before the first token, not glued to it
    rows = execute_gated(agent, "-- leading comment\nSELECT * FROM customers").fetchall()
    assert [r["name"] for r in rows] == ["Acme Corp"]
    # the word 'explain' as a column alias must not trip the check
    row = execute_gated(
        agent, "WITH c AS (SELECT 1 AS explain) SELECT explain FROM c"
    ).fetchone()
    assert row["explain"] == 1


def test_semicolon_inside_a_string_literal_does_not_trip_the_scanner(run_pair):
    agent, _ = run_pair
    row = execute_gated(agent, "SELECT ';' AS x FROM customers").fetchone()
    assert row["x"] == ";"


# ---------------------------------------------------------------------------
# The authorizer is on the connection itself, not just the wrapper
# ---------------------------------------------------------------------------


def test_authorizer_defends_the_raw_connection_too(run_pair):
    agent, _ = run_pair
    with pytest.raises(sqlite3.DatabaseError):
        agent.execute("SELECT * FROM _skills").fetchall()


# ---------------------------------------------------------------------------
# BEGIN is denied, so a gated session can never hold a read transaction
# ---------------------------------------------------------------------------


def test_begin_on_gated_session_is_denied_and_engine_writes_still_work(run_pair):
    # SQLITE_TRANSACTION is not in the allowlist (final-branch audit F5): a
    # held-open read transaction on the agent connection would block engine
    # writes with SQLITE_BUSY. The denial is the uniform unauthorized error,
    # the same shape as every other refusal.
    agent, engine = run_pair
    with pytest.raises(EngineError) as ei:
        execute_gated(agent, "BEGIN")
    assert _assert_uniform_denial(ei) == _UNAUTHORIZED_RESPONSE
    # the denial leaves no transaction behind: the engine writes while the
    # agent connection stays open
    engine.execute(
        "INSERT INTO _customers (id, name, segment, status, created_at, modified_at, origin) "
        "VALUES (3, 'Still Writable', 'residential', 'active', 700, 700, 'run')"
    )
    rows = execute_gated(agent, "SELECT name FROM customers ORDER BY id").fetchall()
    assert [r["name"] for r in rows] == ["Acme Corp", "Still Writable"]


# ---------------------------------------------------------------------------
# Trailing-statement payloads are query failures, not denials (VIS-04; F7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", ["SELECT 1;; ;", "SELECT 1; ;"])
def test_trailing_separators_are_invalid_query_not_unauthorized(run_pair, sql):
    # The lexical scanner treats trailing separators as insignificant, so the
    # payload reaches sqlite3, whose Python ≥3.12 wrapper refuses it with
    # "You can only execute one statement at a time" — a payload-shape
    # failure, mapped to the stable invalid query like any parse failure,
    # never folded into the uniform denial (final-branch audit F7).
    agent, _ = run_pair
    with pytest.raises(EngineError) as ei:
        execute_gated(agent, sql)
    err = ei.value
    assert err.code is ErrorCode.INVALID_ARGUMENT
    assert err.message == "invalid query"
    assert err.details == {}
    assert "one statement" not in err.message  # no SQLite message echo
    assert err.to_response() != _UNAUTHORIZED_RESPONSE
