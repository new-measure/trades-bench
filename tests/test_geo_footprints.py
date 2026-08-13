from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build.footprints import build_apartment_footprints  # noqa: E402
from geo_build.osm_reader import (  # noqa: E402
    OsmElements,
    OsmRelation,
    OsmRelationMember,
    OsmWay,
)

_SQUARE = ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (0.0, 0.0))


def _square_way(osm_id: int, building: str = "apartments") -> OsmWay:
    return OsmWay(osm_id=osm_id, coords=_SQUARE, tags={"building": building})


def test_node_inside_an_apartments_footprint_way_is_covered():
    elements = OsmElements(nodes=(), ways=(_square_way(1),), relations=())
    footprints = build_apartment_footprints(elements)
    assert footprints.covers_point(1.0, 1.0) is True


def test_node_outside_every_footprint_way_is_not_covered():
    elements = OsmElements(nodes=(), ways=(_square_way(1),), relations=())
    footprints = build_apartment_footprints(elements)
    assert footprints.covers_point(50.0, 50.0) is False


def test_dormitory_footprint_way_also_covers():
    elements = OsmElements(nodes=(), ways=(_square_way(1, building="dormitory"),), relations=())
    footprints = build_apartment_footprints(elements)
    assert footprints.covers_point(1.0, 1.0) is True


def test_non_apartment_building_way_is_not_a_footprint():
    elements = OsmElements(nodes=(), ways=(_square_way(1, building="house"),), relations=())
    footprints = build_apartment_footprints(elements)
    assert footprints.covers_point(1.0, 1.0) is False


def test_an_open_way_never_becomes_a_footprint():
    way = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0), (2.0, 2.0)), tags={"building": "apartments"})
    elements = OsmElements(nodes=(), ways=(way,), relations=())
    footprints = build_apartment_footprints(elements)
    assert footprints.covers_point(1.0, 1.0) is False


def test_no_footprints_at_all_never_covers_anything():
    elements = OsmElements(nodes=(), ways=(), relations=())
    footprints = build_apartment_footprints(elements)
    assert footprints.covers_point(0.0, 0.0) is False


def test_multipolygon_relation_footprint_covers_its_interior():
    way_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0), (2.0, 2.0)), tags={})
    way_b = OsmWay(osm_id=2, coords=((2.0, 2.0), (0.0, 2.0), (0.0, 0.0)), tags={})
    relation = OsmRelation(
        osm_id=100,
        tags={"building": "apartments"},
        members=(OsmRelationMember(1), OsmRelationMember(2)),
    )
    elements = OsmElements(nodes=(), ways=(way_a, way_b), relations=(relation,))
    footprints = build_apartment_footprints(elements)
    assert footprints.covers_point(1.0, 1.0) is True
    assert footprints.covers_point(5.0, 5.0) is False


def test_a_relation_without_the_apartment_building_tag_is_not_a_footprint():
    way_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (2.0, 0.0), (2.0, 2.0)), tags={})
    way_b = OsmWay(osm_id=2, coords=((2.0, 2.0), (0.0, 2.0), (0.0, 0.0)), tags={})
    relation = OsmRelation(
        osm_id=100,
        tags={"building": "house"},
        members=(OsmRelationMember(1), OsmRelationMember(2)),
    )
    elements = OsmElements(nodes=(), ways=(way_a, way_b), relations=(relation,))
    footprints = build_apartment_footprints(elements)
    assert footprints.covers_point(1.0, 1.0) is False


