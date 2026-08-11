from __future__ import annotations

from collections.abc import Sequence

from .models import Candidate
from .routing import RoutingBackend

STATUS_USABLE = "usable"
STATUS_UNUSABLE = "unusable"

REASON_UNSNAPPABLE = "unsnappable"
REASON_UNROUTABLE = "unroutable"


def snap_candidates(
    candidates: Sequence[Candidate],
    backend: RoutingBackend,
    *,
    max_snap_distance_meters: float,
) -> None:
    for candidate in candidates:
        snapped = backend.snap(candidate.longitude, candidate.latitude)
        if snapped is None or snapped.distance_meters > max_snap_distance_meters:
            candidate.status = STATUS_UNUSABLE
            candidate.unusable_reason = REASON_UNSNAPPABLE
            candidate.snap_distance_meters = None if snapped is None else snapped.distance_meters
            continue
        candidate.status = STATUS_USABLE
        candidate.unusable_reason = None
        candidate.snapped_lon = snapped.lon
        candidate.snapped_lat = snapped.lat
        candidate.snap_distance_meters = snapped.distance_meters


def mark_unroutable(candidate: Candidate) -> None:
    candidate.status = STATUS_UNUSABLE
    candidate.unusable_reason = REASON_UNROUTABLE
    candidate.anchor_id = None
    candidate.access_distance_km = None


def usable(candidates: Sequence[Candidate]) -> list[Candidate]:
    return [c for c in candidates if c.status == STATUS_USABLE]


__all__ = [
    "REASON_UNROUTABLE",
    "REASON_UNSNAPPABLE",
    "STATUS_UNUSABLE",
    "STATUS_USABLE",
    "mark_unroutable",
    "snap_candidates",
    "usable",
]
