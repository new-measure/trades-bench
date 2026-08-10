"""tools.geo_build.osm_reader: relation-centroid caching and correctness
(CSR-003b review findings 3-4, RES-60).
"""

from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build.osm_reader import OsmElements, OsmRelation, OsmWay  # noqa: E402


def test_ways_by_id_is_cached_across_calls():
    # functools.cached_property must compute the index once, not once per
    # relation_centroid() call (finding 3: an O(ways) rebuild on every call
    # is O(ways x relations) over a whole build).
    way = OsmWay(osm_id=1, coords=((0.0, 0.0), (1.0, 1.0)), tags={})
    elements = OsmElements(nodes=(), ways=(way,), relations=())
    first = elements._ways_by_id
    second = elements._ways_by_id
    assert first is second  # same dict object: computed once, cached


def test_relation_centroid_caching_does_not_change_the_answer_across_calls():
    way = OsmWay(osm_id=1, coords=((0.0, 0.0), (1.0, 1.0)), tags={})
    relation = OsmRelation(osm_id=100, tags={}, member_way_ids=(1,))
    elements = OsmElements(nodes=(), ways=(way,), relations=(relation,))
    first = elements.relation_centroid(relation)
    second = elements.relation_centroid(relation)
    assert first == second == (0.5, 0.5)


def test_relation_centroid_open_way_falls_back_to_line_centroid():
    # A single open (non-closed) way can't be polygonized; falls back to the
    # line's own centroid (its midpoint, for a straight two-node way).
    way = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0)), tags={})
    relation = OsmRelation(osm_id=100, tags={}, member_way_ids=(1,))
    elements = OsmElements(nodes=(), ways=(way,), relations=(relation,))
    assert elements.relation_centroid(relation) == (1.0, 0.0)


def test_relation_centroid_returns_none_when_no_member_way_resolved():
    relation = OsmRelation(osm_id=100, tags={}, member_way_ids=(999,))
    elements = OsmElements(nodes=(), ways=(), relations=(relation,))
    assert elements.relation_centroid(relation) is None


def test_relation_centroid_uses_polygon_centroid_not_node_density_average():
    """finding 4: a node-density-weighted arithmetic mean over member-way
    coordinates can sit far from the true geometric centroid whenever nodes
    are unevenly distributed around the ring — a dense cluster of survey
    points along one edge (common where OSM tracing is more detailed) should
    not drag the reported centroid toward it. Constructs a long thin
    rectangle (true area centroid at its exact geometric center) whose top
    edge is densely resampled near one end and whose other three edges carry
    only their corner nodes, so a naive average is pulled hard toward the
    dense cluster while the true centroid does not move at all.
    """
    # Rectangle (0,0)-(10,0)-(10,1)-(0,1), closed. The top edge (y=1) is
    # traced with 21 extra points clustered in x in [0,2] before closing
    # back to (0,1) and (0,0).
    bottom_right = [(0.0, 0.0), (10.0, 0.0), (10.0, 1.0)]
    top_dense = [(2.0 - i * 0.1, 1.0) for i in range(21)]  # 2.0 down to 0.0
    close = [(0.0, 1.0), (0.0, 0.0)]
    ring = bottom_right + top_dense + close

    way = OsmWay(osm_id=1, coords=tuple(ring), tags={})
    relation = OsmRelation(osm_id=100, tags={}, member_way_ids=(1,))
    elements = OsmElements(nodes=(), ways=(way,), relations=(relation,))

    centroid = elements.relation_centroid(relation)
    assert centroid is not None
    true_centroid = (5.0, 0.5)  # the rectangle's exact geometric center
    naive_average = (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))

    assert centroid[0] == pytest.approx(true_centroid[0], abs=1e-9)
    assert centroid[1] == pytest.approx(true_centroid[1], abs=1e-9)
    # The naive node average is nowhere near either coordinate: proves the
    # fix is not accidentally equivalent to the old averaging behavior.
    assert abs(centroid[0] - naive_average[0]) > 1.0
    assert abs(centroid[1] - naive_average[1]) > 0.1


def test_relation_centroid_polygonizes_multiple_ways_into_one_ring():
    # Two ways together forming one closed ring (a square split into two
    # halves at x=1) - polygonize() must assemble them before centroiding.
    way_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0), (2.0, 2.0)), tags={})
    way_b = OsmWay(osm_id=2, coords=((2.0, 2.0), (0.0, 2.0), (0.0, 0.0)), tags={})
    relation = OsmRelation(osm_id=100, tags={}, member_way_ids=(1, 2))
    elements = OsmElements(nodes=(), ways=(way_a, way_b), relations=(relation,))
    centroid = elements.relation_centroid(relation)
    assert centroid == pytest.approx((1.0, 1.0))