def test_apartment_footprint_courtyard_hole_is_not_covered_but_the_ring_is():
    outer_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)), tags={})
    outer_b = OsmWay(osm_id=2, coords=((10.0, 10.0), (0.0, 10.0), (0.0, 0.0)), tags={})
    inner_a = OsmWay(osm_id=3, coords=((4.0, 4.0), (6.0, 4.0), (6.0, 6.0)), tags={})
    inner_b = OsmWay(osm_id=4, coords=((6.0, 6.0), (4.0, 6.0), (4.0, 4.0)), tags={})
    relation = OsmRelation(
        osm_id=100,
        tags={"building": "apartments"},
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
    footprints = build_apartment_footprints(elements)

    assert footprints.covers_point(1.0, 1.0) is True
    assert footprints.covers_point(5.0, 5.0) is False


def test_an_invalid_self_intersecting_footprint_does_not_crash_and_still_joins_after_repair(
    monkeypatch,
):
    import shapely.ops
    from shapely.geometry import Polygon

    bowtie = Polygon([(0.0, 0.0), (4.0, 4.0), (4.0, 0.0), (0.0, 4.0), (0.0, 0.0)])
    assert not bowtie.is_valid
    monkeypatch.setattr(shapely.ops, "polygonize", lambda lines: [bowtie])

    way_a = OsmWay(osm_id=1, coords=((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 0.0)), tags={})
    way_b = OsmWay(osm_id=2, coords=((0.0, 4.0), (4.0, 4.0), (0.0, 0.0)), tags={})
    relation = OsmRelation(
        osm_id=100,
        tags={"building": "apartments"},
        members=(OsmRelationMember(1), OsmRelationMember(2)),
    )
    elements = OsmElements(nodes=(), ways=(way_a, way_b), relations=(relation,))

    footprints = build_apartment_footprints(elements)
    assert footprints.covers_point(0.5, 0.5) is True
    assert footprints.covers_point(50.0, 50.0) is False


def test_an_unclosed_way_with_four_or_more_coords_never_becomes_a_footprint():
    way = OsmWay(
        osm_id=1,
        coords=((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)),
        tags={"building": "apartments"},
    )
    elements = OsmElements(nodes=(), ways=(way,), relations=())
    footprints = build_apartment_footprints(elements)
    assert footprints.polygons == ()
    assert footprints.covers_point(1.0, 1.0) is False


def test_a_self_intersecting_way_footprint_is_repaired_into_a_valid_polygon():
    way = OsmWay(
        osm_id=1,
        coords=((0.0, 0.0), (4.0, 4.0), (4.0, 0.0), (0.0, 4.0), (0.0, 0.0)),
        tags={"building": "apartments"},
    )
    elements = OsmElements(nodes=(), ways=(way,), relations=())
    footprints = build_apartment_footprints(elements)

    assert len(footprints.polygons) == 1
    assert footprints.polygons[0].is_valid
    assert footprints.polygons[0].area == pytest.approx(8.0)
    assert footprints.covers_point(0.5, 0.5) is True
    assert footprints.covers_point(2.0, 0.5) is False


def test_a_degenerate_zero_area_ring_produces_no_footprint_and_does_not_crash():
    way = OsmWay(
        osm_id=1,
        coords=((0.0, 0.0), (2.0, 0.0), (4.0, 0.0), (0.0, 0.0)),
        tags={"building": "apartments"},
    )
    elements = OsmElements(nodes=(), ways=(way,), relations=())
    footprints = build_apartment_footprints(elements)
    assert footprints.polygons == ()
    assert footprints.covers_point(1.0, 0.0) is False


def test_build_apartment_footprints_polygon_sequence_does_not_depend_on_input_order():
    way_a = OsmWay(osm_id=1, coords=_SQUARE, tags={"building": "apartments"})
    far_square = ((10.0, 10.0), (12.0, 10.0), (12.0, 12.0), (10.0, 12.0), (10.0, 10.0))
    way_b = OsmWay(osm_id=2, coords=far_square, tags={"building": "dormitory"})
    relation = OsmRelation(
        osm_id=100,
        tags={"building": "apartments"},
        members=(OsmRelationMember(3, role="outer"), OsmRelationMember(4, role="outer")),
    )
    member_a = OsmWay(osm_id=3, coords=((20.0, 0.0), (22.0, 0.0), (22.0, 2.0)), tags={})
    member_b = OsmWay(osm_id=4, coords=((22.0, 2.0), (20.0, 2.0), (20.0, 0.0)), tags={})

    forward = OsmElements(
        nodes=(), ways=(way_a, way_b, member_a, member_b), relations=(relation,)
    )
    backward = OsmElements(
        nodes=(), ways=(member_b, member_a, way_b, way_a), relations=(relation,)
    )

    forward_wkt = [polygon.wkt for polygon in build_apartment_footprints(forward).polygons]
    backward_wkt = [polygon.wkt for polygon in build_apartment_footprints(backward).polygons]

    assert forward_wkt == backward_wkt
    assert len(forward_wkt) == 3
