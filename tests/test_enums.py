from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from trades_bench import enums
from trades_bench.enums import (
    EVENT_PRIORITY,
    ConditionCode,
    DayType,
    EntityType,
    EventKind,
    EventStatus,
    InvocationStatus,
    InvocationType,
    KeyedStreamName,
    LedgerCategory,
    PromptKind,
    RunStatus,
    SequentialStreamName,
    VisibilityClass,
)


def test_run_status_set_and_terminal():
    assert set(RunStatus) == {
        "initializing",
        "active",
        "completed",
        "bankrupt",
        "failed",
    }
    assert RunStatus.terminal() == frozenset(
        {RunStatus.COMPLETED, RunStatus.BANKRUPT, RunStatus.FAILED}
    )
    assert RunStatus.INITIALIZING not in RunStatus.terminal()
    assert RunStatus.ACTIVE not in RunStatus.terminal()


def test_event_status_closed():
    assert set(EventStatus) == {"pending", "processing", "applied", "cancelled"}


def test_invocation_kinds():
    assert set(InvocationType) == {"opening", "call", "closing"}
    assert set(InvocationStatus) == {"active", "completed"}


def test_prompt_kinds_are_the_invocation_kinds_plus_reminder():
    assert set(PromptKind) == {"opening", "call", "reminder", "closing"}
    assert {t.value for t in InvocationType} == set(PromptKind) - {
        PromptKind.REMINDER
    }


def test_visibility_classes():
    assert set(VisibilityClass) == {
        "public_sql",
        "public_sdk",
        "prompt_only",
        "notification",
        "private_engine",
        "post_run_only",
    }
    assert VisibilityClass.PRIVATE_ENGINE not in VisibilityClass.public()


def test_ledger_categories_closed_9_member_set():
    assert [c.value for c in LedgerCategory] == [
        "opening_balance_adjustment",
        "revenue",
        "material_cost",
        "payroll_base",
        "payroll_off_hours",
        "payroll_off_hours_bonus",
        "recruiting_cost",
        "training_cost",
        "operating_overhead",
    ]


def test_stream_registry_counts_match_spec():
    assert len(list(SequentialStreamName)) == enums.SEQUENTIAL_STREAM_COUNT == 4
    assert len(list(KeyedStreamName)) == enums.KEYED_STREAM_COUNT == 16
    seq = {str(s) for s in SequentialStreamName}
    keyed = {str(s) for s in KeyedStreamName}
    assert seq.isdisjoint(keyed)
    assert enums.ALL_STREAM_NAMES == seq | keyed
    assert len(enums.ALL_STREAM_NAMES) == 20


def test_sequential_stream_table_complete():
    seq = {str(s) for s in SequentialStreamName}
    assert seq == {
        "weather_process",
        "labor_economy",
        "competitor_background",
        "warm_start",
    }


def test_keyed_stream_table_complete():
    expected = {
        "weather_forecast",
        "demand",
        "acceptance",
        "cancellation",
        "competitor_leak",
        "travel_noise",
        "workforce_health",
        "workforce_attrition",
        "hiring",
        "training",
        "technician_quality",
        "job_execution",
        "job_quality",
        "job_finance",
        "callbacks",
        "reviews",
    }
    keyed = {str(s) for s in KeyedStreamName}
    assert keyed == expected
    assert "weather_forecast" not in {str(s) for s in SequentialStreamName}
    assert "weather_forecast" in keyed


def test_stream_enums_travel_through_json():
    assert str(SequentialStreamName.WEATHER_PROCESS) == "weather_process"
    assert json.dumps({"s": KeyedStreamName.ACCEPTANCE}) == '{"s": "acceptance"}'


