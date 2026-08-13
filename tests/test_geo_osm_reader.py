from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build.osm_reader import (  # noqa: E402
    OsmElements,
    OsmRelation,
    OsmRelationMember,
    OsmWay,
)


def test_ways_by_id_is_cached_across_calls():
    way = OsmWay(osm_id=1, coords=((0.0, 0.0), (1.0, 1.0)), tags={})
    elements = OsmElements(nodes=(), ways=(way,), relations=())
    first = elements._ways_by_id
    second = elements._ways_by_id
    assert first is second


def test_relation_centroid_caching_does_not_change_the_answer_across_calls():
    way = OsmWay(osm_id=1, coords=((0.0, 0.0), (1.0, 1.0)), tags={})
    relation = OsmRelation(osm_id=100, tags={}, members=(OsmRelationMember(1),))
    elements = OsmElements(nodes=(), ways=(way,), relations=(relation,))
    first = elements.relation_centroid(relation)
    second = elements.relation_centroid(relation)
    assert first == second == (0.5, 0.5)


def test_relation_centroid_open_way_falls_back_to_line_centroid():
    way = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0)), tags={})
    relation = OsmRelation(osm_id=100, tags={}, members=(OsmRelationMember(1),))
    elements = OsmElements(nodes=(), ways=(way,), relations=(relation,))
    assert elements.relation_centroid(relation) == (1.0, 0.0)


def test_relation_centroid_returns_none_when_no_member_way_resolved():
    relation = OsmRelation(osm_id=100, tags={}, members=(OsmRelationMember(999),))
    elements = OsmElements(nodes=(), ways=(), relations=(relation,))
    assert elements.relation_centroid(relation) is None


def test_relation_centroid_uses_polygon_centroid_not_node_density_average():
    bottom_right = [(0.0, 0.0), (10.0, 0.0), (10.0, 1.0)]
    top_dense = [(2.0 - i * 0.1, 1.0) for i in range(21)]
    close = [(0.0, 1.0), (0.0, 0.0)]
    ring = bottom_right + top_dense + close

    way = OsmWay(osm_id=1, coords=tuple(ring), tags={})
    relation = OsmRelation(osm_id=100, tags={}, members=(OsmRelationMember(1),))
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
    relation = OsmRelation(
        osm_id=100, tags={}, members=(OsmRelationMember(1), OsmRelationMember(2))
    )
    elements = OsmElements(nodes=(), ways=(way_a, way_b), relations=(relation,))
    centroid = elements.relation_centroid(relation)
    assert centroid == pytest.approx((1.0, 1.0))


def test_relation_centroid_repairs_a_self_intersecting_bowtie_member_polygon(monkeypatch):
    import shapely.ops
    from shapely.errors import GEOSException
    from shapely.geometry import Polygon

    bowtie = Polygon([(0.0, 0.0), (4.0, 4.0), (4.0, 0.0), (0.0, 4.0), (0.0, 0.0)])
    other = Polygon([(1.0, 1.0), (3.0, 1.0), (3.0, 3.0), (1.0, 3.0), (1.0, 1.0)])
    assert not bowtie.is_valid
    real_unary_union = shapely.ops.unary_union
    with pytest.raises(GEOSException):
        real_unary_union([bowtie, other])

    monkeypatch.setattr(shapely.ops, "polygonize", lambda lines: [bowtie, other])

    way_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 0.0)), tags={})
    way_b = OsmWay(osm_id=2, coords=((0.0, 4.0), (4.0, 4.0), (0.0, 0.0)), tags={})
    relation = OsmRelation(
        osm_id=100, tags={}, members=(OsmRelationMember(1), OsmRelationMember(2))
    )
    elements = OsmElements(nodes=(), ways=(way_a, way_b), relations=(relation,))

    centroid = elements.relation_centroid(relation)
    assert centroid == pytest.approx((2.0, 2.0))


