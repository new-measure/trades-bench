from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("shapely")

from geo_build import dedupe  # noqa: E402
from geo_build.models import Candidate  # noqa: E402


def _candidate(source_type, source_id, lon, lat, street, canonical_key):
    c = Candidate(
        source_type=source_type,
        source_id=source_id,
        longitude=lon,
        latitude=lat,
        house_number_raw="1",
        street_raw=street,
        postal_code_raw="85251",
        city="Scottsdale",
        city_source="tag",
        state="AZ",
        state_source="crop",
    )
    c.street = street
    c.canonical_key = canonical_key
    return c


def test_duplicate_key_keeps_first_in_deterministic_order():
    a = _candidate("way", 5, -111.9, 33.5, "Main St", "SAME KEY")
    b = _candidate("node", 1, -111.8, 33.4, "Main St", "SAME KEY")
    accepted, rejected = dedupe.dedupe([a, b], Decimal("10.0"))
    assert [c.source_id for c in accepted] == [1]
    assert len(rejected) == 1
    assert rejected[0].source_type == "way"
    assert rejected[0].source_id == 5
    assert rejected[0].reason == dedupe.REASON_DUPLICATE_KEY


def test_duplicate_key_first_wins_is_independent_of_input_order():
    a = _candidate("node", 1, -111.9, 33.5, "Main St", "SAME KEY")
    b = _candidate("node", 2, -111.8, 33.4, "Main St", "SAME KEY")
    accepted_fwd, _ = dedupe.dedupe([a, b], Decimal("10.0"))
    accepted_rev, _ = dedupe.dedupe([b, a], Decimal("10.0"))
    assert [c.source_id for c in accepted_fwd] == [1]
    assert [c.source_id for c in accepted_rev] == [1]


def test_duplicate_coordinate_rejects_close_candidate_on_same_street():
    a = _candidate("node", 1, -111.900000, 33.500000, "Main St", "KEY A")
    b = _candidate("node", 2, -111.900000, 33.500049, "Main St", "KEY B")
    accepted, rejected = dedupe.dedupe([a, b], Decimal("10.0"))
    assert [c.source_id for c in accepted] == [1]
    assert rejected[0].reason == dedupe.REASON_DUPLICATE_COORDINATE
    assert rejected[0].source_id == 2


def test_duplicate_coordinate_requires_the_same_street():
    a = _candidate("node", 1, -111.900000, 33.500000, "Main St", "KEY A")
    b = _candidate("node", 2, -111.900000, 33.500049, "2nd Ave", "KEY B")
    accepted, rejected = dedupe.dedupe([a, b], Decimal("10.0"))
    assert {c.source_id for c in accepted} == {1, 2}
    assert rejected == []


def test_duplicate_coordinate_respects_the_configured_radius():
    a = _candidate("node", 1, -111.900000, 33.500000, "Main St", "KEY A")
    b = _candidate("node", 2, -111.900000, 33.500495, "Main St", "KEY B")
    accepted_tight, rejected_tight = dedupe.dedupe([a, b], Decimal("10.0"))
    assert {c.source_id for c in accepted_tight} == {1, 2}
    accepted_loose, rejected_loose = dedupe.dedupe([a, b], Decimal("100.0"))
    assert [c.source_id for c in accepted_loose] == [1]
    assert rejected_loose[0].reason == dedupe.REASON_DUPLICATE_COORDINATE


def test_dedupe_sort_key_orders_node_before_way_before_relation():
    a = _candidate("relation", 1, 0, 0, "s", "k1")
    b = _candidate("way", 1, 0, 0, "s", "k2")
    c = _candidate("node", 1, 0, 0, "s", "k3")
    ordered = sorted([a, b, c], key=dedupe.dedupe_sort_key)
    assert [x.source_type for x in ordered] == ["node", "way", "relation"]
