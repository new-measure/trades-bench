"""Tests for the closed enumerations (spec 01 §Canonical primitive types,
§Deterministic randomness, §Event envelope)."""

from __future__ import annotations

import json

from csr_bench import enums
from csr_bench.enums import (
    EntityType,
    EventKind,
    EventStatus,
    InvocationStatus,
    InvocationType,
    KeyedStreamName,
    LedgerCategory,
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
    # spec 06 §9.1 — the closed v1 entry-type set; adding a member is a
    # spec_version change. Values and order are pinned exactly as spelled.
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
    # weather_forecast is keyed, NOT sequential (spec 01 §Keyed-draw registry).
    assert "weather_forecast" not in {str(s) for s in SequentialStreamName}
    assert "weather_forecast" in keyed


def test_stream_enums_travel_through_json():
    assert str(SequentialStreamName.WEATHER_PROCESS) == "weather_process"
    assert json.dumps({"s": KeyedStreamName.ACCEPTANCE}) == '{"s": "acceptance"}'


def test_event_kinds_only_contract_test_now():
    kinds = set(EventKind)
    assert kinds, " registry must be non-empty"
    assert all(k.value.startswith("contract_test_") for k in EventKind)


def test_entity_type_is_contract_test_only_now():
    assert all(t.value.startswith("contract_test_") for t in EntityType)
