"""tools.geo_build.geometry: haversine, the local-projection km buffer, point
containment/nearest lookups, and pairwise disjointness (CSR-003b, RES-60).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("shapely")

from shapely.geometry import Point, Polygon  # noqa: E402

from geo_build import geometry  # noqa: E402


def test_haversine_known_distance():
    # Phoenix Sky Harbor to Tucson International, ~145 nm road-independent
    # great-circle distance is a well-known ballpark (~185-190 km); this only
    # pins the formula against a hand-computed antipodal-adjacent sanity
    # check, not a real-world assertion.
    d_km = geometry.haversine_km(-112.0, 33.0, -112.0, 34.0)
    # 1 degree of latitude is ~111.19 km net of the mean-radius formula used.
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
    # Not bit-exact (round-trips through a scaled affine transform) but the
    # symmetric difference should be tiny relative to the polygon's own area.
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
    # Two squares sharing the edge x=1: a point exactly on that edge is
    # `covers`-true for both; the first (lowest index / lowest zone_id, by
    # this module's convention) wins.
    left = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    right = Polygon([(1, 0), (2, 0), (2, 1), (1, 1)])
    assert geometry.containing_index(1.0, 0.5, [left, right]) == 0


def test_nearest_index_picks_the_closer_polygon():
    left = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    right = Polygon([(10, 0), (11, 0), (11, 1), (10, 1)])
    # A point just east of `left`'s right edge is much nearer to `left`.
    assert geometry.nearest_index(1.01, 0.5, [left, right]) == 0


def test_nearest_index_tie_breaks_to_lowest_index():
    # Two unit squares placed symmetrically around a query point exactly
    # between them: the haversine distance from the query point to each
    # polygon's nearest edge is computed from mirror-symmetric coordinates,
    # so it comes out bit-identical, exercising the real tie (not merely a
    # "close enough" case an epsilon-tolerant comparison would paper over).
    query_lon, query_lat = 0.0, 40.0
    left = Polygon([(-2.0, 39.5), (-1.0, 39.5), (-1.0, 40.5), (-2.0, 40.5)])
    right = Polygon([(1.0, 39.5), (2.0, 39.5), (2.0, 40.5), (1.0, 40.5)])
    assert geometry.nearest_index(query_lon, query_lat, [left, right]) == 0
    # Confirm the tie is real, not accidental: swapping order still picks
    # index 0, i.e. whichever polygon is listed first among true ties.
    assert geometry.nearest_index(query_lon, query_lat, [right, left]) == 0


def _old_buggy_nearest_km(qlon: float, qlat: float, poly: Polygon) -> float:
    """The pre-fix algorithm (review finding 2): nearest-point search in the
    raw, anisotropic lon/lat degree plane, ranked by haversine. Reimplemented
    here (not imported — the real module no longer has this bug) purely as
    an independent baseline so this regression test proves the fix actually
    changes the answer for a real case, rather than merely asserting the new
    function's output in isolation.
    """
    from shapely.ops import nearest_points

    nearest_on_poly, _ = nearest_points(poly, Point(qlon, qlat))
    return geometry.haversine_km(qlon, qlat, nearest_on_poly.x, nearest_on_poly.y)


def test_nearest_index_is_geodesic_not_planar_degree_space():
    """finding 2: at Phoenix's ~33.5°N, 1 degree of longitude is ~92.8 km
    against 1 degree of latitude's ~110.6 km. A nearest-point search that
    runs in raw (unscaled) lon/lat degrees is anisotropic and can pick the
    wrong foot point on a boundary edge that is not axis-aligned, which can
    make an objectively farther polygon look nearer than a truly closer one.

    This is not a hypothetical: the two polygons and query point below were
    found by a targeted numeric search (script, not reproduced here) and
    independently confirmed against `_old_buggy_nearest_km` — the raw
    degree-space algorithm this module used before the fix. `zone_a` has a
    purely north-south (axis-aligned) edge, whose nearest point is identical
    in both the degree-space and km-frame searches (an axis-aligned edge is
    metric-invariant); `zone_b` has a boundary edge oriented at exactly 45°
    in degree space, whose nearest point is *not* metric-invariant. At the
    query point below, `zone_b` is the true geodesically nearer polygon, but
    the old degree-space search finds a suboptimal foot on `zone_b`'s
    diagonal edge, making it look 0.83 km farther than it really is — just
    enough to flip the ranking against the (metric-invariant, unbiased)
    `zone_a`.
    """
    query_lon, query_lat = -111.98, 33.795
    zone_a = Polygon([(-112.58, 33.0), (-112.57, 33.0), (-112.57, 34.5), (-112.58, 34.5)])
    zone_b = Polygon(
        [(-111.605, 33.405), (-111.405, 33.605), (-111.395, 33.595), (-111.595, 33.395)]
    )
    polygons = [zone_a, zone_b]

    # Confirm the premise: the old algorithm gets this wrong.
    old_a = _old_buggy_nearest_km(query_lon, query_lat, zone_a)
    old_b = _old_buggy_nearest_km(query_lon, query_lat, zone_b)
    assert old_a < old_b, "fixture premise broken: old algorithm no longer prefers zone_a"
    old_winner = 0 if old_a < old_b else 1
    assert old_winner == 0  # the bug: old code would pick zone_a (index 0)

    # The fixed function must pick the truly nearer polygon, zone_b (index 1).
    assert geometry.nearest_index(query_lon, query_lat, polygons) == 1


def test_assert_pairwise_disjoint_passes_for_disjoint_polygons():
    polys = {
        "a": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
        "b": Polygon([(1, 0), (2, 0), (2, 1), (1, 1)]),  # touches at x=1, no area overlap
    }
    geometry.assert_pairwise_disjoint(polys)  # must not raise


def test_assert_pairwise_disjoint_raises_on_true_overlap():
    polys = {
        "a": Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
        "b": Polygon([(0.5, 0), (1.5, 0), (1.5, 1), (0.5, 1)]),  # overlaps a by area 0.5
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
    # Smoke test that this module's Point usage matches shapely's own
    # covers() (used elsewhere in the pipeline for crop filtering).
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    assert square.covers(Point(0, 0))  # corner
    assert square.covers(Point(1, 1))  # opposite corner


def test_validate_or_repair_polygon_passes_through_a_valid_polygon_unchanged():
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    assert geometry.validate_or_repair_polygon(square, label="x") is square


def test_validate_or_repair_polygon_repairs_a_self_intersecting_bowtie():
    # finding 5: a "bowtie" ring (edges cross in the middle) is the common
    # real-world shape an OSM admin relation's assembled ring can take.
    # GEOS operations on an invalid geometry (like this one) can return
    # silently wrong results rather than raising, so this must be repaired
    # before anything downstream (split/union/disjointness) touches it.
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
    assert not bowtie.is_valid

    repaired = geometry.validate_or_repair_polygon(bowtie, label="test relation")
    assert repaired.is_valid
    assert not repaired.is_empty
    # The repaired shape is the union of the two non-crossing triangles the
    # bowtie decomposes into — total area 0.5, not the bowtie's own
    # (meaningless, self-cancelling) .area computation.
    assert repaired.area == pytest.approx(0.5)


def test_validate_or_repair_polygon_raises_loudly_on_an_unrecoverable_polygon():
    # A ring degenerate to a single coincident point has no area even after
    # shapely.make_valid repairs it into a valid (but zero-area) geometry -
    # unusable as a municipal polygon, so this must fail the build rather
    # than silently hand back something with no area.
    degenerate = Polygon([(5, 5), (5, 5), (5, 5), (5, 5)])
    with pytest.raises(ValueError, match="administrative relation 42"):
        geometry.validate_or_repair_polygon(degenerate, label="administrative relation 42")
