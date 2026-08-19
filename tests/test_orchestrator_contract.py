from __future__ import annotations

import json
import shutil
from datetime import date, timedelta

import pytest

from _adapters import ScriptedNoOpAdapter
from _helpers import (
    GEO_ARTIFACT_DIR,
    GEO_ARTIFACT_ID,
    GEO_FIXTURE_ZONE_WEIGHTS,
    geo_artifact_sha256,
)
from test_execution import (
    SECOND_WEEKDAY,
    UNSUPPORTED_CATEGORY_ID,
    WEEKDAY,
    _appointment_event_rows,
    _book_execution,
    _build_run,
)
from trades_bench import clock, constants
from trades_bench.adapter import AdapterResponse
from trades_bench.config import load_run_config
from trades_bench.controller import Controller
from trades_bench.enums import LedgerCategory, RunStatus
from trades_bench.ledger import LedgerEntry
from trades_bench.registry import OPERATIONS_BY_NAME
from trades_bench.run_init import initialize_run, open_run_db
from trades_bench.txn import privileged, privileged_request_id

START = "2026-10-01"
END = "2026-10-03"

PRE_OPENING_ORDER = (
    "weather_realized",
    "labor_economy_update",
    "sickness_draw",
    "attrition_draw",
    "training_rates_drawn",
    "shift_materialization",
    "demand_generation",
    "opening_invocation",
)


def _config(**overrides):
    keys = {
        "geography.artifact_id": GEO_ARTIFACT_ID,
        "geography.artifact_sha256": geo_artifact_sha256(),
        "demand.zone_weights": GEO_FIXTURE_ZONE_WEIGHTS,
        "run.end_date": END,
        **overrides,
    }
    return load_run_config(scenario_json=json.dumps({"keys": keys}))


def _run(tmp_path, config=None, *, seed=5, adapter=None):
    config = config or _config()
    path = tmp_path / "run.db"
    initialize_run(path, master_seed=seed, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)
    controller = Controller(conn, config, adapter or ScriptedNoOpAdapter())
    status = controller.run()
    return conn, controller, status


def _instant(service_date, hour, minute=0):
    value = date.fromisoformat(service_date)
    return clock.phoenix_instant(value.year, value.month, value.day, hour, minute)


@pytest.fixture(scope="module")
def ordinary_run(tmp_path_factory):
    conn, controller, status = _run(tmp_path_factory.mktemp("sm_orch"))
    yield conn, controller, status
    conn.close()


def test_sm_orch_01_day_shape_including_zero_job_weekend(ordinary_run):
    conn, _, status = ordinary_run
    assert status is RunStatus.COMPLETED
    rows = conn.execute("SELECT * FROM _day_state ORDER BY service_date").fetchall()
    assert [row["service_date"] for row in rows] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    for row in rows:
        assert row["opening_at"] == _instant(row["service_date"], 6, 45)
        assert row["closing_at"] == _instant(row["service_date"], 19)
        assert row["unresolved_execution_count"] == 0


def test_sm_orch_01_failed_visit_is_a_terminal_execution_and_the_day_closes(tmp_path):
    conn, cfg, controller, _ = _build_run(tmp_path)
    booked = _book_execution(
        controller,
        cfg,
        technician_id=1,
        service_date=WEEKDAY,
        first_block_code="B09_11",
        job_type_id=5,
        requested_category_id=UNSUPPORTED_CATEGORY_ID,
    )
    try:
        assert controller.run() is RunStatus.COMPLETED
        events = _appointment_event_rows(conn, booked["appointment_id"])
        assert [row["event_type"] for row in events] == [
            "dispatched",
            "arrived",
            "work_failed",
        ]
        failed_at = int(events[-1]["occurred_at"])
        day = conn.execute(
            "SELECT * FROM _day_state WHERE service_date = ?", (WEEKDAY,)
        ).fetchone()
        assert day["unresolved_execution_count"] == 0
        assert day["last_execution_completed_at"] == failed_at
        assert day["closing_at"] == max(_instant(WEEKDAY, 19), failed_at)
        assert day["settlement_status"] == "settled"
    finally:
        conn.close()


def test_sm_orch_02_closing_is_future_facing_only():
    from test_invocations import (
        test_closing_invocation_rejects_finished_day_mutation_and_allows_future,
    )

    test_closing_invocation_rejects_finished_day_mutation_and_allows_future()


