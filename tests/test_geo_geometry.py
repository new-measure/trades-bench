from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("shapely")

from shapely.geometry import Point, Polygon  # noqa: E402

from geo_build import geometry  # noqa: E402


def test_haversine_known_distance():
    d_km = geometry.haversine_km(-112.0, 33.0, -112.0, 34.0)
    assert 110.5 <= d_km <= 111.5


def test_haversine_zero_for_identical_point():
    assert geometry.haversine_km(-112.1, 33.5, -112.1, 33.5) == 0.0


def test_haversine_symmetric():
    a = geometry.haversine_km(-112.0, 33.0, -111.5, 33.6)
    b = geometry.haversine_km(-111.5, 33.6, -112.0, 33.0)
    assert a == b


def test_buffer_km_expands_area_and_still_contains_original():
    square = Polygon([(-112.0, 33.0), (-111.9, 33.0), (-111.9, 33.1), (-112.0, 33.1)])
    buffered = geometry.buffer_km(square, Decimal("5.0"))
    assert buffered.area > square.area
    assert buffered.covers(square)


def test_buffer_km_zero_is_approximately_the_original():
    square = Polygon([(-112.0, 33.0), (-111.9, 33.0), (-111.9, 33.1), (-112.0, 33.1)])
    buffered = geometry.buffer_km(square, Decimal("0"))
    assert buffered.symmetric_difference(square).area < square.area * 1e-6


def test_containing_index_finds_the_right_polygon():
    left = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    right = Polygon([(2, 0), (3, 0), (3, 1), (2, 1)])
    assert geometry.containing_index(0.5, 0.5, [left, right]) == 0
    assert geometry.containing_index(2.5, 0.5, [left, right]) == 1


def test_containing_index_none_when_outside_every_polygon():
    left = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    right = Polygon([(2, 0), (3, 0), (3, 1), (2, 1)])
    assert geometry.containing_index(1.5, 0.5, [left, right]) is None


def test_containing_index_boundary_resolves_to_lower_index():
    left = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    right = Polygon([(1, 0), (2, 0), (2, 1), (1, 1)])
    assert geometry.containing_index(1.0, 0.5, [left, right]) == 0


def test_nearest_index_picks_the_closer_polygon():
    left = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    right = Polygon([(10, 0), (11, 0), (11, 1), (10, 1)])
    assert geometry.nearest_index(1.01, 0.5, [left, right]) == 0


def test_nearest_index_tie_breaks_to_lowest_index():
    query_lon, query_lat = 0.0, 40.0
    left = Polygon([(-2.0, 39.5), (-1.0, 39.5), (-1.0, 40.5), (-2.0, 40.5)])
    right = Polygon([(1.0, 39.5), (2.0, 39.5), (2.0, 40.5), (1.0, 40.5)])
    assert geometry.nearest_index(query_lon, query_lat, [left, right]) == 0
    assert geometry.nearest_index(query_lon, query_lat, [right, left]) == 0


def _old_buggy_nearest_km(qlon: float, qlat: float, poly: Polygon) -> float:
    from shapely.ops import nearest_points

    nearest_on_poly, _ = nearest_points(poly, Point(qlon, qlat))
    return geometry.haversine_km(qlon, qlat, nearest_on_poly.x, nearest_on_poly.y)


def test_nearest_index_is_geodesic_not_planar_degree_space():
    query_lon, query_lat = -111.98, 33.795
    zone_a = Polygon([(-112.58, 33.0), (-112.57, 33.0), (-112.57, 34.5), (-112.58, 34.5)])
    zone_b = Polygon(
        [(-111.605, 33.405), (-111.405, 33.605), (-111.395, 33.595), (-111.595, 33.395)]
    )
    polygons = [zone_a, zone_b]

    old_a = _old_buggy_nearest_km(query_lon, query_lat, zone_a)
    old_b = _old_buggy_nearest_km(query_lon, query_lat, zone_b)
    assert old_a < old_b, "fixture premise broken: old algorithm no longer prefers zone_a"
    old_winner = 0 if old_a < old_b else 1
    assert old_winner == 0

    assert geometry.nearest_index(query_lon, query_lat, polygons) == 1


def test_assert_pairwise_disjoint_passes_for_disjoint_polygons():
    polys = {
        "a": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
        "b": Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),
    }
    geometry.assert_pairwise_disjoint(polys)


def test_assert_pairwise_disjoint_raises_on_true_overlap():
    polys = {
        "a": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
        "b": Polygon([(0.5, 0), (1.5, 0), (1.5, 1), (0.5, 1)]),
    }
    with pytest.raises(ValueError, match="not pairwise disjoint"):
        geometry.assert_pairwise_disjoint(polys)


def test_assert_pairwise_disjoint_error_names_the_overlapping_pair():
    polys = {
        "north_phoenix": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
        "scottsdale": Polygon([(0.5, 0), (1.5, 0), (1.5, 1), (0.5, 1)]),
    }
    with pytest.raises(ValueError, match="north_phoenix.*scottsdale|scottsdale.*north_phoenix"):
        geometry.assert_pairwise_disjoint(polys)


def test_point_helpers_use_covers_semantics_directly():
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    assert square.covers(Point(0, 0))
    assert square.covers(Point(1, 1))


def test_validate_or_repair_polygon_passes_through_a_valid_polygon_unchanged():
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    assert geometry.validate_or_repair_polygon(square, label="x") is square


def test_validate_or_repair_polygon_repairs_a_self_intersecting_bowtie():
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
    assert not bowtie.is_valid

    repaired = geometry.validate_or_repair_polygon(bowtie, label="test relation")
    assert repaired.is_valid
    assert not repaired.is_empty
    assert repaired.area == pytest.approx(0.5)


def test_validate_or_repair_polygon_raises_loudly_on_an_unrecoverable_polygon():
    degenerate = Polygon([(5, 5), (5, 5), (5, 5), (5, 5)])
    with pytest.raises(ValueError, match="administrative relation 42"):
        geometry.validate_or_repair_polygon(degenerate, label="administrative relation 42")


def test_validate_or_repair_polygon_drops_stray_non_polygonal_repair_artifacts():
    spiked = Polygon([(0, 0), (4, 0), (4, 4), (2, 4), (2, 6), (2, 4), (0, 4), (0, 0)])
    assert not spiked.is_valid
    from shapely.validation import make_valid

    assert make_valid(spiked).geom_type == "GeometryCollection"

    repaired = geometry.validate_or_repair_polygon(spiked, label="spiked relation")
    assert repaired.geom_type in ("Polygon", "MultiPolygon")
    assert repaired.is_valid
    assert repaired.area == pytest.approx(16.0)
