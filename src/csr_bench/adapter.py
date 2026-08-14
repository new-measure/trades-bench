from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .enums import PromptKind


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
class HarnessAdapter(Protocol):

    def send(self, prompt: AdapterPrompt) -> AdapterResponse: ...


__all__ = ["AdapterPrompt", "AdapterResponse", "HarnessAdapter"]
