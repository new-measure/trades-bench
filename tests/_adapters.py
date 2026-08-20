from __future__ import annotations

import shutil
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from trades_bench.adapter import (
    AdapterPrompt,
    AdapterResponse,
    AgentTurn,
    HarnessPromptKind,
    RunMetadata,
)


class ScriptedNoOpAdapter:

    def __init__(self) -> None:
        self.prompts: list[AdapterPrompt] = []

    def send(self, prompt: AdapterPrompt) -> AdapterResponse:
        self.prompts.append(prompt)
        return AdapterResponse(text="", terminal_action_taken=False)


class CrashOnNthPromptAdapter(ScriptedNoOpAdapter):

    def __init__(self, crash_on_prompt: int) -> None:
        super().__init__()
        self.crash_on_prompt = crash_on_prompt

    def send(self, prompt: AdapterPrompt) -> AdapterResponse:
        if len(self.prompts) + 1 == self.crash_on_prompt:
            raise ConnectionError("adapter crashed before responding")
        return super().send(prompt)


class SnapshotOnNthPromptAdapter(ScriptedNoOpAdapter):

    def __init__(self, snapshot_on_prompt: int, source: Path, destination: Path) -> None:
        super().__init__()
        self.snapshot_on_prompt = snapshot_on_prompt
        self.source = source
        self.destination = destination
        self.snapshot_prompt: AdapterPrompt | None = None

    def send(self, prompt: AdapterPrompt) -> AdapterResponse:
        if len(self.prompts) + 1 == self.snapshot_on_prompt:
            self.snapshot_prompt = prompt
            shutil.copyfile(self.source, self.destination)
        return super().send(prompt)


class RecordingHarnessAdapter:

    name = "controlled"
    version = "0.1.0"
    context_policy = "controlled_daily_reset"
    attempt_definition = (
        "one completed model turn in reply to the active call prompt or a reminder, "
        "with no terminal action committed"
    )

    def __init__(self, model_id: str | None = None) -> None:
        self.model_id = model_id
        self.started: tuple[Path, str, Mapping[str, str]] | None = None
        self.days_started: list[date] = []
        self.days_ended: list[date] = []
        self.sent: list[tuple[str, str, float]] = []
        self.ended_status: str | None = None

    def start_run(
        self, workspace: Path, system_prompt: str, env: Mapping[str, str]
    ) -> None:
        self.started = (workspace, system_prompt, dict(env))

    def start_day(self, service_date: date) -> None:
        self.days_started.append(service_date)

    def send(self, prompt: str, kind: HarnessPromptKind, deadline_s: float) -> AgentTurn:
        self.sent.append((prompt, kind, deadline_s))
        return AgentTurn(
            text="", ended_normally=True, input_tokens=None, output_tokens=None
        )

    def end_day(self, service_date: date) -> None:
        self.days_ended.append(service_date)

    def end_run(self, status: str) -> None:
        self.ended_status = status

    def describe(self) -> RunMetadata:
        return RunMetadata(
            name=self.name,
            version=self.version,
            context_policy=self.context_policy,
            attempt_definition=self.attempt_definition,
            model_id=self.model_id,
        )
