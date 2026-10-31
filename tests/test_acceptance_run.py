from __future__ import annotations

import json
import shutil
from datetime import date, timedelta

import pytest

from _adapters import ScriptedNoOpAdapter, SnapshotOnNthPromptAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
    register_conn,
)
from trades_bench import clock, constants
from trades_bench._dump import dump_hash
from trades_bench.config import load_run_config
from trades_bench.controller import Controller
from trades_bench.enums import EventKind, RunStatus
from trades_bench.events import Events
from trades_bench.run_init import initialize_run, open_run_db
from trades_bench.txn import privileged, privileged_request_id

SEED = 5
FULL_START_DATE = "2026-10-01"
FULL_END_DATE = "2026-10-14"
BANKRUPTCY_DATE = "2026-10-12"
FULL_DATES = tuple(
    (date.fromisoformat(FULL_START_DATE) + timedelta(days=offset)).isoformat()
    for offset in range(12)
)
FAST_END_DATE = "2026-10-03"
STARTING_CASH = 4_200_000
FINAL_CASH = -80_000
RESUME_SAMPLE_COUNT = 30

NO_OP_FIXTURE_ECONOMICS = {
    "demand.base_new_caller_rate_per_day": 2,
    "finance.daily_operating_overhead": 220_000,
    "finance.starting_cash": STARTING_CASH,
    "payroll.off_hours_bonus_per_job": 125_000,
}

DAY_ONE_TRACE = [
    ("day_rollover", "2026-10-01T06:45:00-07:00", 100),
    ("employment_transition", "2026-10-01T06:45:00-07:00", 110),
    ("weather_realized", "2026-10-01T06:45:00-07:00", 120),
    ("labor_economy_update", "2026-10-01T06:45:00-07:00", 130),
    ("sickness_draw", "2026-10-01T06:45:00-07:00", 140),
    ("attrition_draw", "2026-10-01T06:45:00-07:00", 150),
    ("training_rates_drawn", "2026-10-01T06:45:00-07:00", 160),
    ("shift_materialization", "2026-10-01T06:45:00-07:00", 170),
    ("demand_generation", "2026-10-01T06:45:00-07:00", 180),
    ("opening_invocation", "2026-10-01T06:45:00-07:00", 900),
    ("first_block_lock", "2026-10-01T06:45:00-07:00", 905),
    ("first_trip_dispatch", "2026-10-01T06:45:00-07:00", 906),
    ("block_lock", "2026-10-01T09:00:00-07:00", 200),
    ("call_invocation", "2026-10-01T10:54:00-07:00", 910),
    ("block_lock", "2026-10-01T11:00:00-07:00", 200),
    ("block_lock", "2026-10-01T13:00:00-07:00", 200),
    ("call_invocation", "2026-10-01T14:46:00-07:00", 910),
    ("block_lock", "2026-10-01T15:00:00-07:00", 200),
    ("block_lock", "2026-10-01T17:00:00-07:00", 200),
    ("closing_invocation", "2026-10-01T19:00:00-07:00", 920),
    ("daily_settlement", "2026-10-01T19:00:00-07:00", 950),
    ("checkpoint", "2026-10-01T19:00:00-07:00", 960),
]


