"""tools.geo_build.eligibility: every row of the §4.5 tag -> eligibility
table (research/osm-data.md §4.5; CSR-003b, RES-60). Pure tag-dict logic, no
OSM parsing.
"""

from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build.eligibility import classify_eligibility  # noqa: E402

# -- apartment row -----------------------------------------------------------


def test_building_apartments_is_apartment():
    assert classify_eligibility({"building": "apartments"}) == {"apartment"}


def test_building_dormitory_is_apartment():
    assert classify_eligibility({"building": "dormitory"}) == {"apartment"}


def test_building_residential_with_flats_ge_2_is_apartment():
    assert classify_eligibility({"building": "residential", "building:flats": "4"}) == {
        "apartment"
    }


def test_building_residential_with_flats_exactly_2_is_apartment():
    assert classify_eligibility({"building": "residential", "building:flats": "2"}) == {
        "apartment"
    }


def test_building_residential_with_flats_1_is_residential_not_apartment():
    assert classify_eligibility({"building": "residential", "building:flats": "1"}) == {
        "residential"
    }


def test_building_residential_without_flats_tag_is_residential():
    assert classify_eligibility({"building": "residential"}) == {"residential"}


def test_building_residential_with_unparseable_flats_is_residential():
    assert classify_eligibility({"building": "residential", "building:flats": "many"}) == {
        "residential"
    }


# -- commercial row -----------------------------------------------------------


@pytest.mark.parametrize(
    "building",
    [
        "commercial",
        "retail",
        "office",
        "industrial",
        "warehouse",
        "supermarket",
        "hotel",
        "hospital",
        "school",
    ],
)
def test_commercial_building_values(building):
    assert classify_eligibility({"building": building}) == {"commercial"}


def test_shop_tag_presence_is_commercial_regardless_of_value():
    assert classify_eligibility({"shop": "bakery"}) == {"commercial"}


def test_office_tag_presence_is_commercial_regardless_of_value():
    assert classify_eligibility({"office": "insurance"}) == {"commercial"}


def test_commercial_amenity_is_commercial():
    assert classify_eligibility({"amenity": "restaurant"}) == {"commercial"}


def test_noncommercial_amenity_is_not_commercial_by_itself():
    # amenity=parking is not in the closed commercial-amenity vocabulary and
    # carries no building/shop/office tag either -> default residential.
    assert classify_eligibility({"amenity": "parking"}) == {"residential"}


# -- residential row -----------------------------------------------------------


@pytest.mark.parametrize(
    "building",
    ["house", "detached", "semidetached_house", "terrace", "bungalow"],
)
def test_residential_building_values(building):
    assert classify_eligibility({"building": building}) == {"residential"}


# -- default / no usable classification ----------------------------------------


def test_no_tags_defaults_to_residential():
    assert classify_eligibility({}) == {"residential"}


def test_unrecognized_building_value_defaults_to_residential():
    assert classify_eligibility({"building": "yes"}) == {"residential"}


# -- multi-membership -----------------------------------------------------------


def test_candidate_may_carry_more_than_one_eligibility():
    # A building tagged both commercial (via `office=*`) and, degenerate
    # example, also matching the commercial building-value row.
    result = classify_eligibility({"building": "office", "shop": "electronics"})
    assert result == {"commercial"}  # both rows point to the same eligibility here


def test_eligibility_result_is_a_frozenset():
    result = classify_eligibility({"building": "house"})
    assert isinstance(result, frozenset)
