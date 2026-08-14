from __future__ import annotations

import dataclasses

import pytest

from _adapters import ScriptedNoOpAdapter
from csr_bench.adapter import AdapterPrompt, AdapterResponse, HarnessAdapter
from csr_bench.enums import PromptKind


def test_no_op_adapter_conforms_to_the_protocol():
    adapter = ScriptedNoOpAdapter()
    assert isinstance(adapter, HarnessAdapter)


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
