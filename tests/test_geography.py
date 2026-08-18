from __future__ import annotations

import dataclasses
import math
from decimal import ROUND_HALF_UP, Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from _helpers import GEO_ARTIFACT_DIR, GEO_ARTIFACT_ID, geo_run_config, register_conn
from trades_bench import geo_reader, geography
from trades_bench.errors import EngineError, ErrorCode
from trades_bench.run_init import initialize_run, open_run_db

ARTIFACT = geo_reader.load_artifact(GEO_ARTIFACT_DIR)
CONFIG = geo_run_config()
USABLE_ROWS = tuple(row for row in ARTIFACT.catalog if row.status == "usable")

FIRST_ADDRESS = next(row for row in ARTIFACT.catalog if row.address_id == 1)
SECOND_ADDRESS = next(row for row in ARTIFACT.catalog if row.address_id == 2)
SAME_ANCHOR_PEER_ADDRESS = next(row for row in ARTIFACT.catalog if row.address_id == 25)

assert FIRST_ADDRESS.anchor_id != SECOND_ADDRESS.anchor_id
assert SECOND_ADDRESS.anchor_id == SAME_ANCHOR_PEER_ADDRESS.anchor_id


@pytest.fixture
def geo_conn(tmp_path):
    path = tmp_path / "run.db"
    initialize_run(path, master_seed=1, config=geo_run_config(), artifact_dir=GEO_ARTIFACT_DIR)
    conn = open_run_db(path, artifact_dir=GEO_ARTIFACT_DIR)
    register_conn(conn)
    try:
        yield conn
    finally:
        conn.close()


def _no_side_effects(conn):
    before_changes = conn.total_changes
    before_audit = conn.execute("SELECT COUNT(*) c FROM _random_audit").fetchone()["c"]

    def assert_unchanged():
        assert conn.total_changes == before_changes
        assert conn.execute("SELECT COUNT(*) c FROM _random_audit").fetchone()["c"] == before_audit

    return assert_unchanged


def test_resolve_matches_the_exact_catalog_formatted_address(geo_conn):
    address_id = geography.resolve(
        geo_conn, ARTIFACT.abbreviation_table, FIRST_ADDRESS.formatted_address
    )
    assert address_id == FIRST_ADDRESS.address_id


@pytest.mark.parametrize(
    "variant",
    [
        "1 e geneva dr, tempe, az 85282",
        "  1   E   Geneva   Dr ,  Tempe ,  AZ  85282  ",
        "1 East Geneva Drive, Tempe, AZ 85282",
        "1 E Geneva Dr, Tempe, AZ 85282-1234",
    ],
)
def test_resolve_case_whitespace_suffix_and_zip4_variants_resolve_identically(geo_conn, variant):
    assert geography.resolve(geo_conn, ARTIFACT.abbreviation_table, variant) == FIRST_ADDRESS.address_id


def test_resolve_fabricated_address_fails_and_consumes_nothing(geo_conn):
    assert_unchanged = _no_side_effects(geo_conn)
    with pytest.raises(EngineError) as ei:
        geography.resolve(
            geo_conn,
            ARTIFACT.abbreviation_table,
            "99999 Nonexistent Blvd, Tempe, AZ 85282",
        )
    assert ei.value.code is ErrorCode.ADDRESS_UNRESOLVABLE
    assert_unchanged()


def test_resolve_malformed_grammar_still_a_stable_structured_error(geo_conn):
    with pytest.raises(EngineError) as ei:
        geography.resolve(geo_conn, ARTIFACT.abbreviation_table, "not an address")
    assert ei.value.code is ErrorCode.INVALID_ARGUMENT


def test_route_distance_km_same_anchor_floor_binds_on_real_fixture_rows():
    km = geography.route_distance_km(
        SECOND_ADDRESS.access_distance_km,
        SECOND_ADDRESS.anchor_id,
        SAME_ANCHOR_PEER_ADDRESS.access_distance_km,
        SAME_ANCHOR_PEER_ADDRESS.anchor_id,
        ARTIFACT,
        CONFIG,
    )
    assert km == float(CONFIG.get_decimal("geography.min_route_distance_km"))


def test_route_distance_km_same_anchor_detour_factor_binds_when_access_legs_are_large():
    factor = float(CONFIG.get_decimal("geography.intra_cell_detour_factor"))
    km = geography.route_distance_km(1.0, 42, 1.0, 42, ARTIFACT, CONFIG)
    assert km == pytest.approx(2.0 * factor)


def test_route_distance_km_same_anchor_branch_sums_both_access_legs_when_they_differ():
    factor = float(CONFIG.get_decimal("geography.intra_cell_detour_factor"))
    floor = float(CONFIG.get_decimal("geography.min_route_distance_km"))
    access_a_km = 0.4
    access_b_km = 5.6
    expected = max(floor, (access_a_km + access_b_km) * factor)
    km = geography.route_distance_km(access_a_km, 900, access_b_km, 900, ARTIFACT, CONFIG)
    assert km == pytest.approx(expected)


def test_route_distance_km_cross_anchor_sums_both_access_legs_and_the_matrix_entry():
    expected = (
        FIRST_ADDRESS.access_distance_km + SECOND_ADDRESS.access_distance_km
    ) + ARTIFACT.anchor_distance_km(FIRST_ADDRESS.anchor_id, SECOND_ADDRESS.anchor_id)
    km = geography.route_distance_km(
        FIRST_ADDRESS.access_distance_km,
        FIRST_ADDRESS.anchor_id,
        SECOND_ADDRESS.access_distance_km,
        SECOND_ADDRESS.anchor_id,
        ARTIFACT,
        CONFIG,
    )
    assert km == expected


