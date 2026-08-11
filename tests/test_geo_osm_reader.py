from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build.osm_reader import OsmElements, OsmRelation, OsmWay  # noqa: E402


def test_ways_by_id_is_cached_across_calls():
    way = OsmWay(osm_id=1, coords=((0.0, 0.0), (1.0, 1.0)), tags={})
    elements = OsmElements(nodes=(), ways=(way,), relations=())
    first = elements._ways_by_id
    second = elements._ways_by_id
    assert first is second


def test_relation_centroid_caching_does_not_change_the_answer_across_calls():
    way = OsmWay(osm_id=1, coords=((0.0, 0.0), (1.0, 1.0)), tags={})
    relation = OsmRelation(osm_id=100, tags={}, member_way_ids=(1,))
    elements = OsmElements(nodes=(), ways=(way,), relations=(relation,))
    first = elements.relation_centroid(relation)
    second = elements.relation_centroid(relation)
    assert first == second == (0.5, 0.5)


def test_relation_centroid_open_way_falls_back_to_line_centroid():
    way = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0)), tags={})
    relation = OsmRelation(osm_id=100, tags={}, member_way_ids=(1,))
    elements = OsmElements(nodes=(), ways=(way,), relations=(relation,))
    assert elements.relation_centroid(relation) == (1.0, 0.0)


def test_relation_centroid_returns_none_when_no_member_way_resolved():
    relation = OsmRelation(osm_id=100, tags={}, member_way_ids=(999,))
    elements = OsmElements(nodes=(), ways=(), relations=(relation,))
    assert elements.relation_centroid(relation) is None


def test_relation_centroid_uses_polygon_centroid_not_node_density_average():
    bottom_right = [(0.0, 0.0), (10.0, 0.0), (10.0, 1.0)]
    top_dense = [(2.0 - i * 0.1, 1.0) for i in range(21)]
    close = [(0.0, 1.0), (0.0, 0.0)]
    ring = bottom_right + top_dense + close

    way = OsmWay(osm_id=1, coords=tuple(ring), tags={})
    relation = OsmRelation(osm_id=100, tags={}, member_way_ids=(1,))
    elements = OsmElements(nodes=(), ways=(way,), relations=(relation,))

    centroid = elements.relation_centroid(relation)
    assert centroid is not None
    true_centroid = (5.0, 0.5)
    naive_average = (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))

    assert centroid[0] == pytest.approx(true_centroid[0], abs=1e-9)
    assert centroid[1] == pytest.approx(true_centroid[1], abs=1e-9)
    assert abs(centroid[0] - naive_average[0]) > 1.0
    assert abs(centroid[1] - naive_average[1]) > 0.1


def test_relation_centroid_polygonizes_multiple_ways_into_one_ring():
    way_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0), (2.0, 2.0)), tags={})
    way_b = OsmWay(osm_id=2, coords=((2.0, 2.0), (0.0, 2.0), (0.0, 0.0)), tags={})
    relation = OsmRelation(osm_id=100, tags={}, member_way_ids=(1, 2))
    elements = OsmElements(nodes=(), ways=(way_a, way_b), relations=(relation,))
    centroid = elements.relation_centroid(relation)
    assert centroid == pytest.approx((1.0, 1.0))
