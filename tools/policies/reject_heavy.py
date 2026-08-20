from __future__ import annotations

import hashlib

from policies.accept_all import accept_all_decision, reject
from policies.base import PolicyOutcome, PolicyRequest
from trades_bench.config import RunConfig

POLICY_NAME = "reject_heavy"
POLICY_VERSION = "1.0.0"

REJECT_PROBABILITY_KEY = "reference_policies.reject_heavy.reject_probability"

_DRAW_BYTES = 8


def _length_prefixed(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def reject_draw(policy_name: str, master_seed: int, call_id: int) -> int:
    body = (
        _length_prefixed(policy_name)
        + int(master_seed).to_bytes(8, "big")
        + int(call_id).to_bytes(8, "big")
    )
    return int.from_bytes(hashlib.sha256(body).digest()[:_DRAW_BYTES], "big")


class RejectHeavyPolicy:

    def __init__(
        self,
        *,
        master_seed: int,
        reject_threshold: int,
        name: str = POLICY_NAME,
        version: str = POLICY_VERSION,
    ) -> None:
        self.name = name
        self.version = version
        self.master_seed = int(master_seed)
        self.reject_threshold = int(reject_threshold)

    @classmethod
    def from_config(
        cls, config: RunConfig, *, master_seed: int, name: str = POLICY_NAME
    ) -> RejectHeavyPolicy:
        return cls(
            master_seed=master_seed,
            reject_threshold=config.get_prob_threshold(REJECT_PROBABILITY_KEY),
            name=name,
        )

    def rejects(self, call_id: int) -> bool:
        return reject_draw(self.name, self.master_seed, call_id) < self.reject_threshold

    def decide(self, request: PolicyRequest) -> PolicyOutcome:
        if request.call.is_cancellation:
            return accept_all_decision(request)
        if self.rejects(request.call.call_id):
            return reject(request, "keyed draw fell below the reject probability")
        return accept_all_decision(request)


__all__ = [
    "POLICY_NAME",
    "POLICY_VERSION",
    "REJECT_PROBABILITY_KEY",
    "RejectHeavyPolicy",
    "reject_draw",
]
