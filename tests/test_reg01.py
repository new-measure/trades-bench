from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from _adapters import ScriptedNoOpAdapter
from _helpers import GEO_ARTIFACT_DIR
from test_acceptance_booked_run import (
    BOOKED_RUN_DEMAND,
    BOOKED_RUN_ORDERS,
    DAY_ONE,
    DAY_THREE,
    NO_SHOW_ORDERS,
    OVERRUN_DURATION_MINUTES,
    SEED,
    ScriptedBookingAdapter,
    _config,
    _init,
    _ms,
)
from trades_bench import appointments, clock
from trades_bench._dump import dump_hash
from trades_bench.adapter import AdapterResponse
from trades_bench.controller import Controller
from trades_bench.enums import RunStatus
from trades_bench.wire import SPEC_VERSION

GOLDEN_DIR = Path(__file__).parent / "golden" / "reg01"
REGEN_ENV = "REGEN_GOLDEN_REG01"
PRODUCIBLE_SCENARIOS = (
    "booking_day",
    "cancellation_day_acknowledged_and_auto_finalized",
    "overrun_past_1900_day",
    "zero_job_weekend_day",
)
EXCLUDED_SCENARIOS = (
    "callback_warranty_rework_day",
    "sickness_and_walk_off_day",
    "hire_train_terminate_day",
)


class CancellationAdapter(ScriptedBookingAdapter):

    def __init__(self):
        orders = {
            DAY_ONE: tuple(
                replace(order, service_date=DAY_THREE)
                for order in BOOKED_RUN_ORDERS[DAY_ONE][4:6]
            )
        }
        super().__init__(orders)
        self.acknowledged = False

    def send(self, prompt):
        if prompt.kind.value == "call" and "appointment_id" in prompt.payload:
            if not self.acknowledged:
                call_id = int(prompt.payload["call_id"])
                response = appointments.acknowledge_cancellation_command(
                    self.controller.services,
                    f"reg01-acknowledge-{call_id}",
                    self.controller.config,
                    call_id=call_id,
                )
                assert json.loads(response)["ok"]
                self.acknowledged = True
                return AdapterResponse(text="acknowledged", terminal_action_taken=True)
            self.prompts.append(prompt)
            return AdapterResponse(text="", terminal_action_taken=False)
        return super().send(prompt)


def golden_key(config) -> str:
    return "__".join((SPEC_VERSION, config.config_version, str(SEED)))


def golden_path(config) -> Path:
    return GOLDEN_DIR / f"{golden_key(config)}.json"


def _run(path, config, adapter):
    conn = _init(path, config)
    controller = Controller(conn, config, adapter, artifact_dir=GEO_ARTIFACT_DIR)
    if hasattr(adapter, "controller"):
        adapter.controller = controller
    assert controller.run() is RunStatus.COMPLETED
    return conn


def _regression_config(**overrides):
    return _config(**BOOKED_RUN_DEMAND, **overrides)


def _trace(conn, service_date):
    rows = conn.execute(
        "SELECT event_id, kind, scheduled_at_ms, priority, entity_type, entity_id, "
        "payload_version, payload, status FROM _events "
        "WHERE scheduled_at_ms >= ? AND scheduled_at_ms < ? "
        "ORDER BY scheduled_at_ms, priority, order_key, event_id",
        (_ms(service_date, 0, 0), _ms(service_date, 0, 0) + 86_400_000),
    ).fetchall()
    return [
        {
            "event_id": int(row["event_id"]),
            "kind": row["kind"],
            "scheduled_at_ms": int(row["scheduled_at_ms"]),
            "priority": int(row["priority"]),
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "payload_version": int(row["payload_version"]),
            "payload": json.loads(row["payload"]) if row["payload"] else None,
            "status": row["status"],
        }
        for row in rows
    ]


def _scenario_record(conn, service_date, adapter):
    run_id = str(conn.execute("SELECT run_id FROM _run").fetchone()["run_id"])
    return {
        "event_trace": _trace(conn, service_date),
        "prompt_trace": [
            {
                "kind": prompt.kind.value,
                "ts_ms": prompt.ts_ms,
                "payload": prompt.payload,
            }
            for prompt in adapter.prompts
        ],
        "logical_dump_sha256": dump_hash(conn, run_id),
    }


