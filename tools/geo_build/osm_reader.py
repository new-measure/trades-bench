from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property

import osmium


@dataclass(frozen=True)
class OsmNode:
    osm_id: int
    lon: float
    lat: float
    tags: Mapping[str, str]

    @property
    def element_type(self) -> str:
        return "node"

    @property
    def centroid(self) -> tuple[float, float]:
        return (self.lon, self.lat)


@dataclass(frozen=True)
class OsmWay:
    osm_id: int
    coords: tuple[tuple[float, float], ...]
    tags: Mapping[str, str]

    @property
    def element_type(self) -> str:
        return "way"

    @property
    def centroid(self) -> tuple[float, float]:
        return _coords_centroid(self.coords)


_OUTER_ROLES = frozenset({"", "outer"})


@dataclass(frozen=True)
class OsmRelationMember:
    way_id: int
    role: str = ""

    @property
    def is_outer(self) -> bool:
        return self.role in _OUTER_ROLES

    @property
    def is_inner(self) -> bool:
        return self.role == "inner"


@dataclass(frozen=True)
class OsmRelation:
    osm_id: int
    tags: Mapping[str, str]
    members: tuple[OsmRelationMember, ...]

    @property
    def element_type(self) -> str:
        return "relation"


def _coords_centroid(coords: tuple[tuple[float, float], ...]) -> tuple[float, float]:
    from shapely.geometry import LineString, Point, Polygon

    if len(coords) >= 4 and coords[0] == coords[-1]:
        geom = Polygon(coords)
    elif len(coords) >= 2:
        geom = LineString(coords)
    elif coords:
        geom = Point(coords[0])
    else:
        raise ValueError("cannot compute centroid of a way with no coordinates")
    c = geom.centroid
    return (c.x, c.y)


def _repair_polygonized(lines: list, *, label: str) -> list:
    from shapely.ops import polygonize

    from . import geometry

    repaired = []
    for polygon in polygonize(lines):
        try:
            repaired.append(geometry.validate_or_repair_polygon(polygon, label=label))
        except ValueError:
            continue
    return repaired


@dataclass(frozen=True)
class OsmElements:

    nodes: tuple[OsmNode, ...]
    ways: tuple[OsmWay, ...]
    relations: tuple[OsmRelation, ...]

    @cached_property
    def _ways_by_id(self) -> dict[int, OsmWay]:
        return {w.osm_id: w for w in self.ways}

    def _lines_for_way_ids(self, way_ids) -> list:
        from shapely.geometry import LineString

        return [
            LineString(way.coords)
            for way_id in way_ids
            if (way := self._ways_by_id.get(way_id)) is not None and len(way.coords) >= 2
        ]

    def _member_lines(self, relation: OsmRelation) -> list:
        return self._lines_for_way_ids(member.way_id for member in relation.members)

    def relation_polygon(self, relation: OsmRelation):
        from shapely.errors import GEOSException
        from shapely.ops import unary_union

        outer_way_ids = [m.way_id for m in relation.members if m.is_outer]
        inner_way_ids = [m.way_id for m in relation.members if m.is_inner]

        outer_lines = self._lines_for_way_ids(outer_way_ids)
        if not outer_lines:
            return None
        outer_polygons = _repair_polygonized(
            outer_lines, label=f"relation {relation.osm_id} outer footprint polygon"
        )
        if not outer_polygons:
            return None
        try:
            shell = unary_union(outer_polygons)
        except GEOSException:
            return None

        inner_lines = self._lines_for_way_ids(inner_way_ids)
        if not inner_lines:
            return shell
        inner_polygons = _repair_polygonized(
            inner_lines, label=f"relation {relation.osm_id} inner footprint polygon"
        )
        if not inner_polygons:
            return shell
        try:
            return shell.difference(unary_union(inner_polygons))
        except GEOSException:
            return shell

    def relation_centroid(self, relation: OsmRelation) -> tuple[float, float] | None:
        from shapely.errors import GEOSException
        from shapely.geometry import MultiLineString
        from shapely.ops import unary_union

        lines = self._member_lines(relation)
        if not lines:
            return None

        repaired_polygons = _repair_polygonized(
            lines, label=f"relation {relation.osm_id} member polygon"
        )

        try:
            geom = unary_union(repaired_polygons) if repaired_polygons else (
                lines[0] if len(lines) == 1 else MultiLineString([tuple(ln.coords) for ln in lines])
            )
        except GEOSException:
            return None
        c = geom.centroid
        return (c.x, c.y)


class _ElementHandler(osmium.SimpleHandler):
    def __init__(self) -> None:
        super().__init__()
        self._nodes: list[OsmNode] = []
        self._ways: list[OsmWay] = []
        self._relations: list[OsmRelation] = []

    def node(self, n: object) -> None:
        tags = dict(n.tags)  # type: ignore[attr-defined]
        if not tags:
            return
        loc = n.location  # type: ignore[attr-defined]
        if not loc.valid():
            return
        self._nodes.append(OsmNode(osm_id=n.id, lon=loc.lon, lat=loc.lat, tags=tags))  # type: ignore[attr-defined]

    def way(self, w: object) -> None:
        tags = dict(w.tags)  # type: ignore[attr-defined]
        coords = tuple(
            (nd.location.lon, nd.location.lat)
            for nd in w.nodes  # type: ignore[attr-defined]
            if nd.location.valid()
        )
        if not coords:
            return
        self._ways.append(OsmWay(osm_id=w.id, coords=coords, tags=tags))  # type: ignore[attr-defined]

    def relation(self, r: object) -> None:
        tags = dict(r.tags)  # type: ignore[attr-defined]
        if not tags:
            return
        members = tuple(
            OsmRelationMember(way_id=m.ref, role=m.role)  # type: ignore[attr-defined]
            for m in r.members  # type: ignore[attr-defined]
            if m.type == "w"  # type: ignore[attr-defined]
        )
        self._relations.append(OsmRelation(osm_id=r.id, tags=tags, members=members))  # type: ignore[attr-defined]


def read_elements(path: str) -> OsmElements:
    handler = _ElementHandler()
    handler.apply_file(path, locations=True)
    return OsmElements(
        nodes=tuple(handler._nodes),
        ways=tuple(handler._ways),
        relations=tuple(handler._relations),
    )


__all__ = [
    "OsmElements",
    "OsmNode",
    "OsmRelation",
    "OsmRelationMember",
    "OsmWay",
    "read_elements",
]