def test_sm_orch_03_time_control_and_no_next_day_operation(ordinary_run):
    conn, _, _ = ordinary_run
    before = clock.now(conn)
    assert "next_day" not in OPERATIONS_BY_NAME
    assert clock.now(conn) == before
    rows = conn.execute("SELECT ts_ms FROM _invocations WHERE type = 'call'").fetchall()
    assert all(
        constants.CALL_WINDOW_START_MIN
        <= clock.phoenix_local(row["ts_ms"]).hour * 60 + clock.phoenix_local(row["ts_ms"]).minute
        < constants.CALL_WINDOW_END_MIN
        for row in rows
    )


def test_sm_orch_04_event_exclusivity():
    from test_invocations import test_world_event_cannot_commit_while_invocation_active

    test_world_event_cannot_commit_while_invocation_active()


def test_sm_orch_05_final_day_closes_settles_finalizes_and_records_score(ordinary_run):
    conn, _, _ = ordinary_run
    run = conn.execute("SELECT status, final_score_cents FROM _run").fetchone()
    day = conn.execute("SELECT * FROM _day_state WHERE service_date = ?", (END,)).fetchone()
    balance = conn.execute("SELECT SUM(amount_cents) value FROM _ledger_entries").fetchone()["value"]
    kinds = [
        row["kind"]
        for row in conn.execute(
            "SELECT kind FROM _events WHERE status = 'applied' ORDER BY scheduled_at_ms, priority, event_id"
        )
    ]
    assert day["settlement_status"] == "settled"
    assert kinds[-3:] == ["daily_settlement", "checkpoint", "run_finalize"]
    assert run["status"] == "completed"
    assert run["final_score_cents"] == balance


def test_sm_orch_05_final_day_job_finishes_after_midnight_before_finalize(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path, **{"appointments.min_actual_duration_minutes": 450}
    )
    booked = _book_execution(
        controller,
        cfg,
        technician_id=1,
        service_date=WEEKDAY,
        first_block_code="B17_19",
    )
    try:
        assert controller.run() is RunStatus.COMPLETED
        appointment = conn.execute(
            "SELECT actual_end FROM _appointments WHERE id = ?", (booked["appointment_id"],)
        ).fetchone()
        actual_end = int(appointment["actual_end"])
        assert clock.service_date_for_ms(actual_end) != WEEKDAY
        day = conn.execute(
            "SELECT * FROM _day_state WHERE service_date = ?", (WEEKDAY,)
        ).fetchone()
        assert day["closing_at"] == actual_end
        assert day["last_execution_completed_at"] == actual_end
        tail = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM _events WHERE status = 'applied' "
                "ORDER BY scheduled_at_ms, priority, event_id"
            )
        ][-4:]
        assert tail == ["closing_invocation", "daily_settlement", "checkpoint", "run_finalize"]
        run = conn.execute("SELECT status, final_score_cents FROM _run").fetchone()
        assert run["status"] == "completed"
        assert run["final_score_cents"] is not None
    finally:
        conn.close()


def test_sm_orch_07_no_holiday_calendar(ordinary_run):
    conn, _, _ = ordinary_run
    rows = conn.execute("SELECT service_date, day_type FROM _day_state ORDER BY service_date").fetchall()
    assert all(row["day_type"] in {"weekday", "weekend"} for row in rows)
    assert [row["day_type"] for row in rows] == ["weekday", "weekday", "weekend"]


def test_sm_orch_08_weather_precedes_demand_and_demand_precedes_opening(ordinary_run):
    conn, _, _ = ordinary_run
    rows = conn.execute(
        "SELECT kind FROM _events WHERE scheduled_at_ms = ? ORDER BY priority, event_id",
        (_instant(START, 6, 45),),
    ).fetchall()
    kinds = [row["kind"] for row in rows]
    assert tuple(kind for kind in kinds if kind in PRE_OPENING_ORDER) == PRE_OPENING_ORDER


