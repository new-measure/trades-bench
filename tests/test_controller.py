from __future__ import annotations

import json
import shutil
from datetime import date

import pytest

from _adapters import CrashOnNthPromptAdapter, ScriptedNoOpAdapter
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
from trades_bench.controller import BLOCK_CODES, Controller
from trades_bench.enums import EVENT_PRIORITY, EventKind, RunStatus
from trades_bench.run_init import initialize_run, open_run_db

SEED = 5
START_DATE = "2026-10-01"
END_DATE = "2026-10-03"
DATES = ("2026-10-01", "2026-10-02", "2026-10-03")

PRE_OPENING_ORDER = [
    "day_rollover",
    "employment_transition",
    "weather_realized",
    "labor_economy_update",
    "sickness_draw",
    "attrition_draw",
    "training_rates_drawn",
    "shift_materialization",
    "demand_generation",
    "opening_invocation",
    "first_block_lock",
    "first_trip_dispatch",
]


def _config(**overrides):
    keys = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        "run.end_date": END_DATE,
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


def _arrivals(conn, service_date):
    return [
        row["arrived_at"]
        for row in conn.execute(
            "SELECT arrived_at FROM _calls WHERE service_date = ? "
            "ORDER BY arrived_at, id",
            (service_date,),
        )
    ]


@pytest.fixture(scope="module")
def completed_run(tmp_path_factory):
    path = tmp_path_factory.mktemp("controller") / "run.db"
    config = _config()
    conn = _init(path, config)
    adapter = ScriptedNoOpAdapter()
    status = Controller(conn, config, adapter).run()
    yield {
        "conn": conn,
        "config": config,
        "adapter": adapter,
        "status": status,
        "run_id": _run_id(conn),
    }
    conn.close()


def test_day_one_applies_events_in_the_normative_order(completed_run):
    conn = completed_run["conn"]
    rows = conn.execute(
        "SELECT kind, scheduled_at_ms, priority, event_id, applied_at_ms FROM _events "
        "WHERE status = 'applied' AND scheduled_at_ms >= ? AND scheduled_at_ms < ? "
        "ORDER BY scheduled_at_ms, priority, event_id",
        (_ms(DATES[0], 0, 0), _ms(DATES[1], 0, 0)),
    ).fetchall()
    kinds = [row["kind"] for row in rows]
    assert kinds[:12] == PRE_OPENING_ORDER
    assert kinds[-3:] == ["closing_invocation", "daily_settlement", "checkpoint"]
    lock_keys = [
        (_ms(DATES[0], hour, 0), EVENT_PRIORITY[EventKind.BLOCK_LOCK])
        for hour in (9, 11, 13, 15, 17)
    ]
    call_keys = [
        (arrival, EVENT_PRIORITY[EventKind.CALL_INVOCATION])
        for arrival in _arrivals(conn, DATES[0])
    ]
    expected_middle = sorted(lock_keys + call_keys)
    middle = rows[12:-3]
    assert [(row["scheduled_at_ms"], row["priority"]) for row in middle] == expected_middle
    for row in middle:
        expected_kind = (
            "block_lock"
            if row["priority"] == EVENT_PRIORITY[EventKind.BLOCK_LOCK]
            else "call_invocation"
        )
        assert row["kind"] == expected_kind
    applied = [row["applied_at_ms"] for row in rows]
    assert applied == sorted(applied)
    assert conn.execute("SELECT COUNT(*) c FROM _events WHERE status = 'pending'").fetchone()[
        "c"
    ] == 0


def test_day_state_rows_record_openings_closings_and_settlement(completed_run):
    conn = completed_run["conn"]
    rows = conn.execute("SELECT * FROM _day_state ORDER BY service_date").fetchall()
    assert [row["service_date"] for row in rows] == list(DATES)
    for row in rows:
        d = date.fromisoformat(row["service_date"])
        assert row["day_type"] == ("weekday" if d.isoweekday() <= 5 else "weekend")
        assert row["opening_at"] == _ms(row["service_date"], 6, 45)
        assert row["closing_at"] == _ms(row["service_date"], 19, 0)
        assert row["settlement_status"] == "settled"
        assert row["unresolved_execution_count"] == 0
        assert row["last_execution_completed_at"] is None