def _config(**overrides):
    keys = {
        **NO_OP_FIXTURE_ECONOMICS,
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        **overrides,
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def _init(path, config, *, seed=SEED):
    initialize_run(path, master_seed=seed, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    return open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)


def _ms(service_date, hour, minute):
    d = date.fromisoformat(service_date)
    return clock.phoenix_instant(d.year, d.month, d.day, hour, minute)


def _run_id(conn):
    return conn.execute("SELECT run_id FROM _run").fetchone()["run_id"]


def _run_counting_steps(conn, config):
    adapter = ScriptedNoOpAdapter()
    controller = Controller(conn, config, adapter)
    steps = 0
    while controller.step():
        steps += 1
    return controller.status(), steps, len(adapter.prompts)


@pytest.fixture(scope="module")
def full_run(tmp_path_factory):
    path = tmp_path_factory.mktemp("acceptance_full") / "run.db"
    config = _config()
    conn = _init(path, config)
    status, steps, prompts = _run_counting_steps(conn, config)
    run_id = _run_id(conn)
    yield {
        "conn": conn,
        "config": config,
        "status": status,
        "run_id": run_id,
        "steps": steps,
        "prompts": prompts,
        "dump": dump_hash(conn, run_id),
    }
    conn.close()


@pytest.fixture(scope="module")
def fast_baseline(tmp_path_factory):
    path = tmp_path_factory.mktemp("acceptance_fast") / "run.db"
    config = _config(**{"run.end_date": FAST_END_DATE})
    conn = _init(path, config)
    status, steps, prompts = _run_counting_steps(conn, config)
    assert status is RunStatus.COMPLETED
    baseline = {
        "config": config,
        "steps": steps,
        "prompts": prompts,
        "dump": dump_hash(conn, _run_id(conn)),
    }
    conn.close()
    return baseline


def test_no_op_bankruptcy_stops_the_run_after_the_causing_settlement(full_run):
    conn = full_run["conn"]
    assert full_run["status"] is RunStatus.BANKRUPT
    run_row = conn.execute("SELECT * FROM _run").fetchone()
    assert run_row["status"] == "bankrupt"
    assert run_row["first_service_date"] == FULL_START_DATE
    assert run_row["last_service_date"] == FULL_END_DATE
    days = conn.execute("SELECT * FROM _day_state ORDER BY service_date").fetchall()
    assert [row["service_date"] for row in days] == list(FULL_DATES)
    day_types = [row["day_type"] for row in days]
    assert day_types.count("weekday") == 8
    assert day_types.count("weekend") == 4
    for row in days:
        weekday = date.fromisoformat(row["service_date"]).isoweekday() <= 5
        assert row["day_type"] == ("weekday" if weekday else "weekend")
        assert row["opening_at"] == _ms(row["service_date"], 6, 45)
        assert row["closing_at"] == _ms(row["service_date"], 19, 0)
        assert row["settlement_status"] == "settled"
        assert row["unresolved_execution_count"] == 0
    for service_date in FULL_DATES:
        day_start = _ms(service_date, 0, 0)
        day_end = day_start + 86_400_000
        for kind in ("opening_invocation", "closing_invocation", "daily_settlement", "checkpoint"):
            count = conn.execute(
                "SELECT COUNT(*) c FROM _events WHERE kind = ? AND status = 'applied' "
                "AND scheduled_at_ms >= ? AND scheduled_at_ms < ?",
                (kind, day_start, day_end),
            ).fetchone()["c"]
            expected = 0 if service_date == BANKRUPTCY_DATE and kind == "checkpoint" else 1
            assert count == expected, (service_date, kind)
    calls = conn.execute(
        "SELECT ts_ms, status FROM _invocations WHERE type = 'call' ORDER BY sequence"
    ).fetchall()
    for row in calls:
        local = clock.phoenix_local(row["ts_ms"])
        minutes = local.hour * 60 + local.minute
        assert constants.CALL_WINDOW_START_MIN <= minutes < constants.CALL_WINDOW_END_MIN
        assert clock.service_date_for_ms(row["ts_ms"]) in FULL_DATES
        assert row["status"] == "completed"
    per_type = {
        row["type"]: row["c"]
        for row in conn.execute("SELECT type, COUNT(*) c FROM _invocations GROUP BY type")
    }
    assert per_type["opening"] == 12
    assert per_type["closing"] == 12
    assert conn.execute(
        "SELECT COUNT(*) c FROM _invocations WHERE status != 'completed'"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = 'run_finalize' AND status = 'applied'"
    ).fetchone()["c"] == 0
    pending = conn.execute(
        "SELECT kind, scheduled_at_ms FROM _events WHERE status = 'pending' ORDER BY event_id"
    ).fetchall()
    assert [(row["kind"], clock.service_date_for_ms(row["scheduled_at_ms"])) for row in pending] == [
        ("checkpoint", BANKRUPTCY_DATE)
    ]


def test_final_cash_retains_the_negative_bankruptcy_causing_balance(full_run):
    conn = full_run["conn"]
    config = full_run["config"]
    starting = config.get_int("finance.starting_cash")
    assert starting == STARTING_CASH
    wages = [
        int(row["hourly_wage_cents"])
        for row in conn.execute("SELECT hourly_wage_cents FROM _technicians ORDER BY id")
    ]
    assert len(wages) == 4
    daily_payroll = constants.WEEKDAY_PAY_MULTIPLIER * sum(wages)
    overhead = config.get_int("finance.daily_operating_overhead")
    assert starting - 8 * daily_payroll - 12 * overhead == FINAL_CASH
    balance = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM _ledger_entries"
    ).fetchone()["s"]
    assert balance == FINAL_CASH
    assert balance < 0
    bankruptcy = conn.execute(
        "SELECT service_date, balance_after_cents FROM _ledger_entries "
        "WHERE origin = 'run' AND balance_after_cents < 0 ORDER BY occurred_at, id LIMIT 1"
    ).fetchone()
    assert bankruptcy["service_date"] == BANKRUPTCY_DATE
    assert bankruptcy["balance_after_cents"] == FINAL_CASH


