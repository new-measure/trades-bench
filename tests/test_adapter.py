from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path

import pytest

from _adapters import RecordingHarnessAdapter, ScriptedNoOpAdapter
from trades_bench.adapter import (
    AdapterPrompt,
    AdapterResponse,
    AgentTurn,
    ControllerAdapter,
    HarnessAdapter,
    RunMetadata,
)
from trades_bench.enums import PromptKind


def test_no_op_adapter_conforms_to_the_protocol():
    adapter = ScriptedNoOpAdapter()
    assert isinstance(adapter, ControllerAdapter)


def test_no_op_adapter_returns_empty_completed_response_and_no_terminal_action():
    adapter = ScriptedNoOpAdapter()
    prompt = AdapterPrompt(
        kind=PromptKind.CALL, ts_ms=1_000, payload={"call_id": 7}
    )
    response = adapter.send(prompt)
    assert isinstance(response, AdapterResponse)
    assert response.text == ""
    assert response.terminal_action_taken is False
    assert adapter.prompts == [prompt]


def test_prompt_defaults_to_an_empty_payload_and_both_types_are_frozen():
    prompt = AdapterPrompt(kind=PromptKind.OPENING, ts_ms=0)
    assert prompt.payload == {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        prompt.ts_ms = 1
    response = AdapterResponse(text="done", terminal_action_taken=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        response.text = "other"


def test_recording_adapter_conforms_to_the_harness_protocol():
    assert isinstance(RecordingHarnessAdapter(), HarnessAdapter)


def test_harness_adapter_relays_prompts_and_reports_its_labels(tmp_path: Path):
    adapter = RecordingHarnessAdapter(model_id="model-x")
    workspace = tmp_path / "workspace"
    adapter.start_run(workspace, "system prompt", {"SUNRIDGE_API_PORT": "1"})
    adapter.start_day(date(2026, 10, 1))
    turn = adapter.send("hello", "opening", 900.0)
    adapter.end_day(date(2026, 10, 1))
    adapter.end_run("completed")

    assert isinstance(turn, AgentTurn)
    assert turn.ended_normally is True
    assert turn.input_tokens is None and turn.output_tokens is None
    assert adapter.started == (workspace, "system prompt", {"SUNRIDGE_API_PORT": "1"})
    assert adapter.sent == [("hello", "opening", 900.0)]
    assert adapter.days_started == [date(2026, 10, 1)] == adapter.days_ended
    assert adapter.ended_status == "completed"

    metadata = adapter.describe()
    assert metadata == RunMetadata(
        name="controlled",
        version="0.1.0",
        context_policy="controlled_daily_reset",
        attempt_definition=adapter.attempt_definition,
        model_id="model-x",
    )


def test_agent_turn_and_run_metadata_are_frozen():
    turn = AgentTurn(text="t", ended_normally=False, input_tokens=1, output_tokens=2)
    with pytest.raises(dataclasses.FrozenInstanceError):
        turn.text = "other"
    metadata = RunMetadata(
        name="pi",
        version="1",
        context_policy="controlled_daily_reset",
        attempt_definition="one completed model turn",
        model_id=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        metadata.model_id = "x"


def test_a_controller_adapter_is_not_a_harness_adapter():
    assert not isinstance(ScriptedNoOpAdapter(), HarnessAdapter)