def test_every_arrived_call_is_permanent_and_drains_its_pending_row(completed_run):
    conn = completed_run["conn"]
    assert conn.execute("SELECT COUNT(*) c FROM _pending_calls").fetchone()["c"] == 0
    arrived = conn.execute(
        "SELECT c.id, c.arrived_at, c.status, h.arrival_event_id "
        "FROM _calls c JOIN _call_hidden h ON h.call_id = c.id "
        "ORDER BY c.arrived_at, c.id"
    ).fetchall()
    assert arrived
    for row in arrived:
        local = clock.phoenix_local(row["arrived_at"])
        minutes = local.hour * 60 + local.minute
        assert constants.CALL_WINDOW_START_MIN <= minutes < constants.CALL_WINDOW_END_MIN
    call_invocations = conn.execute(
        "SELECT ts_ms, status FROM _invocations WHERE type = 'call' ORDER BY sequence"
    ).fetchall()
    assert [row["ts_ms"] for row in call_invocations] == [row["arrived_at"] for row in arrived]
    assert all(row["status"] == "completed" for row in call_invocations)
    applied_call_events = {
        row["event_id"]
        for row in conn.execute(
            "SELECT event_id FROM _events WHERE kind = 'call_invocation' AND status = 'applied'"
        )
    }
    assert applied_call_events == {row["arrival_event_id"] for row in arrived}


def test_adapter_prompts_arrive_in_timestamp_order_with_field_set_payloads(completed_run):
    conn = completed_run["conn"]
    prompts = completed_run["adapter"].prompts
    expected = []
    for service_date in DATES:
        expected.append(("opening", _ms(service_date, 6, 45)))
        expected.extend(("call", arrival) for arrival in _arrivals(conn, service_date))
        expected.append(("closing", _ms(service_date, 19, 0)))
    assert [(prompt.kind.value, prompt.ts_ms) for prompt in prompts] == expected
    timestamps = [prompt.ts_ms for prompt in prompts]
    assert timestamps == sorted(timestamps)
    call_prompts = [prompt for prompt in prompts if prompt.kind.value == "call"]
    for prompt in call_prompts:
        assert set(prompt.payload) == {
            "time", "call_id", "caller", "phone", "address",
            "customer_type", "requested_service", "urgent",
        }
    for prompt in prompts:
        if prompt.kind.value != "call":
            assert set(prompt.payload) == {"service_date", "time"}
    arrived = conn.execute(
        "SELECT c.id, c.caller_name, c.caller_phone, c.segment, "
        "c.requested_service_name, c.urgent, ac.formatted_address "
        "FROM _calls c JOIN _address_catalog ac ON ac.id = c.address_catalog_id "
        "ORDER BY c.arrived_at, c.id"
    ).fetchall()
    assert [
        (
            prompt.payload["call_id"],
            prompt.payload["caller"],
            prompt.payload["phone"],
            prompt.payload["customer_type"],
            prompt.payload["address"],
            prompt.payload["requested_service"],
            prompt.payload["urgent"],
        )
        for prompt in call_prompts
    ] == [
        (
            row["id"],
            row["caller_name"],
            row["caller_phone"],
            row["segment"],
            row["formatted_address"],
            row["requested_service_name"],
            bool(row["urgent"]),
        )
        for row in arrived
    ]


def test_block_locks_cover_six_blocks_per_day_at_the_right_instants(completed_run):
    conn = completed_run["conn"]
    starts = {code: int(code[1:3]) for code in BLOCK_CODES}
    for service_date in DATES:
        rows = conn.execute(
            "SELECT * FROM _block_locks WHERE service_date = ? ORDER BY block_code",
            (service_date,),
        ).fetchall()
        assert [row["block_code"] for row in rows] == list(BLOCK_CODES)
        for row in rows:
            assert row["block_start"] == _ms(service_date, starts[row["block_code"]], 0)
            if row["block_code"] == "B07_09":
                assert row["locked_at"] == _ms(service_date, 6, 45)
            else:
                assert row["locked_at"] == row["block_start"]


