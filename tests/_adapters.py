from __future__ import annotations

import shutil
from pathlib import Path

from trades_bench.adapter import AdapterPrompt, AdapterResponse


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