@given(
    st.integers(min_value=0, max_value=len(USABLE_ROWS) - 1),
    st.integers(min_value=0, max_value=len(USABLE_ROWS) - 1),
)
@settings(max_examples=200, deadline=None)
def test_route_distance_km_is_symmetric_finite_and_strictly_positive(i, j):
    a, b = USABLE_ROWS[i], USABLE_ROWS[j]
    km_ab = geography.route_distance_km(
        a.access_distance_km, a.anchor_id, b.access_distance_km, b.anchor_id, ARTIFACT, CONFIG
    )
    km_ba = geography.route_distance_km(
        b.access_distance_km, b.anchor_id, a.access_distance_km, a.anchor_id, ARTIFACT, CONFIG
    )
    assert km_ab == km_ba
    assert math.isfinite(km_ab)
    assert km_ab > 0.0


def test_round_one_decimal_breaks_an_exact_tie_up_not_to_even():
    assert geography._round_one_decimal(Decimal("12.25")) == Decimal("12.3")
    assert geography._round_one_decimal(Decimal("0.05")) == Decimal("0.1")


MAPS_DISTANCE_ALLOWED_FIELDS = frozenset(
    {
        "origin_address",
        "destination_address",
        "distance_km",
        "distance_miles",
        "dataset_version",
    }
)

MAPS_DISTANCE_PROHIBITED_SUBSTRINGS = (
    "time", "eta", "traffic", "weather", "zone", "service", "coordinate",
    "lat", "lon", "geometry", "probability", "duration", "speed", "noise",
)


def assert_maps_distance_schema_is_closed() -> None:
    fields = {f.name for f in dataclasses.fields(geography.MapsDistanceResult)}
    assert fields == MAPS_DISTANCE_ALLOWED_FIELDS
    for name in fields:
        for bad in MAPS_DISTANCE_PROHIBITED_SUBSTRINGS:
            assert bad not in name, name


def test_det07c_maps_distance_schema_excludes_travel_time_serviceability_and_lateness_probability():
    assert_maps_distance_schema_is_closed()


def test_maps_distance_echoes_canonical_formatted_address_and_rounds_to_one_decimal(geo_conn):
    result = geography.maps_distance(
        geo_conn,
        ARTIFACT,
        CONFIG,
        "1 east geneva drive, tempe, az 85282-0000",
        SECOND_ADDRESS.formatted_address,
    )
    assert result.origin_address == FIRST_ADDRESS.formatted_address
    assert result.destination_address == SECOND_ADDRESS.formatted_address
    assert result.dataset_version == GEO_ARTIFACT_ID
    assert result.distance_km == round(result.distance_km, 1)
    assert result.distance_miles == round(result.distance_miles, 1)


def test_maps_distance_is_symmetric(geo_conn):
    forward = geography.maps_distance(
        geo_conn, ARTIFACT, CONFIG, FIRST_ADDRESS.formatted_address, SECOND_ADDRESS.formatted_address
    )
    backward = geography.maps_distance(
        geo_conn, ARTIFACT, CONFIG, SECOND_ADDRESS.formatted_address, FIRST_ADDRESS.formatted_address
    )
    assert forward.distance_km == backward.distance_km
    assert forward.distance_miles == backward.distance_miles
    assert forward.origin_address == backward.destination_address
    assert forward.destination_address == backward.origin_address


def test_maps_distance_agrees_exactly_with_route_distance_km_on_the_unrounded_value(geo_conn):
    origin = geography.catalog_point(geo_conn, FIRST_ADDRESS.address_id)
    destination = geography.catalog_point(geo_conn, SECOND_ADDRESS.address_id)
    unrounded = geography.route_distance_km(
        origin.access_distance_km,
        origin.anchor_id,
        destination.access_distance_km,
        destination.anchor_id,
        ARTIFACT,
        CONFIG,
    )
    result = geography.maps_distance(
        geo_conn, ARTIFACT, CONFIG, FIRST_ADDRESS.formatted_address, SECOND_ADDRESS.formatted_address
    )
    expected = Decimal(str(unrounded)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    assert result.distance_km == expected


def test_maps_distance_unresolvable_address_fails_and_consumes_nothing(geo_conn):
    assert_unchanged = _no_side_effects(geo_conn)
    with pytest.raises(EngineError) as ei:
        geography.maps_distance(
            geo_conn,
            ARTIFACT,
            CONFIG,
            "99999 Nonexistent Blvd, Tempe, AZ 85282",
            SECOND_ADDRESS.formatted_address,
        )
    assert ei.value.code is ErrorCode.ADDRESS_UNRESOLVABLE
    assert_unchanged()


def test_maps_distance_is_deterministic_and_consumes_no_draw_or_state(geo_conn):
    assert_unchanged = _no_side_effects(geo_conn)
    first = geography.maps_distance(
        geo_conn, ARTIFACT, CONFIG, FIRST_ADDRESS.formatted_address, SECOND_ADDRESS.formatted_address
    )
    second = geography.maps_distance(
        geo_conn, ARTIFACT, CONFIG, FIRST_ADDRESS.formatted_address, SECOND_ADDRESS.formatted_address
    )
    assert first == second
    assert_unchanged()