def _replace_weather(conn, *, hot_storm: bool):
    conn.execute("DROP TRIGGER trg_zone_weather_daily_no_update")
    if hot_storm:
        values = (1150, 900, 200, 400, 1, 1000, "storm")
    else:
        values = (700, 500, 0, 80, 0, None, "clear")
    conn.execute(
        "UPDATE _zone_weather_daily SET high_f_tenths = ?, low_f_tenths = ?, "
        "precipitation_mm_tenths = ?, wind_mph_tenths = ?, storm_flag = ?, "
        "storm_severity_milli = ?, condition_code = ? "
        "WHERE zone_id = 4 AND local_date = ?",
        (*values, START),
    )
    conn.execute(
        "CREATE TRIGGER trg_zone_weather_daily_no_update "
        "BEFORE UPDATE ON _zone_weather_daily FOR EACH ROW BEGIN "
        "SELECT RAISE(ABORT, 'append_only_table'); END"
    )
    conn.commit()


def _pending_call_signature(conn):
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT zone_id, arrival_at, requested_category_id, urgent "
            "FROM _pending_calls ORDER BY id"
        ).fetchall()
    ]


def test_sm_orch_08_demand_reads_the_current_days_realized_weather(tmp_path):
    config = _config(
        **{
            "run.end_date": START,
            "demand.zone_weights": [0, 0, 0, 1, 0, 0, 0, 0, 0, 0],
            "demand.base_new_caller_rate_per_day": 50,
        }
    )
    source = tmp_path / "source.db"
    initialize_run(source, master_seed=5, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    signatures = []
    for name, hot_storm in (("clear", False), ("storm", True)):
        path = tmp_path / f"{name}.db"
        shutil.copyfile(source, path)
        conn = open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)
        try:
            _replace_weather(conn, hot_storm=hot_storm)
            controller = Controller(conn, config, ScriptedNoOpAdapter())
            while True:
                demand_event = conn.execute(
                    "SELECT status FROM _events WHERE kind = 'demand_generation'"
                ).fetchone()
                if demand_event is not None and demand_event["status"] == "applied":
                    break
                assert controller.step()
            signatures.append(_pending_call_signature(conn))
        finally:
            conn.close()
    assert signatures[0]
    assert signatures[1]
    assert signatures[0] != signatures[1]


class _ClockProbeAdapter(ScriptedNoOpAdapter):
    def __init__(self):
        super().__init__()
        self.times = []

    def send(self, prompt):
        self.times.append(prompt.ts_ms)
        return AdapterResponse(text="", terminal_action_taken=False)


def test_sm_orch_09_deferred_clock_never_decreases(tmp_path):
    adapter = _ClockProbeAdapter()
    conn, _, status = _run(tmp_path, adapter=adapter)
    try:
        assert status is RunStatus.COMPLETED
        assert adapter.times == sorted(adapter.times)
        rows = conn.execute("SELECT service_date, opening_at, closing_at FROM _day_state").fetchall()
        assert len(rows) == len({row["service_date"] for row in rows})
        assert all(row["opening_at"] is not None and row["closing_at"] is not None for row in rows)
    finally:
        conn.close()


def test_sm_orch_09_previous_day_cascade_defers_the_next_day_without_reordering(tmp_path):
    conn, cfg, controller, _ = _build_run(
        tmp_path,
        end_date=SECOND_WEEKDAY,
        **{
            "appointments.min_actual_duration_minutes": 900,
            "demand.base_new_caller_rate_per_day": 50,
        },
    )
    booked = _book_execution(
        controller,
        cfg,
        technician_id=1,
        service_date=WEEKDAY,
        first_block_code="B17_19",
    )
    try:
        assert controller.run() is RunStatus.COMPLETED
        actual_end = int(
            conn.execute(
                "SELECT actual_end FROM _appointments WHERE id = ?", (booked["appointment_id"],)
            ).fetchone()["actual_end"]
        )
        planned_opening = _instant(SECOND_WEEKDAY, 6, 45)
        assert actual_end >= planned_opening
        first_day = conn.execute(
            "SELECT * FROM _day_state WHERE service_date = ?", (WEEKDAY,)
        ).fetchone()
        second_day = conn.execute(
            "SELECT * FROM _day_state WHERE service_date = ?", (SECOND_WEEKDAY,)
        ).fetchone()
        assert first_day["closing_at"] == actual_end
        assert second_day["opening_at"] == actual_end
        assert second_day["closing_at"] >= second_day["opening_at"]
        rows = conn.execute(
            "SELECT kind FROM _events WHERE scheduled_at_ms = ? ORDER BY priority, event_id",
            (planned_opening,),
        ).fetchall()
        kinds = [row["kind"] for row in rows]
        assert tuple(kind for kind in kinds if kind in PRE_OPENING_ORDER) == PRE_OPENING_ORDER
        delayed = conn.execute(
            "SELECT e.scheduled_at_ms, c.arrived_at, i.ts_ms, i.sequence "
            "FROM _events e JOIN _call_hidden h ON h.arrival_event_id = e.event_id "
            "JOIN _calls c ON c.id = h.call_id "
            "JOIN _invocations i ON i.origin_event_id = e.event_id "
            "WHERE c.service_date = ? AND e.scheduled_at_ms < ? ORDER BY i.sequence",
            (SECOND_WEEKDAY, actual_end),
        ).fetchall()
        assert delayed
        assert [row["scheduled_at_ms"] for row in delayed] == sorted(
            row["scheduled_at_ms"] for row in delayed
        )
        assert all(row["arrived_at"] == actual_end for row in delayed)
        assert all(row["ts_ms"] == actual_end for row in delayed)
        counts = dict(
            conn.execute(
                "SELECT type, COUNT(*) count FROM _invocations "
                "WHERE type IN ('opening', 'closing') GROUP BY type"
            ).fetchall()
        )
        assert counts == {"opening": 2, "closing": 2}
    finally:
        conn.close()


