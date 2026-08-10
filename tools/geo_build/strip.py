"""Strip stage (research/osm-data.md §4.6): project a fully-processed
:class:`~tools.geo_build.models.Candidate` down to exactly the 03 §3.1
catalog column list.

``address_id`` is intentionally not assigned here — it is opaque row
identity, assigned once by :func:`tools.geo_build.serialize.assign_address_ids`
after the whole catalog is sorted, so it stays a pure function of the final
deterministic row order rather than of OSM element ids (which the catalog
Must not expose — 03 §3.1: "no OSM ids").

``anchor_id``, ``access_distance_km``, and `status`/`unusable_reason`'s real
values are §4.8 routability (CSR-003c). This stage emits them as
placeholders: `anchor_id`/`access_distance_km` empty, and `status` as the
sentinel `"pending"` — deliberately *not* one of 03 §3.1's own
`usable | unusable` enum members, so a reader can never mistake a
003b-emitted row for one CSR-003c has already routed. Recorded as a
deviation in `docs/tickets/CSR-003.md`'s implementation note for this
sub-issue.
"""

from __future__ import annotations

from .models import Candidate

#: 03 §3.1's exact column order — the one authoritative list this module
#: must reproduce (research/osm-data.md §4.6: "must not restate a divergent
#: list", so this tuple is the single place the order is spelled).
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

#: §4.8 is out of scope for this sub-issue (CSR-003c); a value outside the
#: real `usable | unusable` enum flags every emitted row as not-yet-routed.
PENDING_STATUS = "pending"

#: Decision 3 / research/osm-data.md §7: "Coordinates Must be stored at a
#: fixed decimal precision so serialization is stable." ~0.11m at the
#: equator — comfortably finer than any dedupe/snap tolerance this pipeline
#: configures.
COORDINATE_DECIMAL_PLACES = 6

#: research/osm-data.md conventions: "no timestamps inside hashed content."
#: `created_at` mirrors the engine's own deterministic pre-world sentinel
#: pattern (`migrations.py`'s `applied_at = 0`).
CREATED_AT_SENTINEL = 0


def to_catalog_row(candidate: Candidate, dataset_version: str) -> dict[str, object]:
    """Project ``candidate`` (already normalized, deduped, classified, and
    zone-assigned) to a 03 §3.1 row, keyed exactly by :data:`CATALOG_COLUMNS`
    (``address_id`` filled in later, see module docstring).
    """
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
        "anchor_id": None,
        "access_distance_km": None,
        "segment_eligibility": ",".join(sorted(candidate.segment_eligibility)),
        "status": PENDING_STATUS,
        "unusable_reason": None,
        "created_at": CREATED_AT_SENTINEL,
    }


__all__ = [
    "CATALOG_COLUMNS",
    "COORDINATE_DECIMAL_PLACES",
    "CREATED_AT_SENTINEL",
    "PENDING_STATUS",
    "to_catalog_row",
]
