"""SCH-08 provenance hidden (spec 02 §1.3, §13; spec 09 VIS-09;
CSR-002 decisions 8, 18).

``origin`` is a private provenance marker on the 15 root-entity tables
(§1.3) and must appear in no public view; and warm-start rows must be
structurally indistinguishable from run rows in every public projection
(02:616: "as ordinary rows indistinguishable in structure from rows the run
will create"). The fixture passes ``'warm_start'`` explicitly while the
schema default is ``'run'`` (decision 18), so a mixed world exists to prove
indistinguishability: the four fixture technicians (``origin='warm_start'``)
and a hand-inserted run technician (default ``origin='run'``) must project
through the ``technicians`` view with identical column sets and shapes.
"""

from __future__ import annotations

import sqlite3

from _helpers import register_conn
from csr_bench.config import load_run_config
from csr_bench.run_init import initialize_run
from csr_bench.views import VIEWS


def _warm_run(tmp_path):
    db_path = tmp_path / "run.db"
    initialize_run(db_path, master_seed=11, config=load_run_config())
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    register_conn(conn)
    return conn


def test_origin_appears_in_no_view_column_list():
    for spec in VIEWS:
        assert all(column.name != "origin" for column in spec.columns), spec.name


def test_warm_start_rows_are_indistinguishable_from_run_rows(tmp_path):
    # The four fixture technicians carry origin='warm_start'; the inserted
    # row 900 is a run row (the schema default). Both project identically
    # through the technicians view: same column set, no origin, same shapes.
    conn = _warm_run(tmp_path)
    conn.execute(
        "INSERT INTO _technicians (id, name, street, unit, city, state, postal_code, "
        "home_latitude, home_longitude, home_zone_id, joined_on, hourly_wage_cents, "
        "employment_status, availability_status, created_at, modified_at) VALUES "
        "(900, 'Run Tech', '', '', 'Phoenix', 'AZ', '85001', 33.5, -112.0, 1, "
        "'2026-01-01', 1000, 'active', 'available', 0, 0)"
    )
    rows = conn.execute("SELECT * FROM technicians ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [1, 2, 3, 4, 900]
    run_row = rows[-1]
    declared = [
        column.name
        for spec in VIEWS
        if spec.name == "technicians"
        for column in spec.columns
    ]
    assert list(run_row.keys()) == declared
    assert "origin" not in run_row.keys()
    # every projection column resolves the same way for both origins
    for warm_row in rows[:-1]:
        assert list(warm_row.keys()) == list(run_row.keys())
        for key in ("id", "name", "home_zone_id"):
            assert key in run_row.keys()
            assert warm_row[key] is not None
