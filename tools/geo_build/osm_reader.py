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


@dataclass(frozen=True)
class OsmRelation:
    osm_id: int
    tags: Mapping[str, str]
    member_way_ids: tuple[int, ...]

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


@dataclass(frozen=True)
class OsmElements:

    nodes: tuple[OsmNode, ...]
    ways: tuple[OsmWay, ...]
    relations: tuple[OsmRelation, ...]

    @cached_property
    def _ways_by_id(self) -> dict[int, OsmWay]:
        return {w.osm_id: w for w in self.ways}

    def relation_centroid(self, relation: OsmRelation) -> tuple[float, float] | None:
        from shapely.geometry import LineString, MultiLineString
        from shapely.ops import polygonize, unary_union

        lines = [
            LineString(way.coords)
            for way_id in relation.member_way_ids
            if (way := self._ways_by_id.get(way_id)) is not None and len(way.coords) >= 2
        ]
        if not lines:
            return None

        polygons = list(polygonize(lines))
        geom = unary_union(polygons) if polygons else (
            lines[0] if len(lines) == 1 else MultiLineString([tuple(ln.coords) for ln in lines])
        )
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
        member_way_ids = tuple(
            m.ref  # type: ignore[attr-defined]
            for m in r.members  # type: ignore[attr-defined]
            if m.type == "w"  # type: ignore[attr-defined]
        )
        self._relations.append(OsmRelation(osm_id=r.id, tags=tags, member_way_ids=member_way_ids))  # type: ignore[attr-defined]


def read_elements(path: str) -> OsmElements:
    handler = _ElementHandler()
    handler.apply_file(path, locations=True)
    return OsmElements(
        nodes=tuple(handler._nodes),
        ways=tuple(handler._ways),
        relations=tuple(handler._relations),
    )


__all__ = ["OsmElements", "OsmNode", "OsmRelation", "OsmWay", "read_elements"]
