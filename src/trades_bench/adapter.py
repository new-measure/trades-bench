from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from .enums import PromptKind

HarnessPromptKind = Literal["opening", "call", "reminder", "closing"]


@dataclass(frozen=True)
class AdapterPrompt:

    kind: PromptKind
    ts_ms: int
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterResponse:

    text: str
    terminal_action_taken: bool


@runtime_checkable
class ControllerAdapter(Protocol):

    def send(self, prompt: AdapterPrompt) -> AdapterResponse: ...


@dataclass(frozen=True)
class AgentTurn:

    text: str
    ended_normally: bool
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class RunMetadata:

    name: str
    version: str
    context_policy: str
    attempt_definition: str
    model_id: str | None


@runtime_checkable
class HarnessAdapter(Protocol):

    name: str
    version: str
    context_policy: str
    attempt_definition: str

    def start_run(
        self, workspace: Path, system_prompt: str, env: Mapping[str, str]
    ) -> None: ...

    def start_day(self, service_date: date) -> None: ...

    def send(self, prompt: str, kind: HarnessPromptKind, deadline_s: float) -> AgentTurn: ...

    def end_day(self, service_date: date) -> None: ...

    def end_run(self, status: str) -> None: ...

    def describe(self) -> RunMetadata: ...


__all__ = [
    "AdapterPrompt",
    "AdapterResponse",
    "AgentTurn",
    "ControllerAdapter",
    "HarnessAdapter",
    "HarnessPromptKind",
    "RunMetadata",
]