def test_sm_orch_10_settlement_occurred_at_id_and_balance_after_order(ordinary_run):
    conn, _, _ = ordinary_run
    for offset in range(3):
        service_date = (date.fromisoformat(START) + timedelta(days=offset)).isoformat()
        rows = conn.execute(
            "SELECT id, entry_type, amount_cents, balance_after_cents FROM _ledger_entries "
            "WHERE service_date = ? ORDER BY occurred_at, id",
            (service_date,),
        ).fetchall()
        assert rows[-1]["entry_type"] == "operating_overhead"
        if date.fromisoformat(service_date).weekday() < 5:
            assert all(row["entry_type"] == "payroll_base" for row in rows[:-1])
        for row in rows:
            total = conn.execute(
                "SELECT SUM(amount_cents) value FROM _ledger_entries WHERE id <= ?", (row["id"],)
            ).fetchone()["value"]
            assert row["balance_after_cents"] == total


def test_acc_04_synthetic_mid_day_financial_commit_halts_before_next_prompt(tmp_path):
    config = _config(**{"finance.starting_cash": 1})
    path = tmp_path / "run.db"
    initialize_run(path, master_seed=5, config=config, artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)
    adapter = ScriptedNoOpAdapter()
    controller = Controller(conn, config, adapter)
    try:
        controller.run(max_steps=1)
        occurred_at = _instant(START, 12)

        def post(ctx):
            ctx.services.ledger.post(
                [LedgerEntry(LedgerCategory.MATERIAL_COST, -2, occurred_at, service_date=START)]
            )
            return {}

        body = json.loads(privileged(controller.services, privileged_request_id("acc04", 1), post))
        assert body["ok"] is True
        prompt_count = len(adapter.prompts)
        assert controller.status() is RunStatus.BANKRUPT
        assert controller.step() is False
        assert len(adapter.prompts) == prompt_count
        assert conn.execute("SELECT final_score_cents FROM _run").fetchone()["final_score_cents"] == 0
    finally:
        conn.close()


def test_acc_04_bankruptcy_date_is_recorded_by_the_causing_ledger_prefix(tmp_path):
    config = _config(**{"finance.starting_cash": 100_000})
    conn, _, status = _run(tmp_path, config)
    try:
        assert status is RunStatus.BANKRUPT
        first_negative = conn.execute(
            "SELECT service_date, occurred_at, balance_after_cents FROM _ledger_entries "
            "WHERE balance_after_cents < 0 AND occurred_at >= ? ORDER BY id LIMIT 1",
            (_instant(START, 0),),
        ).fetchone()
        assert first_negative is not None
        assert first_negative["service_date"] == START
        assert clock.service_date_for_ms(first_negative["occurred_at"]) == START
        assert first_negative["balance_after_cents"] < 0
        assert conn.execute(
            "SELECT COUNT(*) count FROM _day_state WHERE service_date > ?", (START,)
        ).fetchone()["count"] == 0
    finally:
        conn.close()
