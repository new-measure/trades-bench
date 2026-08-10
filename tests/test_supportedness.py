"""SCH-07 supportedness privacy (spec 02 §2.5, §7.1; spec 09 VIS-08;
CSR-002 decisions 6, 8).

The public surface must not resolve an arbitrary requested-category string to
a supported job type: the private ``_service_catalog`` carries the
category→supported mapping and is unreachable through the gate with the
uniform error (VIS-08); no public view column is named for the mapping; and
the public ``job_types`` view exposes exactly the five seeded types, with
``other`` carrying NULL business unit and NULL required skill — the "is this
supported?" question is answerable only from company knowledge (02:600).
"""

from __future__ import annotations

import sqlite3

import pytest

from _helpers import register_conn
from csr_bench.config import load_run_config
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.run_init import initialize_run
from csr_bench.sql_gate import execute_gated, open_agent_session
from csr_bench.views import VIEWS

#: The one uniform error every gate denial surfaces, byte for byte (VIS-05).
_UNAUTHORIZED_RESPONSE = EngineError.unauthorized().to_response()


def _warm_run(tmp_path, seed: int = 7):
    """A warm-started run on a tmp path; returns ``(db_path, conn)`` with the
    engine connection registered with the autouse closer."""
    db_path = tmp_path / "run.db"
    initialize_run(db_path, master_seed=seed, config=load_run_config())
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    register_conn(conn)
    return db_path, conn


def _agent(db_path):
    agent = open_agent_session(db_path)
    register_conn(agent)
    return agent


def assert_supportedness_privacy(agent: sqlite3.Connection) -> None:
    """The SCH-07/VIS-08 assertions, shared with the VIS corpus test."""
    with pytest.raises(EngineError) as ei:
        execute_gated(agent, "SELECT * FROM _service_catalog")
    assert ei.value.to_response() == _UNAUTHORIZED_RESPONSE

    # no public view column names the category→supported mapping (02:600:
    # "Nothing publicly maps an arbitrary requested category to a supported
    # type")
    for spec in VIEWS:
        names = {column.name for column in spec.columns}
        assert names.isdisjoint(
            {"supported_job_type_id", "is_supported", "category_id"}
        ), f"{spec.name} exposes a supportedness column"


def test_service_catalog_is_unreachable_via_the_gate(tmp_path):
    db_path, _ = _warm_run(tmp_path)
    agent = _agent(db_path)
    for sql in (
        "SELECT * FROM _service_catalog",
        "SELECT name FROM _service_catalog",
        "SELECT supported_job_type_id FROM _service_catalog",
    ):
        with pytest.raises(EngineError) as ei:
            execute_gated(agent, sql)
        assert ei.value.code is ErrorCode.UNAUTHORIZED
        assert ei.value.to_response() == _UNAUTHORIZED_RESPONSE


def test_no_public_column_resolves_supportedness(tmp_path):
    db_path, _ = _warm_run(tmp_path)
    assert_supportedness_privacy(_agent(db_path))


def test_job_types_exposes_exactly_the_five_types(tmp_path):
    # spec 02 §2.3: the four supported types plus the misbooking sink
    # `other`, which carries NULL business_unit_id and NULL
    # required_skill_code (02:190).
    _, conn = _warm_run(tmp_path)
    rows = conn.execute(
        "SELECT code, business_unit_id, required_skill_code FROM job_types ORDER BY id"
    ).fetchall()
    assert [(r["code"], r["business_unit_id"], r["required_skill_code"]) for r in rows] == [
        ("hvac_service", 1, "hvac_service"),
        ("hvac_installation", 1, "hvac_install"),
        ("plumbing", 2, "plumbing"),
        ("electrical", 3, "electrical"),
        ("other", None, None),
    ]
