from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build import normalize_stage  # noqa: E402
from geo_build.models import Candidate  # noqa: E402
from trades_bench.address import load_default_abbreviation_table  # noqa: E402

TABLE = load_default_abbreviation_table()


def _candidate(house_number="100", street="Main St", city="Scottsdale", zip_code="85251"):
    return Candidate(
        source_type="node",
        source_id=1,
        longitude=-111.9,
        latitude=33.5,
        house_number_raw=house_number,
        street_raw=street,
        postal_code_raw=zip_code,
        city=city,
        city_source="tag",
        state="AZ",
        state_source="crop",
    )


def test_compose_address_text_matches_normalize_grammar():
    text = normalize_stage.compose_address_text("100", "Main St", "Scottsdale", "AZ", "85251")
    assert text == "100 Main St, Scottsdale, AZ 85251"


def test_normalize_candidate_success_fills_in_normalized_fields():
    c = _candidate()
    rejection = normalize_stage.normalize_candidate(c, TABLE)
    assert rejection is None
    assert c.formatted_address == "100 Main St, Scottsdale, AZ 85251"
    assert c.canonical_key == "100 MAIN ST, SCOTTSDALE, AZ 85251"
    assert c.house_number == "100"
    assert c.street == "Main St"
    assert c.unit is None


def test_normalize_candidate_applies_suffix_and_directional_abbreviation():
    c = _candidate(street="East Camelback Road")
    rejection = normalize_stage.normalize_candidate(c, TABLE)
    assert rejection is None
    assert c.street == "E Camelback Rd"


def test_normalize_candidate_grammar_failure_is_rejected():
    c = _candidate(house_number="!!!")
    rejection = normalize_stage.normalize_candidate(c, TABLE)
    assert rejection is not None
    assert rejection.reason == normalize_stage.REASON_GRAMMAR_INVALID
    assert rejection.source_type == "node"
    assert rejection.source_id == 1
    assert c.formatted_address is None


def test_normalize_candidate_ambiguous_table_is_rejected():
    ambiguous_table = {
        "suffixes": {"Road": "Rd", "ROAD": "Way"},
    }
    c = _candidate(street="100 Some Road")
    rejection = normalize_stage.normalize_candidate(c, ambiguous_table)
    assert rejection is not None
    assert rejection.reason == normalize_stage.REASON_AMBIGUOUS


def test_normalize_candidate_ambiguous_table_rejects_even_without_using_the_colliding_word():
    ambiguous_table = {
        "suffixes": {"Road": "Rd", "ROAD": "Way"},
    }
    c = _candidate(street="Main St")
    rejection = normalize_stage.normalize_candidate(c, ambiguous_table)
    assert rejection is not None
    assert rejection.reason == normalize_stage.REASON_AMBIGUOUS
