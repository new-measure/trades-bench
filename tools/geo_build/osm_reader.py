"""Single-pass pyosmium reading of address-bearing OSM elements.

Deliberately separate from :mod:`tools.geo_build.zones`, which does its own
pass with an ``area()`` callback to get pyosmium's automatic multipolygon
assembly for administrative relations. Mixing that callback into this reader
would make *every* closed way in the file (any building, any tagged
courtyard) independently re-emitted as a second, ring-assembled ``Area``
object alongside the plain ``way()`` callback — pyosmium's two-pass area
assembler runs off tag heuristics, not off what this module happens to care
about. Keeping this reader to ``node()``/``way()``/``relation()`` only means
one element, one callback, no double-counting.

Reads both ``.osm`` XML and ``.osm.pbf`` — pyosmium handles both by extension,
which is the whole reason the ticket prefers it over shelling out to
``osmium-tool``.
"""

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
    coords: tuple[tuple[float, float], ...]  # (lon, lat) in way order
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
    """Geometry centroid of a way's coordinates (research/osm-data.md §4.2:
    "For ways and relations the candidate's coordinate is the geometry
    centroid"). A closed ring with enough points is treated as a polygon
    (area centroid); anything else falls back to a line/point centroid so a
    two-node or open way never crashes the build.
    """
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
    """Everything :mod:`tools.geo_build.extract` needs from one pass over an
    OSM source: tagged nodes, tagged ways (with resolved node coordinates),
    and tagged relations (address-bearing ones are vanishingly rare in
    practice, but the spec names them as a candidate source, so they are
    carried through rather than silently dropped).
    """

    nodes: tuple[OsmNode, ...]
    ways: tuple[OsmWay, ...]
    relations: tuple[OsmRelation, ...]

    @cached_property
    def _ways_by_id(self) -> dict[int, OsmWay]:
        """Built once per instance, not once per :meth:`relation_centroid`
        call — the naive per-call rebuild is O(ways) every time and this
        method is called once per relation from both the crop and extract
        stages, making the whole pipeline O(ways x relations) (benchmarked
        at ~3.5ms/call over 200k ways, extrapolating to ~1.6h on a real
        Arizona extract). ``functools.cached_property`` writes into the
        instance's own ``__dict__`` directly rather than through
        ``__setattr__``, so it works on a frozen dataclass without needing
        ``object.__setattr__`` tricks.
        """
        return {w.osm_id: w for w in self.ways}

    def relation_centroid(self, relation: OsmRelation) -> tuple[float, float] | None:
        """Geometry centroid of a relation's member ways (research/osm-data.md
        §4.2: "For ways and relations the candidate's coordinate is the
        geometry centroid"), or ``None`` if none of its members resolved to a
        way this pass captured.

        Mirrors :func:`_coords_centroid`'s approach rather than averaging
        member-node coordinates directly: an arithmetic mean over all member
        nodes is node-density-weighted, not area-weighted, and can sit
        meaningfully far from the true geometric centroid (a constructed
        repro puts it 5.2 km off) whenever nodes cluster unevenly around the
        ring (a common real-world shape — dense nodes tracing a curved
        street frontage, sparse ones along a straight back edge). Builds the
        member ways' line geometry, polygonizes it into ring(s) where
        possible (the common closed-ring shape), and falls back to the raw
        line geometry's own centroid when the ways do not form a closed ring
        (e.g. a single open way, as in the two-node case this module's own
        tests exercise).
        """
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
        # Unlike `node()`, this does *not* require tags: a way that is a
        # member of an address-tagged relation (§4.2 names relations as a
        # candidate source) carries the address on the *relation*, not on
        # itself, and `OsmElements.relation_centroid` needs this way's
        # coordinates to be available regardless. Untagged ways still need a
        # resolvable coordinate list to be worth keeping.
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
    """One pass over ``path`` (``.osm`` or ``.osm.pbf``) collecting every
    tagged node, way, and relation with resolved coordinates.
    """
    handler = _ElementHandler()
    handler.apply_file(path, locations=True)
    return OsmElements(
        nodes=tuple(handler._nodes),
        ways=tuple(handler._ways),
        relations=tuple(handler._relations),
    )


__all__ = ["OsmElements", "OsmNode", "OsmRelation", "OsmWay", "read_elements"]
