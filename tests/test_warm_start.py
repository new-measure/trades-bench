from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest

from _helpers import GEO_ARTIFACT_DIR, geo_run_config, geo_scenario_json, register_conn
from csr_bench import _dump, clock, db, warm_start
from csr_bench.config import load_run_config
from csr_bench.db import get_run, in_memory
from csr_bench.enums import RunStatus
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.ledger import Ledger
from csr_bench.migrations import migrate
from csr_bench.run_init import boundary_check, initialize_run, open_run_db
from csr_bench.sql_gate import execute_gated, open_agent_session
from csr_bench.views import VIEWS

_UNAUTHORIZED_RESPONSE = EngineError.unauthorized().to_response()


def _build(tmp_path, seed=1, config=None):
    cfg = config if config is not None else geo_run_config()
    db_path = tmp_path / f"run-{seed}-{uuid.uuid4().hex[:8]}.db"
    run_id = initialize_run(
        db_path, master_seed=seed, config=cfg, artifact_dir=GEO_ARTIFACT_DIR
    )
    conn = open_run_db(db_path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    return conn, run_id, cfg, db_path


def test_ws01_clean_boundary(tmp_path):
    conn, run_id, cfg, _ = _build(tmp_path)
    boundary_check(conn, cfg)
    assert conn.execute("SELECT COUNT(*) c FROM _calls").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM _appointments").fetchone()["c"] == 0
    assert get_run(conn, run_id)["status"] == "active"


def test_ws02_roster_and_coverage(tmp_path):
    conn, _, _, _ = _build(tmp_path)
    techs = conn.execute(
        "SELECT id, joined_on, hourly_wage_cents, employment_status, "
        "availability_status FROM _technicians ORDER BY id"
    ).fetchall()
    assert [t["id"] for t in techs] == [1, 2, 3, 4]
    for t in techs:
        if t["id"] == 2:
            assert "2026-04-01" <= t["joined_on"] <= "2026-09-30"
            assert t["joined_on"] == "2026-08-03"
        else:
            assert t["joined_on"] < "2026-04-01"
    assert [t["employment_status"] for t in techs] == ["active"] * 4
    assert [t["availability_status"] for t in techs] == ["available"] * 4
    assert [t["hourly_wage_cents"] for t in techs] == [3500, 4000, 3200, 3400]
    quals: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT technician_id, skill_code FROM _technician_skills ORDER BY id"
    ):
        quals.setdefault(row["technician_id"], []).append(row["skill_code"])
    assert quals == {
        1: ["hvac_service"],
        2: ["hvac_service", "hvac_install"],
        3: ["plumbing"],
        4: ["electrical"],
    }
    rotations = conn.execute(
        "SELECT id, business_unit_id, technician_id, status, effective_start_date, "
        "vacated_at FROM _on_call_rotations ORDER BY id"
    ).fetchall()
    assert len(rotations) == 3
    assert [r["status"] for r in rotations] == ["active"] * 3
    assert [r["effective_start_date"] for r in rotations] == ["2026-04-01"] * 3
    assert all(r["vacated_at"] is None for r in rotations)
    bu_skill = {1: "hvac_service", 2: "plumbing", 3: "electrical"}
    holder_skills = {t: set(qs) for t, qs in quals.items()}
    for r in rotations:
        assert bu_skill[r["business_unit_id"]] in holder_skills[r["technician_id"]]

    weekday = date(2026, 10, 5)
    weekend = date(2026, 10, 3)
    office = {
        r["technician_id"]
        for r in conn.execute(
            "SELECT technician_id FROM _technician_shifts "
            "WHERE service_date = ? AND shift_type = 'office'",
            (weekday.isoformat(),),
        )
    }
    assert office == {1, 2, 3, 4}
    after = conn.execute(
        "SELECT technician_id, on_call_rotation_id FROM _technician_shifts "
        "WHERE service_date = ? AND shift_type = 'after_hours'",
        (weekday.isoformat(),),
    ).fetchall()
    assert {(r["technician_id"], r["on_call_rotation_id"]) for r in after} == {
        (1, 1),
        (3, 2),
        (4, 3),
    }
    wknd = conn.execute(
        "SELECT technician_id, on_call_rotation_id FROM _technician_shifts "
        "WHERE service_date = ? AND shift_type = 'weekend'",
        (weekend.isoformat(),),
    ).fetchall()
    assert {(r["technician_id"], r["on_call_rotation_id"]) for r in wknd} == {
        (1, 1),
        (3, 2),
        (4, 3),
    }
    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM _technician_shifts "
            "WHERE service_date = ? AND shift_type = 'office'",
            (weekend.isoformat(),),
        ).fetchone()["c"]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM _technician_shifts "
            "WHERE technician_id = 2 AND shift_type IN ('after_hours','weekend')"
        ).fetchone()["c"]
        == 0
    )


