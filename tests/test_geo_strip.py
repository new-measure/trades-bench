"""tools.geo_build.strip: projection to exactly the 03 §3.1 catalog columns
(research/osm-data.md §4.6; CSR-003b, RES-60).
"""

from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build import strip  # noqa: E402
from geo_build.models import Candidate  # noqa: E402


def _normalized_candidate():
    c = Candidate(
        source_type="node",
        source_id=42,
        longitude=-111.9012345,
        latitude=33.5098765,
        house_number_raw="100",
        street_raw="Main St",
        postal_code_raw="85251",
        city="Scottsdale",
        city_source="admin_relation",
        state="AZ",
        state_source="crop",
    )
    c.house_number = "100"
    c.street = "Main St"
    c.city = "Scottsdale"
    c.state = "AZ"
    c.postal_code = "85251"
    c.formatted_address = "100 Main St, Scottsdale, AZ 85251"
    c.canonical_key = "100 MAIN ST, SCOTTSDALE, AZ 85251"
    c.segment_eligibility = frozenset({"commercial", "apartment"})
    c.zone_id = 3
    c.effective_zone_id = 3
    return c


def test_catalog_row_has_exactly_the_03_section_3_1_columns():
    row = strip.to_catalog_row(_normalized_candidate(), "dev-test")
    assert set(row.keys()) == set(strip.CATALOG_COLUMNS)


def test_catalog_row_excludes_osm_ids_and_tags():
    row = strip.to_catalog_row(_normalized_candidate(), "dev-test")
    serialized_keys = set(row.keys())
    assert "source_id" not in serialized_keys
    assert "source_type" not in serialized_keys
    assert "tags" not in serialized_keys
    assert "unit" not in serialized_keys  # 03 §3.1: "There is no unit column"


def test_catalog_row_values_carry_through():
    row = strip.to_catalog_row(_normalized_candidate(), "dev-test")
    assert row["dataset_version"] == "dev-test"
    assert row["house_number"] == "100"
    assert row["formatted_address"] == "100 Main St, Scottsdale, AZ 85251"
    assert row["canonical_key"] == "100 MAIN ST, SCOTTSDALE, AZ 85251"
    assert row["zone_id"] == 3
    assert row["effective_zone_id"] == 3


def test_catalog_row_segment_eligibility_is_sorted_and_joined():
    row = strip.to_catalog_row(_normalized_candidate(), "dev-test")
    assert row["segment_eligibility"] == "apartment,commercial"


def test_catalog_row_coordinates_have_fixed_decimal_precision():
    row = strip.to_catalog_row(_normalized_candidate(), "dev-test")
    assert row["longitude"] == f"{-111.9012345:.6f}"
    assert row["latitude"] == f"{33.5098765:.6f}"
    assert row["longitude"].count(".") == 1
    assert len(row["longitude"].split(".")[1]) == strip.COORDINATE_DECIMAL_PLACES


def test_catalog_row_routability_fields_are_pending_placeholders():
    row = strip.to_catalog_row(_normalized_candidate(), "dev-test")
    assert row["anchor_id"] is None
    assert row["access_distance_km"] is None
    assert row["status"] == strip.PENDING_STATUS
    # deliberately not one of 03 §3.1's real usable/unusable enum members.
    assert row["status"] not in ("usable", "unusable")
    assert row["unusable_reason"] is None


def test_catalog_row_created_at_is_the_deterministic_sentinel():
    row = strip.to_catalog_row(_normalized_candidate(), "dev-test")
    assert row["created_at"] == 0


def test_catalog_row_address_id_is_not_assigned_here():
    row = strip.to_catalog_row(_normalized_candidate(), "dev-test")
    assert row["address_id"] is None


def test_to_catalog_row_requires_normalization_first():
    c = _normalized_candidate()
    c.formatted_address = None
    with pytest.raises(ValueError, match="normalized"):
        strip.to_catalog_row(c, "dev-test")
