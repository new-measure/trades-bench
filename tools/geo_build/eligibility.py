"""Segment eligibility classification (research/osm-data.md §4.5).

A candidate may carry more than one eligibility; the table's rows are not
mutually exclusive (a `building=commercial` object with `shop=*` matches the
commercial row twice over, which is fine — the result is a set). Eligibility
is `private_engine` (03 §12); this module only computes it, visibility is
CSR-003e's runtime schema concern.
"""

from __future__ import annotations

from collections.abc import Mapping

#: The closed three-member vocabulary (03 §3.1's `segment_eligibility` set).
#: Not a csr_bench enum because segment eligibility is computed only at build
#: time (this package); the runtime `_address_catalog` column CSR-003e adds
#: stores these same three literals as data, not as an engine-side enum.
SEGMENT_ELIGIBILITY_VALUES: frozenset[str] = frozenset({"residential", "apartment", "commercial"})

_APARTMENT_BUILDING_VALUES = frozenset({"apartments", "dormitory"})

_COMMERCIAL_BUILDING_VALUES = frozenset(
    {"commercial", "retail", "office", "industrial", "warehouse", "supermarket", "hotel",
     "hospital", "school"}
)

#: `building=residential` is deliberately excluded here — it is handled by
#: the dedicated flats-count branch in :func:`classify_eligibility` (row 1's
#: "with building:flats >= 2" qualifier only makes sense as a refinement of
#: row 3's `residential` entry, not an addition alongside it; a building
#: housing several units reads as `apartment`, not both).
_RESIDENTIAL_BUILDING_VALUES = frozenset(
    {"house", "detached", "semidetached_house", "terrace", "bungalow"}
)

#: "a commercial amenity=*" (§4.5) is not enumerated by the spec text. Read as
#: a closed vocabulary the way CSR-003a's unit-designator keywords were (see
#: `docs/tickets/CSR-003.md`'s implementation note for this sub-issue): the
#: retail/service amenities where a caller at that address is plausibly a
#: commercial customer, not every civic or community amenity OSM allows.
_COMMERCIAL_AMENITY_VALUES = frozenset(
    {
        "restaurant", "cafe", "fast_food", "bar", "pub", "bank", "pharmacy", "fuel",
        "car_wash", "car_rental", "cinema", "theatre", "marketplace", "clinic",
        "dentist", "veterinary",
    }
)

#: `building:flats >= 2` (§4.5's `building=residential` apartment carve-out)
#: threshold, taken verbatim from the spec table.
_APARTMENT_MIN_FLATS = 2


def _flats_count(tags: Mapping[str, str]) -> int | None:
    raw = tags.get("building:flats")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def classify_eligibility(tags: Mapping[str, str]) -> frozenset[str]:
    """Apply every row of the §4.5 table to ``tags``; default to
    ``{"residential"}`` when nothing else matched.
    """
    result: set[str] = set()

    building = tags.get("building")
    if building in _APARTMENT_BUILDING_VALUES:
        result.add("apartment")
    if building == "residential":
        flats = _flats_count(tags)
        if flats is not None and flats >= _APARTMENT_MIN_FLATS:
            result.add("apartment")
        else:
            result.add("residential")

    if (
        building in _COMMERCIAL_BUILDING_VALUES
        or "shop" in tags
        or "office" in tags
        or tags.get("amenity") in _COMMERCIAL_AMENITY_VALUES
    ):
        result.add("commercial")

    if building in _RESIDENTIAL_BUILDING_VALUES:
        result.add("residential")

    if not result:
        result.add("residential")  # "No usable classification" default (§4.5)

    return frozenset(result)


__all__ = ["SEGMENT_ELIGIBILITY_VALUES", "classify_eligibility"]