def test_ws05_rebuilds_are_byte_identical_and_seed_independent(tmp_path):
    conn1, rid1, _, _ = _build(tmp_path, seed=1)
    conn2, rid2, _, _ = _build(tmp_path, seed=2)
    conn3, rid3, _, _ = _build(tmp_path, seed=1)
    assert _dump.dump_hash(conn1, rid1) == _dump.dump_hash(conn3, rid3)
    tables1 = _dump.dump(conn1, rid1)["tables"]
    tables2 = _dump.dump(conn2, rid2)["tables"]
    tables1.pop("_run")
    tables2.pop("_run")
    assert tables1 == tables2


def test_ws08_private_state_exists_and_is_hidden(tmp_path):
    conn, _, _, db_path = _build(tmp_path)
    assert conn.execute("SELECT COUNT(*) c FROM _technician_true_skill").fetchone()["c"] == 5
    assert conn.execute("SELECT COUNT(*) c FROM _technician_strain").fetchone()["c"] == 4

    for spec in VIEWS:
        names = {c.name for c in spec.columns}
        assert "base_skill" not in names
        assert "training_gain" not in names
        assert "strain_value" not in names

    sess = open_agent_session(db_path)
    register_conn(sess)
    for sql in ("SELECT * FROM _technician_true_skill", "SELECT * FROM _technician_strain"):
        with pytest.raises(EngineError) as ei:
            execute_gated(sess, sql)
        assert ei.value.code is ErrorCode.UNAUTHORIZED
        assert ei.value.to_response() == _UNAUTHORIZED_RESPONSE


def test_ws09_fixture_form_views(tmp_path):
    conn, _, _, _ = _build(tmp_path)
    assert conn.execute("SELECT COUNT(*) c FROM hiring_requests").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM on_call_rotations").fetchone()["c"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM ledger_entries").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM training_rates").fetchone()["c"] == 0

    registry = {spec.name: spec for spec in VIEWS}
    for view_name in (
        "hiring_requests",
        "on_call_rotations",
        "ledger_entries",
        "reviews",
        "training_rates",
    ):
        declared = [c.name for c in registry[view_name].columns]
        stored = [row[1] for row in conn.execute(f"PRAGMA table_info({view_name})")]
        assert stored == declared

    hr = conn.execute("SELECT * FROM hiring_requests").fetchone()
    assert hr["status"] == "completed"
    assert json.loads(hr["requested_skill_codes_json"]) == ["hvac_service", "hvac_install"]
    assert hr["minimum_experience_years"] == 3
    assert hr["preferred_home_zone_id"] == 3
    assert hr["committed_start_date"] == "2026-08-03"
    assert hr["committed_hourly_wage_cents"] == 4000
    assert hr["recruiting_cost_cents"] == 40000
    assert hr["technician_id"] == 2
    assert hr["requested_at"] == clock.phoenix_instant(2026, 7, 1, 9, 0)


def test_acc12_single_opening_adjustment_pins_cash(tmp_path):
    conn, _, cfg, _ = _build(tmp_path)
    rows = conn.execute(
        "SELECT entry_type, amount_cents, balance_after_cents, occurred_at, service_date "
        "FROM _ledger_entries ORDER BY id"
    ).fetchall()
    assert [r["entry_type"] for r in rows] == ["recruiting_cost", "opening_balance_adjustment"]
    assert (
        conn.execute(
            "SELECT COUNT(*) c FROM _ledger_entries "
            "WHERE entry_type = 'opening_balance_adjustment'"
        ).fetchone()["c"]
        == 1
    )
    pin = rows[1]
    assert pin["occurred_at"] == clock.phoenix_instant(2026, 9, 30, 23, 59) + 59_000
    assert pin["service_date"] == "2026-09-30"
    running = 0
    for r in rows:
        running += r["amount_cents"]
        assert r["balance_after_cents"] == running
    starting_cash = cfg.get_int("finance.starting_cash")
    assert running == starting_cash
    assert rows[-1]["balance_after_cents"] == starting_cash
    assert Ledger(conn).cash_balance() == starting_cash


