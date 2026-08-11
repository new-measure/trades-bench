from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass
class Candidate:
    source_type: str
    source_id: int
    longitude: float
    latitude: float

    house_number_raw: str
    street_raw: str
    postal_code_raw: str
    city: str
    city_source: str
    state: str
    state_source: str

    tags: Mapping[str, str] = field(default_factory=dict)

    house_number: str | None = None
    street: str | None = None
    unit: str | None = None
    postal_code: str | None = None
    formatted_address: str | None = None
    canonical_key: str | None = None

    segment_eligibility: frozenset[str] = field(default_factory=frozenset)

    zone_id: int | None = None
    effective_zone_id: int | None = None

    snapped_lon: float | None = None
    snapped_lat: float | None = None
    snap_distance_meters: float | None = None

    anchor_id: int | None = None
    access_distance_km: float | None = None

    status: str = "pending"
    unusable_reason: str | None = None


@dataclass(frozen=True)
class RejectedCandidate:
    source_type: str
    source_id: int
    reason: str
    detail: str = ""


__all__ = ["Candidate", "RejectedCandidate"]
