"""Funnel-count accumulation (research/osm-data.md §8 items 2-3's machine-
readable half; the human-readable `data_quality.md` prose report itself is
CSR-003c's job — this module only produces the data structure and its
CSV/JSON serialization that report will read).

The reason vocabulary is deliberately open (a plain ``dict[str, int]``, not a
closed enum): this sub-issue's stages produce ``incomplete_address``,
``state_mismatch``, ``normalization_ambiguous``, ``normalization_invalid``,
``duplicate_key``, and ``duplicate_coordinate``; CSR-003c adds §4.8's
``unsnappable``/``unroutable`` on top without needing to touch this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Candidate, RejectedCandidate


@dataclass
class FunnelCounts:
    candidates_total: int = 0
    accepted: int = 0
    rejected_by_reason: dict[str, int] = field(default_factory=dict)
    #: §4.2's tag-provenance breakdown: how many rows took `city`/`state`
    #: from tags versus derivation (§8 item 3's second half).
    city_source_counts: dict[str, int] = field(default_factory=dict)
    state_source_counts: dict[str, int] = field(default_factory=dict)

    def record_extraction(
        self, candidates: list[Candidate], rejected: list[RejectedCandidate]
    ) -> None:
        """§4.2's output: every element considered becomes exactly one
        accepted candidate (counted toward `candidates_total` and the
        provenance breakdown) or one rejection.
        """
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
        """Later-stage rejections (normalize, dedupe): each was already
        counted once into `candidates_total` at extraction; this only
        tallies the reason.
        """
        for r in rejected:
            self._bump_reason(r.reason)

    def record_final_accepted(self, count: int) -> None:
        self.accepted = count

    def _bump_reason(self, reason: str) -> None:
        self.rejected_by_reason[reason] = self.rejected_by_reason.get(reason, 0) + 1

    def total_rejected(self) -> int:
        return sum(self.rejected_by_reason.values())

    def is_balanced(self) -> bool:
        """`candidates_total == accepted + sum(rejected)` — every candidate
        that entered the funnel left it exactly once, either accepted or
        with exactly one recorded reason.
        """
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