def test_event_kind_closed_set_and_central_priority_declaration():
    expected = {
        "day_rollover": 100,
        "employment_transition": 110,
        "weather_realized": 120,
        "labor_economy_update": 130,
        "sickness_draw": 140,
        "attrition_draw": 150,
        "training_rates_drawn": 160,
        "shift_materialization": 170,
        "demand_generation": 180,
        "block_lock": 200,
        "appointment_dispatch": 300,
        "appointment_arrival": 310,
        "work_started": 320,
        "work_completed": 330,
        "work_failed": 332,
        "company_no_show": 335,
        "off_hours_accrual_mark": 340,
        "technician_return_home": 350,
        "technician_home_arrival": 360,
        "review_published": 410,
        "competitor_effect": 420,
        "opening_invocation": 900,
        "first_block_lock": 905,
        "first_trip_dispatch": 906,
        "call_invocation": 910,
        "closing_invocation": 920,
        "daily_settlement": 950,
        "checkpoint": 960,
        "run_finalize": 970,
    }
    assert {k.value: EVENT_PRIORITY[k] for k in EventKind} == expected
    assert set(EVENT_PRIORITY) == set(EventKind)
    assert len(EventKind) == 29


def test_world_events_sort_before_invocations_except_the_opening_pair():
    harness_kinds = {
        EventKind.OPENING_INVOCATION,
        EventKind.FIRST_BLOCK_LOCK,
        EventKind.FIRST_TRIP_DISPATCH,
        EventKind.CALL_INVOCATION,
        EventKind.CLOSING_INVOCATION,
        EventKind.DAILY_SETTLEMENT,
        EventKind.CHECKPOINT,
        EventKind.RUN_FINALIZE,
    }
    world_kinds = set(EventKind) - harness_kinds
    invocation_priorities = {
        EVENT_PRIORITY[k]
        for k in (
            EventKind.OPENING_INVOCATION,
            EventKind.CALL_INVOCATION,
            EventKind.CLOSING_INVOCATION,
        )
    }
    assert max(EVENT_PRIORITY[k] for k in world_kinds) < min(invocation_priorities)
    assert EVENT_PRIORITY[EventKind.FIRST_BLOCK_LOCK] > EVENT_PRIORITY[EventKind.OPENING_INVOCATION]
    assert EVENT_PRIORITY[EventKind.FIRST_TRIP_DISPATCH] > EVENT_PRIORITY[EventKind.FIRST_BLOCK_LOCK]
    assert (
        EVENT_PRIORITY[EventKind.OPENING_INVOCATION]
        < EVENT_PRIORITY[EventKind.CALL_INVOCATION]
        < EVENT_PRIORITY[EventKind.CLOSING_INVOCATION]
        < EVENT_PRIORITY[EventKind.DAILY_SETTLEMENT]
        < EVENT_PRIORITY[EventKind.CHECKPOINT]
        < EVENT_PRIORITY[EventKind.RUN_FINALIZE]
    )


def test_entity_type_closed_set():
    assert {t.value for t in EntityType} == {
        "run",
        "zone",
        "customer",
        "job",
        "appointment",
        "technician",
        "pending_call",
    }


def test_day_type_from_iso_weekday():
    assert set(DayType) == {"weekday", "weekend"}
    assert [DayType.from_iso_weekday(n) for n in range(1, 8)] == [
        DayType.WEEKDAY,
        DayType.WEEKDAY,
        DayType.WEEKDAY,
        DayType.WEEKDAY,
        DayType.WEEKDAY,
        DayType.WEEKEND,
        DayType.WEEKEND,
    ]
    first = date(2026, 10, 1)
    for offset in range(14):
        d = first + timedelta(days=offset)
        expected = DayType.WEEKDAY if d.isoweekday() <= 5 else DayType.WEEKEND
        assert DayType.from_iso_weekday(d.isoweekday()) is expected
    for bad in (0, 8, -1, True, "1", 1.0):
        with pytest.raises(ValueError):
            DayType.from_iso_weekday(bad)


def test_condition_code_closed_7_member_set():
    assert {c.value for c in ConditionCode} == {
        "clear",
        "hot",
        "extreme_heat",
        "windy",
        "rain",
        "storm",
        "cool",
    }
    assert len(ConditionCode) == 7
