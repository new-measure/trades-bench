from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build.eligibility import classify_eligibility  # noqa: E402


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
    assert classify_eligibility({"amenity": "parking"}) == {"residential"}


@pytest.mark.parametrize(
    "building",
    ["house", "detached", "semidetached_house", "terrace", "bungalow"],
)
def test_residential_building_values(building):
    assert classify_eligibility({"building": building}) == {"residential"}


def test_no_tags_defaults_to_residential():
    assert classify_eligibility({}) == {"residential"}


def test_unrecognized_building_value_defaults_to_residential():
    assert classify_eligibility({"building": "yes"}) == {"residential"}


def test_candidate_may_carry_more_than_one_eligibility():
    result = classify_eligibility({"building": "office", "shop": "electronics"})
    assert result == {"commercial"}


def test_eligibility_result_is_a_frozenset():
    result = classify_eligibility({"building": "house"})
    assert isinstance(result, frozenset)


def test_unit_tag_present_adds_apartment_without_dropping_the_residential_fallback():
    result = classify_eligibility({}, unit_tag_present=True)
    assert result == {"residential", "apartment"}


def test_unit_tag_present_adds_apartment_on_top_of_a_commercial_building():
    result = classify_eligibility({"building": "office"}, unit_tag_present=True)
    assert result == {"commercial", "apartment"}


def test_unit_tag_absent_and_no_footprint_leaves_classification_unchanged():
    result = classify_eligibility({"building": "house"})
    assert result == {"residential"}
    assert classify_eligibility(
        {"building": "house"}, unit_tag_present=False, inside_apartment_footprint=False
    ) == {"residential"}


def test_inside_apartment_footprint_adds_apartment_without_dropping_the_residential_fallback():
    result = classify_eligibility({}, inside_apartment_footprint=True)
    assert result == {"residential", "apartment"}


def test_inside_apartment_footprint_is_idempotent_with_an_already_apartment_building():
    result = classify_eligibility({"building": "apartments"}, inside_apartment_footprint=True)
    assert result == {"apartment"}


def test_both_new_signals_together_still_only_add_apartment_once():
    result = classify_eligibility(
        {"building": "house"}, unit_tag_present=True, inside_apartment_footprint=True
    )
    assert result == {"residential", "apartment"}
