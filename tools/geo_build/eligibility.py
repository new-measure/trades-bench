from __future__ import annotations

from collections.abc import Mapping

SEGMENT_ELIGIBILITY_VALUES: frozenset[str] = frozenset({"residential", "apartment", "commercial"})

_APARTMENT_BUILDING_VALUES = frozenset({"apartments", "dormitory"})

_COMMERCIAL_BUILDING_VALUES = frozenset(
    {"commercial", "retail", "office", "industrial", "warehouse", "supermarket", "hotel",
     "hospital", "school"}
)

_RESIDENTIAL_BUILDING_VALUES = frozenset(
    {"house", "detached", "semidetached_house", "terrace", "bungalow"}
)

_COMMERCIAL_AMENITY_VALUES = frozenset(
    {
        "restaurant", "cafe", "fast_food", "bar", "pub", "bank", "pharmacy", "fuel",
        "car_wash", "car_rental", "cinema", "theatre", "marketplace", "clinic",
        "dentist", "veterinary",
    }
)

_APARTMENT_MIN_FLATS = 2


def _flats_count(tags: Mapping[str, str]) -> int | None:
    raw = tags.get("building:flats")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _classify_from_tags(tags: Mapping[str, str]) -> set[str]:
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
        result.add("residential")

    return result


def classify_eligibility(
    tags: Mapping[str, str],
    *,
    unit_tag_present: bool = False,
    inside_apartment_footprint: bool = False,
) -> frozenset[str]:
    result = _classify_from_tags(tags)
    if unit_tag_present or inside_apartment_footprint:
        result.add("apartment")
    return frozenset(result)


__all__ = ["SEGMENT_ELIGIBILITY_VALUES", "classify_eligibility"]