def test_every_ledger_row_carries_the_running_balance(full_run):
    conn = full_run["conn"]
    rows = conn.execute(
        "SELECT amount_cents, balance_after_cents, occurred_at FROM _ledger_entries ORDER BY id"
    ).fetchall()
    assert rows
    running = 0
    previous_occurred_at = None
    for row in rows:
        running += row["amount_cents"]
        assert row["balance_after_cents"] == running
        if previous_occurred_at is not None:
            assert row["occurred_at"] >= previous_occurred_at
        previous_occurred_at = row["occurred_at"]
    balance = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM _ledger_entries"
    ).fetchone()["s"]
    assert running == balance
    assert running == FINAL_CASH


def test_day_one_of_the_two_week_run_replays_the_pinned_trace(full_run):
    conn = full_run["conn"]
    rows = conn.execute(
        "SELECT kind, scheduled_at_ms, priority FROM _events WHERE status = 'applied' "
        "AND scheduled_at_ms >= ? AND scheduled_at_ms < ? "
        "ORDER BY scheduled_at_ms, priority, order_key, event_id",
        (_ms(FULL_DATES[0], 0, 0), _ms(FULL_DATES[1], 0, 0)),
    ).fetchall()
    trace = [
        (row["kind"], clock.to_iso8601(row["scheduled_at_ms"]), row["priority"]) for row in rows
    ]
    assert trace == DAY_ONE_TRACE


def test_a_fresh_two_week_run_from_the_same_seed_matches_the_baseline_dump(full_run, tmp_path):
    config = _config()
    conn = _init(tmp_path / "twin.db", config)
    register_conn(conn)
    status = Controller(conn, config, ScriptedNoOpAdapter()).run()
    assert status is RunStatus.BANKRUPT
    assert dump_hash(conn, _run_id(conn)) == full_run["dump"]


def _kill_copy_resume_and_compare(
    tmp_path, config, boundary, expected_dump, expected_status=RunStatus.COMPLETED
):
    killed_path = tmp_path / f"killed_{boundary}.db"
    conn = _init(killed_path, config)
    try:
        status = Controller(conn, config, ScriptedNoOpAdapter()).run(max_steps=boundary)
        assert status is RunStatus.ACTIVE, boundary
    finally:
        conn.close()
    resumed_path = tmp_path / f"resumed_{boundary}.db"
    shutil.copyfile(killed_path, resumed_path)
    resumed = open_run_db(resumed_path, artifact_dir=GEO_ARTIFACT_DIR)
    try:
        status = Controller(resumed, config, ScriptedNoOpAdapter()).run()
        assert status is expected_status, boundary
        assert dump_hash(resumed, _run_id(resumed)) == expected_dump, boundary
    finally:
        resumed.close()
    killed_path.unlink()
    resumed_path.unlink()