def test_relation_centroid_returns_none_when_union_still_raises_after_repair(monkeypatch):
    import shapely.ops
    from shapely.errors import GEOSException

    def _raise(*args, **kwargs):
        raise GEOSException("TopologyException: side location conflict at 2 2")

    monkeypatch.setattr(shapely.ops, "unary_union", _raise)

    way_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0), (2.0, 2.0)), tags={})
    way_b = OsmWay(osm_id=2, coords=((2.0, 2.0), (0.0, 2.0), (0.0, 0.0)), tags={})
    relation = OsmRelation(
        osm_id=100, tags={}, members=(OsmRelationMember(1), OsmRelationMember(2))
    )
    elements = OsmElements(nodes=(), ways=(way_a, way_b), relations=(relation,))

    assert elements.relation_centroid(relation) is None


def test_relation_polygon_assembles_a_multipolygon_relation_that_covers_its_interior():
    way_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0), (2.0, 2.0)), tags={})
    way_b = OsmWay(osm_id=2, coords=((2.0, 2.0), (0.0, 2.0), (0.0, 0.0)), tags={})
    relation = OsmRelation(
        osm_id=100, tags={}, members=(OsmRelationMember(1), OsmRelationMember(2))
    )
    elements = OsmElements(nodes=(), ways=(way_a, way_b), relations=(relation,))

    from shapely.geometry import Point

    polygon = elements.relation_polygon(relation)
    assert polygon is not None
    assert polygon.covers(Point(1.0, 1.0))
    assert not polygon.covers(Point(5.0, 5.0))


def test_relation_polygon_returns_none_when_no_member_way_resolved():
    relation = OsmRelation(osm_id=100, tags={}, members=(OsmRelationMember(999),))
    elements = OsmElements(nodes=(), ways=(), relations=(relation,))
    assert elements.relation_polygon(relation) is None


def test_relation_polygon_returns_none_for_an_open_way_that_never_rings_up():
    way = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0)), tags={})
    relation = OsmRelation(osm_id=100, tags={}, members=(OsmRelationMember(1),))
    elements = OsmElements(nodes=(), ways=(way,), relations=(relation,))
    assert elements.relation_polygon(relation) is None


def test_relation_polygon_repairs_a_self_intersecting_bowtie_member_polygon(monkeypatch):
    import shapely.ops
    from shapely.geometry import Point, Polygon

    bowtie = Polygon([(0.0, 0.0), (4.0, 4.0), (4.0, 0.0), (0.0, 4.0), (0.0, 0.0)])
    other = Polygon([(1.0, 1.0), (3.0, 1.0), (3.0, 3.0), (1.0, 3.0), (1.0, 1.0)])
    assert not bowtie.is_valid

    monkeypatch.setattr(shapely.ops, "polygonize", lambda lines: [bowtie, other])

    way_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 0.0)), tags={})
    way_b = OsmWay(osm_id=2, coords=((0.0, 4.0), (4.0, 4.0), (0.0, 0.0)), tags={})
    relation = OsmRelation(
        osm_id=100, tags={}, members=(OsmRelationMember(1), OsmRelationMember(2))
    )
    elements = OsmElements(nodes=(), ways=(way_a, way_b), relations=(relation,))

    polygon = elements.relation_polygon(relation)
    assert polygon is not None
    assert polygon.is_valid
    assert polygon.covers(Point(2.0, 2.0))


def test_relation_polygon_returns_none_when_union_still_raises_after_repair(monkeypatch):
    import shapely.ops
    from shapely.errors import GEOSException

    def _raise(*args, **kwargs):
        raise GEOSException("TopologyException: side location conflict at 2 2")

    monkeypatch.setattr(shapely.ops, "unary_union", _raise)

    way_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0), (2.0, 2.0)), tags={})
    way_b = OsmWay(osm_id=2, coords=((2.0, 2.0), (0.0, 2.0), (0.0, 0.0)), tags={})
    relation = OsmRelation(
        osm_id=100, tags={}, members=(OsmRelationMember(1), OsmRelationMember(2))
    )
    elements = OsmElements(nodes=(), ways=(way_a, way_b), relations=(relation,))

    assert elements.relation_polygon(relation) is None