def _record(tmp_path):
    booking_config = _regression_config(**{"run.end_date": DAY_ONE})
    booking_adapter = ScriptedBookingAdapter({DAY_ONE: BOOKED_RUN_ORDERS[DAY_ONE][:1]})
    booking = _run(
        tmp_path / "booking.db",
        booking_config,
        booking_adapter,
    )
    cancellation_config = _regression_config(
        **{
            "run.end_date": DAY_THREE,
            "cancellations.base_probability_by_segment": {
                "residential": 1.0,
                "commercial": 1.0,
                "apartment": 1.0,
            },
        }
    )
    cancellation_adapter = CancellationAdapter()
    cancellation = _run(
        tmp_path / "cancellation.db", cancellation_config, cancellation_adapter
    )
    overrun_config = _regression_config(
        **{
            "run.end_date": DAY_ONE,
            "appointments.min_actual_duration_minutes": OVERRUN_DURATION_MINUTES,
        }
    )
    overrun_adapter = ScriptedBookingAdapter(NO_SHOW_ORDERS)
    overrun = _run(tmp_path / "overrun.db", overrun_config, overrun_adapter)
    weekend_config = _regression_config(**{"run.end_date": DAY_THREE})
    weekend_adapter = ScriptedNoOpAdapter()
    weekend = _run(tmp_path / "weekend.db", weekend_config, weekend_adapter)
    cancellation_dates = [
        clock.service_date_for_ms(int(row["arrived_at"]))
        for row in cancellation.execute(
            "SELECT arrived_at FROM _calls WHERE call_kind = 'cancellation' ORDER BY arrived_at"
        )
    ]
    assert len(cancellation_dates) == 2
    assert len(set(cancellation_dates)) == 1
    statuses = [
        row["status"]
        for row in cancellation.execute(
            "SELECT status FROM _calls WHERE call_kind = 'cancellation' ORDER BY id"
        )
    ]
    assert statuses == ["acknowledged", "unanswered"]
    scenarios = {
        "booking_day": _scenario_record(booking, DAY_ONE, booking_adapter),
        "cancellation_day_acknowledged_and_auto_finalized": _scenario_record(
            cancellation, cancellation_dates[0], cancellation_adapter
        ),
        "overrun_past_1900_day": _scenario_record(overrun, DAY_ONE, overrun_adapter),
        "zero_job_weekend_day": _scenario_record(weekend, DAY_THREE, weekend_adapter),
    }
    for conn in (booking, cancellation, overrun, weekend):
        conn.close()
    document = {
        "spec_version": SPEC_VERSION,
        "config_version": booking_config.config_version,
        "master_seed": SEED,
        "scenarios": scenarios,
        "excluded_scenarios": list(EXCLUDED_SCENARIOS),
    }
    return booking_config, (json.dumps(document, indent=2, ensure_ascii=True) + "\n").encode()


def test_golden_event_traces(tmp_path):
    config, recorded = _record(tmp_path)
    path = golden_path(config)
    if os.environ.get(REGEN_ENV) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(recorded)
        pytest.skip(f"{REGEN_ENV}=1 rewrote {path.name}")
    assert path.is_file(), (
        f"missing event golden {path}; rerun with {REGEN_ENV}=1 to record it deliberately"
    )
    stored = path.read_bytes()
    if stored != recorded:
        actual = tmp_path / "actual.json"
        actual.write_bytes(recorded)
        raise AssertionError(
            f"event goldens changed for key {golden_key(config)}; compare {path} with "
            f"{actual} and rerun with {REGEN_ENV}=1 only for a deliberate event change"
        )


def test_golden_scenario_set_is_frozen():
    config = _regression_config()
    path = golden_path(config)
    assert path.is_file()
    stored = json.loads(path.read_bytes())
    assert tuple(stored["scenarios"]) == PRODUCIBLE_SCENARIOS
    assert tuple(stored["excluded_scenarios"]) == EXCLUDED_SCENARIOS
    assert stored["spec_version"] == SPEC_VERSION
    assert stored["config_version"] == config.config_version
    assert stored["master_seed"] == SEED
    for scenario in stored["scenarios"].values():
        assert set(scenario) == {"event_trace", "prompt_trace", "logical_dump_sha256"}
        assert len(scenario["logical_dump_sha256"]) == 64
        assert scenario["event_trace"]
        assert scenario["prompt_trace"]
