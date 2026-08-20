from __future__ import annotations

from policies.base import PolicyOutcome, PolicyRequest

POLICY_NAME = "do_nothing"
POLICY_VERSION = "1.0.0"


class DoNothingPolicy:

    def __init__(self, *, name: str = POLICY_NAME, version: str = POLICY_VERSION) -> None:
        self.name = name
        self.version = version

    def decide(self, request: PolicyRequest) -> PolicyOutcome:
        return PolicyOutcome()


__all__ = ["POLICY_NAME", "POLICY_VERSION", "DoNothingPolicy"]