def test_settlement_posts_expected_ledger_rows_in_order(completed_run):
    conn = completed_run["conn"]
    config = completed_run["config"]
    technicians = conn.execute(
        "SELECT id, hourly_wage_cents FROM _technicians ORDER BY id"
    ).fetchall()
    assert len(technicians) == 4
    overhead = config.get_int("finance.daily_operating_overhead")
    assert overhead == 30_000
    for service_date in DATES:
        rows = conn.execute(
            "SELECT * FROM _ledger_entries WHERE service_date = ? AND origin = 'run' "
            "ORDER BY id",
            (service_date,),
        ).fetchall()
        weekday = date.fromisoformat(service_date).isoweekday() <= 5
        if weekday:
            assert [row["entry_type"] for row in rows] == ["payroll_base"] * 4 + [
                "operating_overhead"
            ]
            for row, technician in zip(rows[:4], technicians, strict=False):
                assert row["technician_id"] == technician["id"]
                assert row["amount_cents"] == -(
                    constants.WEEKDAY_PAY_MULTIPLIER * technician["hourly_wage_cents"]
                )
        else:
            assert [row["entry_type"] for row in rows] == ["operating_overhead"]
        assert rows[-1]["amount_cents"] == -overhead
        for row in rows:
            assert row["amount_cents"] < 0
            assert row["occurred_at"] == _ms(service_date, 19, 0)


def test_run_completes_with_one_opening_closing_settlement_per_date(completed_run):
    conn = completed_run["conn"]
    assert completed_run["status"] is RunStatus.COMPLETED
    assert conn.execute("SELECT status FROM _run").fetchone()["status"] == "completed"
    for service_date in DATES:
        day_start = _ms(service_date, 0, 0)
        day_end = day_start + 86_400_000
        for kind in ("opening_invocation", "closing_invocation", "daily_settlement", "checkpoint"):
            count = conn.execute(
                "SELECT COUNT(*) c FROM _events WHERE kind = ? AND status = 'applied' "
                "AND scheduled_at_ms >= ? AND scheduled_at_ms < ?",
                (kind, day_start, day_end),
            ).fetchone()["c"]
            assert count == 1, (service_date, kind)
    finalized = conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = 'run_finalize' AND status = 'applied'"
    ).fetchone()["c"]
    assert finalized == 1
    per_type = {
        row["type"]: row["c"]
        for row in conn.execute(
            "SELECT type, COUNT(*) c FROM _invocations GROUP BY type"
        )
    }
    assert per_type["opening"] == 3
    assert per_type["closing"] == 3
    assert conn.execute(
        "SELECT COUNT(*) c FROM _invocations WHERE status != 'completed'"
    ).fetchone()["c"] == 0


def test_rerunning_the_loop_on_a_completed_database_changes_nothing(completed_run):
    conn = completed_run["conn"]
    before = dump_hash(conn, completed_run["run_id"])
    adapter = ScriptedNoOpAdapter()
    status = Controller(conn, completed_run["config"], adapter).run()
    assert status is RunStatus.COMPLETED
    assert adapter.prompts == []
    assert dump_hash(conn, completed_run["run_id"]) == before


NO_OP_EVENT_KINDS = {
    "employment_transition",
    "weather_realized",
    "labor_economy_update",
    "sickness_draw",
    "attrition_draw",
    "training_rates_drawn",
    "shift_materialization",
    "first_trip_dispatch",
}


def test_stub_events_and_weather_realized_draw_nothing_and_write_nothing(tmp_path):
    config = _config()
    conn = _init(tmp_path / "noop.db", config)
    register_conn(conn)
    controller = Controller(conn, config, ScriptedNoOpAdapter())

    def applied_ids():
        return {
            row["event_id"]
            for row in conn.execute("SELECT event_id FROM _events WHERE status = 'applied'")
        }

    def audit_count():
        return conn.execute("SELECT COUNT(*) c FROM _random_audit").fetchone()["c"]

    def weather_rows():
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM _zone_weather_daily ORDER BY zone_id, local_date"
            )
        ]

    seen = set()
    while True:
        before_ids = applied_ids()
        audit_before = audit_count()
        weather_before = weather_rows()
        if not controller.step():
            break
        for event_id in applied_ids() - before_ids:
            kind = conn.execute(
                "SELECT kind FROM _events WHERE event_id = ?", (event_id,)
            ).fetchone()["kind"]
            if kind in NO_OP_EVENT_KINDS:
                seen.add(kind)
                assert audit_count() == audit_before, kind
                if kind == "weather_realized":
                    assert weather_rows() == weather_before
    assert controller.status() is RunStatus.COMPLETED
    assert seen == NO_OP_EVENT_KINDS


