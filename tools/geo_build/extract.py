"""Extract stage (research/osm-data.md §4.2).

Emits one :class:`~tools.geo_build.models.Candidate` per OSM node, way, or
relation that carries **at least one** ``addr:*`` tag (research/osm-data.md
line 76: "one candidate per OSM node, way, or relation that carries the
required address tags") — the candidate universe, not "every element the
crop kept". An element with no ``addr:*`` tag at all (a road, a park, a
power line — the overwhelming majority of any real extract) never becomes a
candidate and never appears in either `catalog.csv` or `rejected.csv`; one
that carries *some* `addr:*` tags but is missing a hard one (housenumber,
street, or a postcode reducible to 5 digits) is a candidate that gets
rejected `incomplete_address` — still "the single largest rejection class"
research/osm-data.md §4.2 expects, since partial addresses are the common
real-world case, not tagged-but-irrelevant infrastructure.

Deriving ``city``/``state`` when their tags are absent and recording the
provenance flag each derivation requires is also this stage's job. It does
**not** normalize (§4.3), dedupe (§4.4), classify eligibility (§4.5 — needs
the raw tags this stage still has, but the spec orders eligibility after
dedupe), or assign the final zone_id (§4.7, which runs on the survivors of
every earlier stage). It borrows the same zone-polygon nearest/containing
lookup §4.7 uses only to resolve the "zone" branch of its own city
derivation — a preview of that stage's geometry, not a substitute for it.
"""

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

#: research/osm-data.md §4.2: "addr:unit, addr:flats, and similar are
#: dropped — units live on the location record, not the catalog row." The
#: catalog row itself never carries any raw tag (§4.6 strip only ever reads
#: `Candidate`'s named fields), so this only needs to keep these two literal
#: examples out of `Candidate.tags` — the subset §4.5 eligibility still
#: consults for building/shop/office/amenity classification.
_DROPPED_UNIT_TAGS = frozenset({"addr:unit", "addr:flats"})


def _has_any_addr_tag(tags: dict[str, str]) -> bool:
    return any(k.startswith("addr:") for k in tags)


@dataclass(frozen=True)
class ExtractionContext:
    """Everything the extraction stage needs from the zone module, bundled
    once so :func:`extract_candidates` stays a pure function of
    ``(elements, context)``.
    """

    municipal_relations: tuple[MunicipalRelation, ...]
    zone_polygons_in_order: list  # BaseGeometry, ordered by ascending zone_id
    principal_city_by_zone_code: dict[str, str]


def build_extraction_context(zone_result: ZoneExtractionResult) -> ExtractionContext:
    """§4.2's "zone" city-derivation fallback needs a *display* name per
    zone. Reusing `seeded_zone_cities()`'s first entry gives exactly that:
    for the seven single-municipality zones it is the municipality's own
    name; for the Central/North Phoenix split it is "Central Phoenix" /
    "North Phoenix" (the zone's own name, since neither is a real
    municipality — the shared "Phoenix" entry those two zones also carry is
    used by the *other* branch, admin_relation, via
    `ZoneExtractionResult.municipal_relations`); for the composite it is the
    first of the three seeded component cities ("Goodyear"), an arbitrary
    but deterministic and documented tie-break for the rare point that is
    outlying with respect to all three components yet nearest that zone.
    """
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
        # A minimal strip/uppercase equality check — not a reimplementation
        # of `normalize()`'s state rule (which also folds punctuation and is
        # gated on the full three-segment grammar); this only decides the
        # §4.2 crop-leak rejection *before* any candidate reaches §4.3.
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
    )


def extract_candidates(
    elements: OsmElements, ctx: ExtractionContext
) -> tuple[list[Candidate], list[RejectedCandidate]]:
    """Walk every node/way/relation in ``elements`` (already cropped) that
    carries at least one ``addr:*`` tag — the candidate universe (module
    docstring) — and emit one :class:`Candidate` or :class:`RejectedCandidate`
    for each. An element with no ``addr:*`` tag at all (every road, park,
    powerline, and administrative-boundary way/relation the crop also kept)
    is skipped entirely: it never enters the funnel, on either side.
    """
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
