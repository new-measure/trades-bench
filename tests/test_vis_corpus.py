"""The VIS corpus against a warm-started database (spec 09 §VIS-01..12;
spec 02 §13: "run the full attack corpus against the warm-started database,
not an empty one"; CSR-002 decision 13).

``test_sql_gate.py`` owns the core corpus on a hand-built run database and
is deliberately untouched; this file adds the shapes it does not cover and
the corpus sections that need the fixture world: VIS-01..06's remaining
smuggling shapes (INTERSECT/EXCEPT, correlated subqueries, join and HAVING
conditions, ORDER BY over hidden reads), VIS-07's sweep (imported from the
SCH-04 file, written once) and absent-view probes, VIS-08 (imported from
test_supportedness), VIS-09's stored-view classification sweep, VIS-11's
post_run_only surface, and VIS-12's in-process rendered-value scan. The
full rendered-output scan — prompts, CLI, HTTP — is the CSR-012 form of
VIS-12; the in-process approximation here scans the view result rows
themselves, which is the only surface this ticket owns.
"""

from __future__ import annotations

import sqlite3

import pytest

from _helpers import register_conn
from csr_bench import clock, db, views
from csr_bench.clock import phoenix_instant
from csr_bench.config import load_run_config
from csr_bench.enums import VisibilityClass
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.run_init import initialize_run
from csr_bench.sql_gate import execute_gated, open_agent_session
from csr_bench.visibility import classify
from test_schema_contract import assert_no_view_row_past_the_clock
from test_supportedness import assert_supportedness_privacy

#: The one uniform error every denial must surface, byte for byte (VIS-05).
_UNAUTHORIZED_RESPONSE = EngineError.unauthorized().to_response()

#: The VIS-01..06 shapes test_sql_gate.py does not already carry, all run
#: against the warm-started database (spec 02 §13).
NEW_DENIED_SQL: tuple[tuple[str, str], ...] = (
    # -- VIS-02 set-operation smuggling ---------------------------------------
    ("smuggle-intersect", "SELECT id FROM customers INTERSECT SELECT id FROM _technician_true_skill"),
    ("smuggle-except", "SELECT id FROM customers EXCEPT SELECT id FROM _technician_true_skill"),
    ("smuggle-correlated", "SELECT id FROM customers c WHERE EXISTS (SELECT 1 FROM _technician_true_skill t WHERE t.technician_id = c.id)"),
    # -- VIS-03 join / aggregate / ordering conditions ------------------------
    ("join-hidden", "SELECT * FROM customers c JOIN _skills s ON 1=1"),
    ("having-hidden", "SELECT segment, COUNT(*) FROM customers GROUP BY segment HAVING COUNT(*) > (SELECT COUNT(*) FROM _technician_true_skill)"),
    ("orderby-hidden-aggregate", "SELECT id FROM customers ORDER BY (SELECT max(base_skill) FROM _technician_true_skill)"),
)


@pytest.fixture
def warm_run(tmp_path):
    """A warm-started run plus a gated agent session on a separate
    connection, exactly as the agent would use it. The engine connection is
    registered with the autouse closer."""
    db_path = tmp_path / "run.db"
    initialize_run(db_path, master_seed=12_345, config=load_run_config())
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


# ---------------------------------------------------------------------------
# VIS-01..06 — the remaining smuggling shapes (warm-started database)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql", [sql for _, sql in NEW_DENIED_SQL], ids=[label for label, _ in NEW_DENIED_SQL]
)
def test_new_denied_shapes_surface_the_uniform_error(warm_run, sql):
    _, agent, _ = warm_run
    with pytest.raises(EngineError) as ei:
        execute_gated(agent, sql)
    _assert_uniform_denial(ei)


# ---------------------------------------------------------------------------
# VIS-07 — future-information channels
# ---------------------------------------------------------------------------


def test_vis07_sweep_every_whitelisted_view_mid_day(warm_run):
    # VIS-07(c): sweeping every whitelisted view mid-day returns no row whose
    # timestamp is after current simulated time. The sweep is written once in
    # the SCH-04 file and imported here.
    engine, _, _ = warm_run
    noon = phoenix_instant(2026, 10, 1, 12, 0)
    db.begin(engine)
    clock.advance(engine, noon, "2026-10-01")
    db.commit(engine)
    assert_no_view_row_past_the_clock(engine, noon, "2026-10-01")


def test_no_weather_view_and_no_address_catalog_view(warm_run):
    # VIS-07(b): no weather surface exists at all; the address catalog is a
    # private table with no view. Both probes surface the byte-identical
    # uniform denial — indistinguishable from a hidden-table denial (VIS-05).
    _, agent, _ = warm_run
    for sql in ("SELECT * FROM weather", "SELECT * FROM address_catalog"):
        with pytest.raises(EngineError) as ei:
            execute_gated(agent, sql)
        _assert_uniform_denial(ei)


# ---------------------------------------------------------------------------
# VIS-08 — supportedness mapping stays private (SCH-07 owns the detail)
# ---------------------------------------------------------------------------


def test_vis08_supportedness_privacy(warm_run):
    _, agent, _ = warm_run
    assert_supportedness_privacy(agent)


# ---------------------------------------------------------------------------
# VIS-09 — field-classification sweep at the stored-view level
# ---------------------------------------------------------------------------


def test_vis09_every_stored_view_column_classifies_public(warm_run):
    # The registry-level rule is pinned by test_views' registry test; this
    # sweep runs against the *created* views on the warm-started database:
    # PRAGMA table_info must equal the registry's declared columns and every
    # declared column must resolve to a public class — no private_engine or
    # post_run_only value may sit on any public surface (spec 09 §VIS-09;
    # 02:592).
    engine, _, _ = warm_run
    for spec in views.VIEWS:
        stored = [row["name"] for row in engine.execute(f"PRAGMA table_info({spec.name})")]
        assert stored == [column.name for column in spec.columns], spec.name
        for column in spec.columns:
            if column.source is not None:
                assert classify(column.source) in VisibilityClass.public(), column
            else:
                assert column.visibility_class in VisibilityClass.public(), column


# ---------------------------------------------------------------------------
# VIS-11 — post_run_only surface stays offline
# ---------------------------------------------------------------------------


def test_vis11_post_run_only_surface(warm_run):
    # _run and _invocations are post_run_only (01:57; 02 §5): never through
    # the engine's live SQL surface, only through the offline analysis path
    # (direct database opening) once the run is terminal. Concretely: no view
    # exposes master_seed or any _invocations column; the gate denies both
    # tables with the uniform error; a direct engine connection reads them.
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
    # A warm-started run has no invocations — the count is exactly 0 (a `>= 0`
    # here would be tautological and could never fail).
    assert engine.execute("SELECT COUNT(*) c FROM _invocations").fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# VIS-12 — rendered-output corpus scan, in-process form
# ---------------------------------------------------------------------------
# The full-year rendered-output scan (every prompt, tool response, CLI line,
# notification, error string) is the CSR-012 form of VIS-12. The in-process
# approximation this ticket owns: scan every public view's result rows over
# the warm-started database for that run's private values — the hidden
# true-skill literals, the zero strain, and the run's master_seed — and
# assert none appears. Recruiting cost is deliberately NOT scanned: it is
# public via ledger_entries/hiring_requests (02:594).


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
