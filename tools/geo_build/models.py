"""The candidate record threaded through every pipeline stage.

One :class:`Candidate` is created per hard-tag-complete OSM element
(research/osm-data.md §4.2) and is progressively filled in by
:mod:`tools.geo_build.normalize_stage`, :mod:`tools.geo_build.eligibility`,
and :mod:`tools.geo_build.zone_assign` before :mod:`tools.geo_build.strip`
projects it down to the 03 §3.1 catalog columns. A candidate that fails any
stage becomes a :class:`RejectedCandidate` instead and takes no further part
in the pipeline (research/osm-data.md §6: ``rejected.csv`` — "build QA only").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass
class Candidate:
    # -- identity (§4.2) --------------------------------------------------
    source_type: str  # "node" | "way" | "relation"
    source_id: int
    longitude: float
    latitude: float

    # -- extracted / derived (§4.2) ---------------------------------------
    house_number_raw: str
    street_raw: str
    postal_code_raw: str
    city: str
    city_source: str  # "tag" | "admin_relation" | "zone"
    state: str
    state_source: str  # "tag" | "crop"

    #: Every OSM tag on the source element, carried through only so the §4.5
    #: eligibility stage (which runs *after* dedupe) can still see them.
    #: Never part of the final catalog row (§4.6 strips it, along with every
    #: other non-catalog tag) and never serialized.
    tags: Mapping[str, str] = field(default_factory=dict)

    # -- normalize() output (§4.3) -----------------------------------------
    house_number: str | None = None
    street: str | None = None
    unit: str | None = None
    postal_code: str | None = None
    formatted_address: str | None = None
    canonical_key: str | None = None

    # -- eligibility (§4.5) -------------------------------------------------
    segment_eligibility: frozenset[str] = field(default_factory=frozenset)

    # -- zone assignment (§4.7) ---------------------------------------------
    zone_id: int | None = None
    effective_zone_id: int | None = None


@dataclass(frozen=True)
class RejectedCandidate:
    source_type: str
    source_id: int
    reason: str
    detail: str = ""


__all__ = ["Candidate", "RejectedCandidate"]
