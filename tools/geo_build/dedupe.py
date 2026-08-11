from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .geometry import haversine_km
from .models import Candidate, RejectedCandidate

REASON_DUPLICATE_KEY = "duplicate_key"
REASON_DUPLICATE_COORDINATE = "duplicate_coordinate"

_SOURCE_TYPE_RANK: dict[str, int] = {"node": 0, "way": 1, "relation": 2}


def dedupe_sort_key(candidate: Candidate) -> tuple[int, int]:
    return (_SOURCE_TYPE_RANK[candidate.source_type], candidate.source_id)


def dedupe(
    candidates: list[Candidate], duplicate_coordinate_meters: Decimal
) -> tuple[list[Candidate], list[RejectedCandidate]]:
    ordered = sorted(candidates, key=dedupe_sort_key)
    radius_m = float(duplicate_coordinate_meters)

    accepted: list[Candidate] = []
    rejected: list[RejectedCandidate] = []
    seen_keys: set[str] = set()
    accepted_by_street: dict[str, list[Candidate]] = defaultdict(list)

    for c in ordered:
        assert c.canonical_key is not None
        if c.canonical_key in seen_keys:
            rejected.append(
                RejectedCandidate(
                    source_type=c.source_type,
                    source_id=c.source_id,
                    reason=REASON_DUPLICATE_KEY,
                    detail=c.canonical_key,
                )
            )
            continue

        is_duplicate_coordinate = False
        for other in accepted_by_street.get(c.street or "", ()):
            distance_m = (
                haversine_km(c.longitude, c.latitude, other.longitude, other.latitude) * 1000.0
            )
            if distance_m <= radius_m:
                is_duplicate_coordinate = True
                break
        if is_duplicate_coordinate:
            rejected.append(
                RejectedCandidate(
                    source_type=c.source_type,
                    source_id=c.source_id,
                    reason=REASON_DUPLICATE_COORDINATE,
                    detail=c.street or "",
                )
            )
            continue

        seen_keys.add(c.canonical_key)
        accepted_by_street[c.street or ""].append(c)
        accepted.append(c)

    return accepted, rejected


__all__ = [
    "REASON_DUPLICATE_COORDINATE",
    "REASON_DUPLICATE_KEY",
    "dedupe",
    "dedupe_sort_key",
]