def test_relation_polygon_courtyard_multipolygon_has_a_hole_for_inner_members():
    outer_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)), tags={})
    outer_b = OsmWay(osm_id=2, coords=((10.0, 10.0), (0.0, 10.0), (0.0, 0.0)), tags={})
    inner_a = OsmWay(osm_id=3, coords=((4.0, 4.0), (6.0, 4.0), (6.0, 6.0)), tags={})
    inner_b = OsmWay(osm_id=4, coords=((6.0, 6.0), (4.0, 6.0), (4.0, 4.0)), tags={})
    relation = OsmRelation(
        osm_id=100,
        tags={},
        members=(
            OsmRelationMember(1, role="outer"),
            OsmRelationMember(2, role="outer"),
            OsmRelationMember(3, role="inner"),
            OsmRelationMember(4, role="inner"),
        ),
    )
    elements = OsmElements(
        nodes=(), ways=(outer_a, outer_b, inner_a, inner_b), relations=(relation,)
    )

    from shapely.geometry import Point

    polygon = elements.relation_polygon(relation)
    assert polygon is not None
    assert len(polygon.interiors) == 1
    assert polygon.covers(Point(1.0, 1.0))
    assert not polygon.covers(Point(5.0, 5.0))


def test_relation_polygon_treats_an_empty_role_member_as_outer():
    way_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0), (2.0, 2.0)), tags={})
    way_b = OsmWay(osm_id=2, coords=((2.0, 2.0), (0.0, 2.0), (0.0, 0.0)), tags={})
    relation = OsmRelation(
        osm_id=100,
        tags={},
        members=(OsmRelationMember(1, role="outer"), OsmRelationMember(2, role="")),
    )
    elements = OsmElements(nodes=(), ways=(way_a, way_b), relations=(relation,))

    from shapely.geometry import Point

    polygon = elements.relation_polygon(relation)
    assert polygon is not None
    assert polygon.covers(Point(1.0, 1.0))
    assert not polygon.covers(Point(5.0, 5.0))


def test_relation_polygon_ignores_members_with_unknown_roles():
    outer_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)), tags={})
    outer_b = OsmWay(osm_id=2, coords=((10.0, 10.0), (0.0, 10.0), (0.0, 0.0)), tags={})
    stray_a = OsmWay(osm_id=3, coords=((4.0, 4.0), (6.0, 4.0), (6.0, 6.0)), tags={})
    stray_b = OsmWay(osm_id=4, coords=((6.0, 6.0), (4.0, 6.0), (4.0, 4.0)), tags={})
    relation = OsmRelation(
        osm_id=100,
        tags={},
        members=(
            OsmRelationMember(1, role="outer"),
            OsmRelationMember(2, role="outer"),
            OsmRelationMember(3, role="outline"),
            OsmRelationMember(4, role="outline"),
        ),
    )
    elements = OsmElements(
        nodes=(), ways=(outer_a, outer_b, stray_a, stray_b), relations=(relation,)
    )

    from shapely.geometry import Point

    polygon = elements.relation_polygon(relation)
    assert polygon is not None
    assert len(polygon.interiors) == 0
    assert polygon.covers(Point(5.0, 5.0))


def test_relation_polygon_ignores_an_inner_ring_that_never_closes():
    outer_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)), tags={})
    outer_b = OsmWay(osm_id=2, coords=((10.0, 10.0), (0.0, 10.0), (0.0, 0.0)), tags={})
    open_inner = OsmWay(osm_id=3, coords=((4.0, 4.0), (6.0, 4.0)), tags={})
    relation = OsmRelation(
        osm_id=100,
        tags={},
        members=(
            OsmRelationMember(1, role="outer"),
            OsmRelationMember(2, role="outer"),
            OsmRelationMember(3, role="inner"),
        ),
    )
    elements = OsmElements(
        nodes=(), ways=(outer_a, outer_b, open_inner), relations=(relation,)
    )

    from shapely.geometry import Point

    polygon = elements.relation_polygon(relation)
    assert polygon is not None
    assert len(polygon.interiors) == 0
    assert polygon.covers(Point(5.0, 5.0))
