from __future__ import annotations

from csr_bench.adapter import AdapterPrompt, AdapterResponse


class ScriptedNoOpAdapter:

    def __init__(self) -> None:
        self.prompts: list[AdapterPrompt] = []

    def send(self, prompt: AdapterPrompt) -> AdapterResponse:
        self.prompts.append(prompt)
        return AdapterResponse(text="", terminal_action_taken=False)
