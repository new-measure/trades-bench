from __future__ import annotations

import re
from dataclasses import dataclass

from . import zone_assign
from .geometry import containing_index
from .models import Candidate, RejectedCandidate
from .osm_reader import OsmElements, OsmNode, OsmRelation, OsmWay
from .zones import ZONE_CODES, MunicipalRelation, ZoneExtractionResult, seeded_zone_cities

REASON_INCOMPLETE_ADDRESS = "incomplete_address"
REASON_STATE_MISMATCH = "state_mismatch"

_REQUIRED_STATE = "AZ"
_ZIP_DIGITS_RE = re.compile(r"[^0-9]")

_DROPPED_UNIT_TAGS = frozenset({"addr:unit", "addr:flats"})


def _has_any_addr_tag(tags: dict[str, str]) -> bool:
    return any(k.startswith("addr:") for k in tags)


@dataclass(frozen=True)
class ExtractionContext:

    municipal_relations: tuple[MunicipalRelation, ...]
    zone_polygons_in_order: list
    principal_city_by_zone_code: dict[str, str]


def build_extraction_context(zone_result: ZoneExtractionResult) -> ExtractionContext:
    seeded = seeded_zone_cities()
    principal_city_by_zone_code = {code: seeded[code][0] for code in ZONE_CODES}
    return ExtractionContext(
        municipal_relations=zone_result.municipal_relations,
        zone_polygons_in_order=zone_result.zone_polygons_in_order(),
        principal_city_by_zone_code=principal_city_by_zone_code,
    )


def _zip_reduces_to_five_digits(raw: str) -> bool:
    digits = _ZIP_DIGITS_RE.sub("", raw)
    return len(digits) in (5, 9)


def _derive_city(
    longitude: float, latitude: float, ctx: ExtractionContext
) -> tuple[str, str]:
    idx = containing_index(
        longitude, latitude, [m.polygon for m in ctx.municipal_relations]
    )
    if idx is not None:
        return ctx.municipal_relations[idx].name, "admin_relation"
    _, effective_zone_id = zone_assign.assign_zone(
        longitude, latitude, ctx.zone_polygons_in_order
    )
    zone_code = ZONE_CODES[effective_zone_id - 1]
    return ctx.principal_city_by_zone_code[zone_code], "zone"


def _extract_one(
    source_type: str,
    source_id: int,
    tags: dict[str, str],
    centroid: tuple[float, float] | None,
    ctx: ExtractionContext,
) -> Candidate | RejectedCandidate:
    house_number = tags.get("addr:housenumber")
    street = tags.get("addr:street")
    postcode = tags.get("addr:postcode")

    missing = [
        name
        for name, value in (
            ("addr:housenumber", house_number),
            ("addr:street", street),
            ("addr:postcode", postcode),
        )
        if not value
    ]
    if missing:
        return RejectedCandidate(
            source_type=source_type,
            source_id=source_id,
            reason=REASON_INCOMPLETE_ADDRESS,
            detail=f"missing tags: {', '.join(missing)}",
        )
    assert house_number is not None and street is not None and postcode is not None
    if not _zip_reduces_to_five_digits(postcode):
        return RejectedCandidate(
            source_type=source_type,
            source_id=source_id,
            reason=REASON_INCOMPLETE_ADDRESS,
            detail=f"addr:postcode {postcode!r} does not reduce to a 5-digit ZIP",
        )

    state_tag = tags.get("addr:state")
    if state_tag is not None:
        if state_tag.strip().upper() != _REQUIRED_STATE:
            return RejectedCandidate(
                source_type=source_type,
                source_id=source_id,
                reason=REASON_STATE_MISMATCH,
                detail=f"addr:state={state_tag!r} does not normalize to AZ (crop leak)",
            )
        state, state_source = _REQUIRED_STATE, "tag"
    else:
        state, state_source = _REQUIRED_STATE, "crop"

    if centroid is None:
        return RejectedCandidate(
            source_type=source_type,
            source_id=source_id,
            reason=REASON_INCOMPLETE_ADDRESS,
            detail="no resolvable geometry for centroid",
        )
    longitude, latitude = centroid

    city_tag = tags.get("addr:city")
    if city_tag:
        city, city_source = city_tag, "tag"
    else:
        city, city_source = _derive_city(longitude, latitude, ctx)

    return Candidate(
        source_type=source_type,
        source_id=source_id,
        longitude=longitude,
        latitude=latitude,
        house_number_raw=house_number,
        street_raw=street,
        postal_code_raw=postcode,
        city=city,
        city_source=city_source,
        state=state,
        state_source=state_source,
        tags={k: v for k, v in tags.items() if k not in _DROPPED_UNIT_TAGS},
        unit_tag_present=any(k in tags for k in _DROPPED_UNIT_TAGS),
    )


def extract_candidates(
    elements: OsmElements, ctx: ExtractionContext
) -> tuple[list[Candidate], list[RejectedCandidate]]:
    candidates: list[Candidate] = []
    rejected: list[RejectedCandidate] = []

    typed_elements: tuple[tuple[str, tuple], ...] = (
        ("node", elements.nodes),
        ("way", elements.ways),
        ("relation", elements.relations),
    )
    for source_type, group in typed_elements:
        for element in group:
            tags = dict(element.tags)
            if not _has_any_addr_tag(tags):
                continue

            element_id = element.osm_id
            if isinstance(element, OsmRelation):
                centroid = elements.relation_centroid(element)
            elif isinstance(element, (OsmNode, OsmWay)):
                centroid = element.centroid
            else:  # pragma: no cover - exhaustive by construction
                raise TypeError(f"unexpected element type {type(element)!r}")

            result = _extract_one(source_type, element_id, tags, centroid, ctx)
            if isinstance(result, RejectedCandidate):
                rejected.append(result)
            else:
                candidates.append(result)

    return candidates, rejected


__all__ = [
    "REASON_INCOMPLETE_ADDRESS",
    "REASON_STATE_MISMATCH",
    "ExtractionContext",
    "build_extraction_context",
    "extract_candidates",
]