@pytest.mark.parametrize("slot", range(1, RESUME_SAMPLE_COUNT + 1))
def test_sampled_boundary_kills_resume_to_the_identical_dump(fast_baseline, tmp_path, slot):
    total = fast_baseline["steps"]
    boundary = max(1, slot * (total - 1) // RESUME_SAMPLE_COUNT)
    _kill_copy_resume_and_compare(
        tmp_path, fast_baseline["config"], boundary, fast_baseline["dump"]
    )


def _snapshot_resume_and_compare(
    tmp_path, config, prompt_number, expected_dump, expected_status=RunStatus.COMPLETED
):
    live_path = tmp_path / f"live_{prompt_number}.db"
    snapshot_path = tmp_path / f"snapshot_{prompt_number}.db"
    conn = _init(live_path, config)
    try:
        adapter = SnapshotOnNthPromptAdapter(prompt_number, live_path, snapshot_path)
        assert Controller(conn, config, adapter).run() is expected_status, prompt_number
        assert adapter.snapshot_prompt is not None, prompt_number
    finally:
        conn.close()
    resumed = open_run_db(snapshot_path, artifact_dir=GEO_ARTIFACT_DIR)
    try:
        active = resumed.execute(
            "SELECT COUNT(*) c FROM _invocations WHERE status = 'active'"
        ).fetchone()["c"]
        assert active == 1, prompt_number
        status = Controller(resumed, config, ScriptedNoOpAdapter()).run()
        assert status is expected_status, prompt_number
        assert dump_hash(resumed, _run_id(resumed)) == expected_dump, prompt_number
    finally:
        resumed.close()
    live_path.unlink()
    snapshot_path.unlink()


@pytest.mark.parametrize("slot", range(1, RESUME_SAMPLE_COUNT + 1))
def test_sampled_mid_prompt_snapshots_resume_to_the_identical_dump(fast_baseline, tmp_path, slot):
    total = fast_baseline["prompts"]
    prompt_number = max(1, slot * total // RESUME_SAMPLE_COUNT)
    _snapshot_resume_and_compare(
        tmp_path, fast_baseline["config"], prompt_number, fast_baseline["dump"]
    )


@pytest.mark.nightly
def test_every_boundary_of_the_two_week_run_resumes_to_the_identical_dump(full_run, tmp_path):
    config = full_run["config"]
    for boundary in range(1, full_run["steps"]):
        _kill_copy_resume_and_compare(
            tmp_path, config, boundary, full_run["dump"], RunStatus.BANKRUPT
        )
    for prompt_number in range(1, full_run["prompts"] + 1):
        _snapshot_resume_and_compare(
            tmp_path, config, prompt_number, full_run["dump"], RunStatus.BANKRUPT
        )


class InterleavingProbeAdapter(ScriptedNoOpAdapter):

    def __init__(self):
        super().__init__()
        self.controller = None
        self.injected_event_ids = []
        self.active_count_during_send = None
        self.injected_statuses_during_send = None

    def send(self, prompt):
        if prompt.kind.value == "call" and not self.injected_event_ids:
            controller = self.controller
            ts = prompt.ts_ms

            def fn(ctx):
                events = Events(ctx.conn, ctx.rng)
                first = events.schedule(EventKind.EMPLOYMENT_TRANSITION, ts)
                second = events.schedule(EventKind.FIRST_TRIP_DISPATCH, ts)
                return {"first": first, "second": second}

            body = json.loads(
                privileged(controller.services, privileged_request_id("probe_schedule", ts), fn)
            )
            assert body["ok"]
            self.injected_event_ids = [body["result"]["first"], body["result"]["second"]]
            self.active_count_during_send = controller.conn.execute(
                "SELECT COUNT(*) c FROM _invocations WHERE status = 'active'"
            ).fetchone()["c"]
            placeholders = ", ".join("?" for _ in self.injected_event_ids)
            self.injected_statuses_during_send = [
                row["status"]
                for row in controller.conn.execute(
                    f"SELECT status FROM _events WHERE event_id IN ({placeholders}) "
                    "ORDER BY event_id",
                    self.injected_event_ids,
                )
            ]
        return super().send(prompt)


def test_an_event_created_inside_an_invocation_applies_only_after_it_completes(tmp_path):
    config = _config(**{"run.end_date": FAST_END_DATE})
    conn = _init(tmp_path / "probe.db", config)
    register_conn(conn)
    adapter = InterleavingProbeAdapter()
    controller = Controller(conn, config, adapter)
    adapter.controller = controller

    def applied_ids():
        return {
            row["event_id"]
            for row in conn.execute("SELECT event_id FROM _events WHERE status = 'applied'")
        }

    def invocation_count():
        return conn.execute("SELECT COUNT(*) c FROM _invocations").fetchone()["c"]

    step_records = []
    previous_applied = applied_ids()
    previous_invocations = invocation_count()
    previous_clock = clock.now(conn)
    probe_step = None
    while controller.step():
        current_applied = applied_ids()
        current_invocations = invocation_count()
        current_clock = clock.now(conn)
        assert current_clock >= previous_clock
        if current_clock != previous_clock:
            assert current_applied != previous_applied or current_invocations != previous_invocations
        step_records.append(current_applied - previous_applied)
        if probe_step is None and adapter.injected_event_ids:
            probe_step = len(step_records) - 1
        previous_applied = current_applied
        previous_invocations = current_invocations
        previous_clock = current_clock
    assert controller.status() is RunStatus.COMPLETED
    assert adapter.injected_event_ids
    assert adapter.active_count_during_send == 1
    assert adapter.injected_statuses_during_send == ["pending", "pending"]
    first_id, second_id = adapter.injected_event_ids
    first_step = next(i for i, new in enumerate(step_records) if first_id in new)
    second_step = next(i for i, new in enumerate(step_records) if second_id in new)
    assert first_step == probe_step + 1
    assert second_step == probe_step + 2
    assert conn.execute(
        "SELECT COUNT(*) c FROM _invocations WHERE status = 'active'"
    ).fetchone()["c"] == 0
    injected = conn.execute(
        "SELECT event_id, status, scheduled_at_ms, applied_at_ms, priority FROM _events "
        "WHERE event_id IN (?, ?) ORDER BY event_id",
        (first_id, second_id),
    ).fetchall()
    assert [row["status"] for row in injected] == ["applied", "applied"]
    assert injected[0]["scheduled_at_ms"] == injected[1]["scheduled_at_ms"]
    assert injected[0]["priority"] < injected[1]["priority"]
    assert injected[0]["applied_at_ms"] <= injected[1]["applied_at_ms"]
