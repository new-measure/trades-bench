from __future__ import annotations

from .anchors import ACCESS_DISTANCE_DECIMAL_PLACES
from .models import Candidate

CATALOG_COLUMNS: tuple[str, ...] = (
    "address_id",
    "dataset_version",
    "house_number",
    "street",
    "city",
    "state",
    "postal_code",
    "formatted_address",
    "canonical_key",
    "latitude",
    "longitude",
    "zone_id",
    "effective_zone_id",
    "anchor_id",
    "access_distance_km",
    "segment_eligibility",
    "status",
    "unusable_reason",
    "created_at",
)

PENDING_STATUS = "pending"

COORDINATE_DECIMAL_PLACES = 6

CREATED_AT_SENTINEL = 0


def to_catalog_row(candidate: Candidate, dataset_version: str) -> dict[str, object]:
    if candidate.formatted_address is None or candidate.canonical_key is None:
        raise ValueError(
            f"candidate {candidate.source_type}/{candidate.source_id} has not "
            "been normalized (run normalize_stage before strip)"
        )
    return {
        "address_id": None,
        "dataset_version": dataset_version,
        "house_number": candidate.house_number,
        "street": candidate.street,
        "city": candidate.city,
        "state": candidate.state,
        "postal_code": candidate.postal_code,
        "formatted_address": candidate.formatted_address,
        "canonical_key": candidate.canonical_key,
        "latitude": f"{candidate.latitude:.{COORDINATE_DECIMAL_PLACES}f}",
        "longitude": f"{candidate.longitude:.{COORDINATE_DECIMAL_PLACES}f}",
        "zone_id": candidate.zone_id,
        "effective_zone_id": candidate.effective_zone_id,
        "anchor_id": candidate.anchor_id,
        "access_distance_km": (
            None
            if candidate.access_distance_km is None
            else f"{candidate.access_distance_km:.{ACCESS_DISTANCE_DECIMAL_PLACES}f}"
        ),
        "segment_eligibility": ",".join(sorted(candidate.segment_eligibility)),
        "status": candidate.status,
        "unusable_reason": candidate.unusable_reason,
        "created_at": CREATED_AT_SENTINEL,
    }


__all__ = [
    "CATALOG_COLUMNS",
    "COORDINATE_DECIMAL_PLACES",
    "CREATED_AT_SENTINEL",
    "PENDING_STATUS",
    "to_catalog_row",
]
