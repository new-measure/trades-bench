"""WS-01/02/05/08/09, ACC-12, and the fixture-shape tests through the real
``run_init`` path (spec 02 §8, spec 09; CSR-002 decisions 10-12).

Every test builds a fresh run database on a tmp path via
:func:`csr_bench.run_init.initialize_run` and reopens it through
:func:`csr_bench.run_init.open_run_db` — the same open sequence a resumed
process uses — so the fixture is exercised through the real machinery, exactly
as decision 12 requires.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest

from _helpers import register_conn
from csr_bench import _dump, clock, warm_start
from csr_bench.config import load_run_config
from csr_bench.db import get_run, in_memory
from csr_bench.enums import RunStatus
from csr_bench.errors import EngineError, ErrorCode
from csr_bench.ledger import Ledger
from csr_bench.migrations import migrate
from csr_bench.run_init import boundary_check, initialize_run, open_run_db
from csr_bench.sql_gate import execute_gated, open_agent_session
from csr_bench.views import VIEWS

#: The one uniform error every gate denial must surface, byte for byte (VIS-05).
_UNAUTHORIZED_RESPONSE = EngineError.unauthorized().to_response()


def _build(tmp_path, seed=1, config=None):
    """A fresh run on a tmp path through the real init sequence.

    Returns ``(conn, run_id, config, db_path)``; the reopened connection is
    registered with the autouse closer so it never leaks.
    """
    cfg = config if config is not None else load_run_config()
    db_path = tmp_path / f"run-{seed}-{uuid.uuid4().hex[:8]}.db"
    run_id = initialize_run(db_path, master_seed=seed, config=cfg)
    conn = open_run_db(db_path)
    register_conn(conn)
    return conn, run_id, cfg, db_path


# ---------------------------------------------------------------------------
# WS-01 — clean boundary through the real init sequence (decision 12)
# ---------------------------------------------------------------------------


def test_ws01_clean_boundary(tmp_path):
    conn, run_id, cfg, _ = _build(tmp_path)
    boundary_check(conn, cfg)  # must not raise
    assert conn.execute("SELECT COUNT(*) c FROM _calls").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM _appointments").fetchone()["c"] == 0
    assert get_run(conn, run_id)["status"] == "active"


# ---------------------------------------------------------------------------
# WS-02 — roster and coverage (spec 02 §8.4)
# ---------------------------------------------------------------------------


def test_ws02_roster_and_coverage(tmp_path):
    conn, _, _, _ = _build(tmp_path)
    techs = conn.execute(
        "SELECT id, joined_on, hourly_wage_cents, employment_status, "
        "availability_status FROM _technicians ORDER BY id"
    ).fetchall()
    assert [t["id"] for t in techs] == [1, 2, 3, 4]
    # spec 02 §8.4: exactly one joined inside the window — the recent hire
    # (constants.recent_hire_technician_id, tech 2) — the other three before
    # 2026-04-01
    for t in techs:
        if t["id"] == 2:
            assert "2026-04-01" <= t["joined_on"] <= "2026-09-30"
            assert t["joined_on"] == "2026-08-03"
        else:
            assert t["joined_on"] < "2026-04-01"
    # all four are active and available at the boundary
    assert [t["employment_status"] for t in techs] == ["active"] * 4
    assert [t["availability_status"] for t in techs] == ["available"] * 4
    # wages: the per-trade base; tech 2 (multi-skill) gets the higher of his
    # two trades' bases (decision 11, documented placeholder for CSR-007)
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
    # effective from the window start, never vacated (spec 02 §8.4)
    assert [r["effective_start_date"] for r in rotations] == ["2026-04-01"] * 3
    assert all(r["vacated_at"] is None for r in rotations)
    # every rotation is held by a technician qualified for its business unit
    bu_skill = {1: "hvac_service", 2: "plumbing", 3: "electrical"}
    holder_skills = {t: set(qs) for t, qs in quals.items()}
    for r in rotations:
        assert bu_skill[r["business_unit_id"]] in holder_skills[r["technician_id"]]

    # sampled weekday (Mon 2026-10-05) and weekend day (Sat 2026-10-03):
    # every technician has an office shift on weekdays; the three rotation
    # holders have after-hours (weekday) and weekend shifts carrying their
    # rotation id; the non-holder (tech 2) has none of those.
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
    # office is weekday-only and off-hours never touches the non-holder
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


# ---------------------------------------------------------------------------
# WS-05 — determinism: two builds of the same inputs are byte-identical, and
# the fixture itself is seed-independent (decision 11)
# ---------------------------------------------------------------------------


def test_ws05_rebuilds_are_byte_identical_and_seed_independent(tmp_path):
    conn1, rid1, _, _ = _build(tmp_path, seed=1)
    conn2, rid2, _, _ = _build(tmp_path, seed=2)
    conn3, rid3, _, _ = _build(tmp_path, seed=1)
    # two fresh builds of the same inputs: identical logical record (spec 02 §8.1)
    assert _dump.dump_hash(conn1, rid1) == _dump.dump_hash(conn3, rid3)
    # across seeds the fixture is identical too — the only differing row is
    # the run record's own master_seed, which is run identity metadata, not
    # warm-start content (the dump normalizes run_id, spec 09 §2)
    tables1 = _dump.dump(conn1, rid1)["tables"]
    tables2 = _dump.dump(conn2, rid2)["tables"]
    tables1.pop("_run")
    tables2.pop("_run")
    assert tables1 == tables2


# ---------------------------------------------------------------------------
# WS-08 — hidden private state exists and never leaks (spec 04 §1.5)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# WS-09 — CSR-Bench-specific fixture form (decision 11)
# ---------------------------------------------------------------------------


def test_ws09_fixture_form_views(tmp_path):
    conn, _, _, _ = _build(tmp_path)
    assert conn.execute("SELECT COUNT(*) c FROM hiring_requests").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM on_call_rotations").fetchone()["c"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM ledger_entries").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM training_rates").fetchone()["c"] == 0

    # the stored views' columns match the registry's declared columns
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

    # the one completed hiring request carries the recent hire's committed terms
    hr = conn.execute("SELECT * FROM hiring_requests").fetchone()
    assert hr["status"] == "completed"
    assert json.loads(hr["requested_skill_codes_json"]) == ["hvac_service", "hvac_install"]
    assert hr["minimum_experience_years"] == 3
    assert hr["preferred_home_zone_id"] == 3
    assert hr["committed_start_date"] == "2026-08-03"
    assert hr["committed_hourly_wage_cents"] == 4000
    assert hr["recruiting_cost_cents"] == 40000
    assert hr["technician_id"] == 2
    # the request preceded the committed start by the fixed lead time
    assert hr["requested_at"] == clock.phoenix_instant(2026, 7, 1, 9, 0)


# ---------------------------------------------------------------------------
# ACC-12 — one opening-balance adjustment; exact running-balance chain
# (decision 9, spec 02 §8.7)
# ---------------------------------------------------------------------------


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
    # balance_after_cents is the exact running total at every row (ACC-01)
    running = 0
    for r in rows:
        running += r["amount_cents"]
        assert r["balance_after_cents"] == running
    starting_cash = cfg.get_int("finance.starting_cash")
    assert running == starting_cash
    assert rows[-1]["balance_after_cents"] == starting_cash
    assert Ledger(conn).cash_balance() == starting_cash


# ---------------------------------------------------------------------------
# Shift materialization counts and shapes (spec 02 §8.8 / spec 04 §9.3)
# ---------------------------------------------------------------------------


def test_shift_counts_across_run_calendar(tmp_path):
    conn, _, _, _ = _build(tmp_path)
    d0 = date.fromisoformat("2026-10-01")
    d1 = date.fromisoformat("2027-09-30")
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
    # every row lands on the right Phoenix-local day and window
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


# ---------------------------------------------------------------------------
# Run record and clock seeding (decision 12)
# ---------------------------------------------------------------------------


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
    assert run["last_service_date"] == "2027-09-30"
    assert run["timezone"] == "America/Phoenix"
    assert run["master_seed"] == 42
    assert RunStatus(run["status"]) is RunStatus.ACTIVE


# ---------------------------------------------------------------------------
# The loader guard (spec 02 §8.1: runs only while status = 'initializing')
# ---------------------------------------------------------------------------


def test_warm_start_refuses_non_initializing_status(tmp_path):
    conn, run_id, cfg, _ = _build(tmp_path)  # status is now 'active'
    with pytest.raises(EngineError) as ei:
        warm_start.run(conn, run_id, 1, cfg, "")
    assert ei.value.code is ErrorCode.INVALID_RUN_STATUS


def test_warm_start_refuses_missing_run(tmp_path):
    cfg = load_run_config()
    empty = in_memory()
    register_conn(empty)
    migrate(empty)
    with pytest.raises(EngineError) as ei:
        warm_start.run(empty, "no-such-run", 1, cfg, "")
    assert ei.value.code is ErrorCode.INVALID_RUN_STATUS