def test_shift_counts_across_run_calendar(tmp_path):
    conn, _, cfg, _ = _build(tmp_path)
    d0 = date.fromisoformat(cfg.get_string("run.start_date"))
    d1 = date.fromisoformat(cfg.get_string("run.end_date"))
    weekdays = 0
    weekends = 0
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            weekdays += 1
        else:
            weekends += 1
        d += timedelta(days=1)
    rows = conn.execute(
        "SELECT shift_type, start_at, end_at, on_call_rotation_id FROM _technician_shifts"
    ).fetchall()
    office = [r for r in rows if r["shift_type"] == "office"]
    after = [r for r in rows if r["shift_type"] == "after_hours"]
    weekend = [r for r in rows if r["shift_type"] == "weekend"]
    assert len(office) == 4 * weekdays
    assert len(after) == 3 * weekdays
    assert len(weekend) == 3 * weekends
    assert len(rows) == 4 * weekdays + 3 * weekdays + 3 * weekends
    for r in office:
        dt = clock.phoenix_local(r["start_at"])
        assert (dt.hour, dt.minute) == (7, 0) and dt.weekday() < 5
        assert r["end_at"] - r["start_at"] == 10 * 3_600_000
        assert r["on_call_rotation_id"] is None
    for r in after:
        dt = clock.phoenix_local(r["start_at"])
        assert (dt.hour, dt.minute) == (17, 0) and dt.weekday() < 5
        assert r["end_at"] - r["start_at"] == 2 * 3_600_000
        assert r["on_call_rotation_id"] is not None
    for r in weekend:
        dt = clock.phoenix_local(r["start_at"])
        assert (dt.hour, dt.minute) == (7, 0) and dt.weekday() >= 5
        assert r["end_at"] - r["start_at"] == 12 * 3_600_000
        assert r["on_call_rotation_id"] is not None


def test_run_record_and_clock_seeded_at_opening_instant(tmp_path):
    conn, run_id, _, _ = _build(tmp_path, seed=42)
    opening = clock.phoenix_instant(2026, 10, 1, 6, 45)
    row = conn.execute(
        "SELECT current_time_ms, service_date FROM _clock WHERE singleton = 1"
    ).fetchone()
    assert row["current_time_ms"] == opening
    assert row["service_date"] == "2026-10-01"
    run = get_run(conn, run_id)
    assert run["start_at_ms"] == opening
    assert run["first_service_date"] == "2026-10-01"
    assert run["last_service_date"] == "2026-10-14"
    assert run["timezone"] == "America/Phoenix"
    assert run["master_seed"] == 42
    assert RunStatus(run["status"]) is RunStatus.ACTIVE


def test_scenario_set_run_window_builds_the_whole_run_around_it(tmp_path):
    scenario = json.loads(geo_scenario_json())
    scenario["keys"]["run.start_date"] = "2026-11-02"
    scenario["keys"]["run.end_date"] = "2026-11-15"
    scenario["keys"]["weather.precompute_end_date"] = "2026-11-22"
    cfg = load_run_config(scenario_json=json.dumps(scenario))
    conn, run_id, _, _ = _build(tmp_path, config=cfg)

    run = get_run(conn, run_id)
    assert run["first_service_date"] == "2026-11-02"
    assert run["last_service_date"] == "2026-11-15"
    assert run["start_at_ms"] == clock.phoenix_instant(2026, 11, 2, 6, 45)
    assert RunStatus(run["status"]) is RunStatus.ACTIVE
    boundary_check(conn, cfg)

    bounds = conn.execute(
        "SELECT MIN(service_date) lo, MAX(service_date) hi FROM _technician_shifts"
    ).fetchone()
    assert (bounds["lo"], bounds["hi"]) == ("2026-11-02", "2026-11-15")
    weekdays = 0
    weekends = 0
    d = date.fromisoformat("2026-11-02")
    while d <= date.fromisoformat("2026-11-15"):
        if d.weekday() < 5:
            weekdays += 1
        else:
            weekends += 1
        d += timedelta(days=1)
    total = conn.execute("SELECT COUNT(*) c FROM _technician_shifts").fetchone()["c"]
    assert total == 4 * weekdays + 3 * weekdays + 3 * weekends


def test_warm_start_refuses_non_initializing_status(tmp_path):
    conn, run_id, cfg, _ = _build(tmp_path)
    with pytest.raises(EngineError) as ei:
        warm_start.run(conn, run_id, 1, cfg, "", artifact=None)
    assert ei.value.code is ErrorCode.INVALID_RUN_STATUS


def test_warm_start_refuses_missing_run(tmp_path):
    cfg = load_run_config()
    empty = in_memory()
    register_conn(empty)
    migrate(empty)
    with pytest.raises(EngineError) as ei:
        warm_start.run(empty, "no-such-run", 1, cfg, "", artifact=None)
    assert ei.value.code is ErrorCode.INVALID_RUN_STATUS


def test_warm_start_refuses_missing_artifact(tmp_path):
    db_path = tmp_path / "no-artifact.db"
    conn = open_run_db(db_path)
    register_conn(conn)
    run_id = db.new_run_id()
    cfg = load_run_config()
    db.begin(conn)
    db.insert_run(
        conn,
        run_id=run_id,
        spec_version="1.0.0",
        config_version=cfg.config_version,
        config_hash=cfg.config_hash,
        master_seed=1,
        start_at_ms=0,
        first_service_date="2026-10-01",
        last_service_date="2027-09-30",
        timezone="America/Phoenix",
        status=RunStatus.INITIALIZING,
    )
    db.commit(conn)
    with pytest.raises(EngineError) as ei:
        warm_start.run(conn, run_id, 1, cfg, "", artifact=None)
    assert ei.value.code is ErrorCode.INVARIANT_VIOLATED