def test_two_fresh_runs_from_the_same_seed_produce_identical_dumps(completed_run, tmp_path):
    config = _config()
    conn = _init(tmp_path / "twin.db", config)
    register_conn(conn)
    status = Controller(conn, config, ScriptedNoOpAdapter()).run()
    assert status is RunStatus.COMPLETED
    assert dump_hash(conn, _run_id(conn)) == dump_hash(
        completed_run["conn"], completed_run["run_id"]
    )


@pytest.mark.parametrize("steps", [9, 10, 14])
def test_interrupted_run_resumes_from_a_file_copy_to_identical_state(
    completed_run, tmp_path, steps
):
    config = _config()
    path = tmp_path / "interrupted.db"
    conn = _init(path, config)
    try:
        status = Controller(conn, config, ScriptedNoOpAdapter()).run(max_steps=steps)
        assert status is RunStatus.ACTIVE
    finally:
        conn.close()
    resumed_path = tmp_path / "resumed.db"
    shutil.copyfile(path, resumed_path)
    resumed = open_run_db(resumed_path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(resumed)
    status = Controller(resumed, config, ScriptedNoOpAdapter()).run()
    assert status is RunStatus.COMPLETED
    assert dump_hash(resumed, _run_id(resumed)) == dump_hash(
        completed_run["conn"], completed_run["run_id"]
    )


@pytest.mark.parametrize("crash_on_prompt", [1, 2])
def test_adapter_crash_leaves_an_active_invocation_that_resume_completes(
    completed_run, tmp_path, crash_on_prompt
):
    config = _config()
    path = tmp_path / "crashed.db"
    conn = _init(path, config)
    try:
        with pytest.raises(ConnectionError):
            Controller(conn, config, CrashOnNthPromptAdapter(crash_on_prompt)).run()
        active = conn.execute(
            "SELECT type, ts_ms FROM _invocations WHERE status = 'active'"
        ).fetchall()
        assert len(active) == 1
        active_type = active[0]["type"]
        active_ts = active[0]["ts_ms"]
    finally:
        conn.close()
    resumed_path = tmp_path / "resumed.db"
    shutil.copyfile(path, resumed_path)
    resumed = open_run_db(resumed_path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(resumed)
    adapter = ScriptedNoOpAdapter()
    status = Controller(resumed, config, adapter).run()
    assert status is RunStatus.COMPLETED
    assert adapter.prompts[0].kind.value == active_type
    assert adapter.prompts[0].ts_ms == active_ts
    assert resumed.execute(
        "SELECT COUNT(*) c FROM _invocations WHERE status != 'completed'"
    ).fetchone()["c"] == 0
    assert dump_hash(resumed, _run_id(resumed)) == dump_hash(
        completed_run["conn"], completed_run["run_id"]
    )


def test_settlement_driving_cash_negative_flips_bankrupt_and_halts(tmp_path):
    config = _config(**{"finance.starting_cash": 100_000})
    conn = _init(tmp_path / "bankrupt.db", config)
    register_conn(conn)
    adapter = ScriptedNoOpAdapter()
    status = Controller(conn, config, adapter).run()
    assert status is RunStatus.BANKRUPT
    assert conn.execute("SELECT status FROM _run").fetchone()["status"] == "bankrupt"
    ledger_balance = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) s FROM _ledger_entries"
    ).fetchone()["s"]
    assert ledger_balance < 0
    days = conn.execute("SELECT * FROM _day_state ORDER BY service_date").fetchall()
    assert [row["service_date"] for row in days] == [DATES[0]]
    assert days[0]["settlement_status"] == "settled"
    settlements = conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = 'daily_settlement' AND status = 'applied'"
    ).fetchone()["c"]
    assert settlements == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM _events WHERE kind = 'checkpoint' AND status = 'pending'"
    ).fetchone()["c"] == 1
    max_ts = conn.execute("SELECT MAX(ts_ms) m FROM _invocations").fetchone()["m"]
    assert max_ts == _ms(DATES[0], 19, 0)
    assert conn.execute(
        "SELECT COUNT(*) c FROM _invocations WHERE status != 'completed'"
    ).fetchone()["c"] == 0
    replay = ScriptedNoOpAdapter()
    assert Controller(conn, config, replay).run() is RunStatus.BANKRUPT
    assert replay.prompts == []
