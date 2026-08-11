from __future__ import annotations

from dataclasses import dataclass, field

from .models import Candidate, RejectedCandidate


@dataclass
class FunnelCounts:
    candidates_total: int = 0
    accepted: int = 0
    rejected_by_reason: dict[str, int] = field(default_factory=dict)
    city_source_counts: dict[str, int] = field(default_factory=dict)
    state_source_counts: dict[str, int] = field(default_factory=dict)

    def record_extraction(
        self, candidates: list[Candidate], rejected: list[RejectedCandidate]
    ) -> None:
        self.candidates_total += len(candidates) + len(rejected)
        for c in candidates:
            self.city_source_counts[c.city_source] = self.city_source_counts.get(
                c.city_source, 0
            ) + 1
            self.state_source_counts[c.state_source] = self.state_source_counts.get(
                c.state_source, 0
            ) + 1
        for r in rejected:
            self._bump_reason(r.reason)

    def record_rejections(self, rejected: list[RejectedCandidate]) -> None:
        for r in rejected:
            self._bump_reason(r.reason)

    def record_unusable(self, reasons: list[str]) -> None:
        for reason in reasons:
            self._bump_reason(reason)

    def record_final_accepted(self, count: int) -> None:
        self.accepted = count

    def _bump_reason(self, reason: str) -> None:
        self.rejected_by_reason[reason] = self.rejected_by_reason.get(reason, 0) + 1

    def total_rejected(self) -> int:
        return sum(self.rejected_by_reason.values())

    def is_balanced(self) -> bool:
        return self.candidates_total == self.accepted + self.total_rejected()

    def to_dict(self) -> dict[str, object]:
        return {
            "candidates_total": self.candidates_total,
            "accepted": self.accepted,
            "rejected_by_reason": dict(sorted(self.rejected_by_reason.items())),
            "city_source_counts": dict(sorted(self.city_source_counts.items())),
            "state_source_counts": dict(sorted(self.state_source_counts.items())),
        }


__all__ = ["FunnelCounts"]
