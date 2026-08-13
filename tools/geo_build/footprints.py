from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from .osm_reader import OsmElements, OsmWay

_APARTMENT_FOOTPRINT_BUILDING_VALUES = frozenset({"apartments", "dormitory"})


def _closed_way_polygon(way: OsmWay):
    if len(way.coords) < 4 or way.coords[0] != way.coords[-1]:
        return None
    from shapely.geometry import Polygon

    from . import geometry

    try:
        return geometry.validate_or_repair_polygon(
            Polygon(way.coords), label=f"way {way.osm_id} footprint polygon"
        )
    except ValueError:
        return None


@dataclass(frozen=True)
class ApartmentFootprints:

    polygons: tuple

    @cached_property
    def _tree(self):
        from shapely.strtree import STRtree

        return STRtree(self.polygons)

    def covers_point(self, longitude: float, latitude: float) -> bool:
        from shapely.geometry import Point

        if not self.polygons:
            return False
        pt = Point(longitude, latitude)
        return len(self._tree.query(pt, predicate="covered_by")) > 0


def build_apartment_footprints(elements: OsmElements) -> ApartmentFootprints:
    found: list[tuple[str, int, object]] = []

    for way in elements.ways:
        if way.tags.get("building") not in _APARTMENT_FOOTPRINT_BUILDING_VALUES:
            continue
        polygon = _closed_way_polygon(way)
        if polygon is not None:
            found.append(("way", way.osm_id, polygon))

    for relation in elements.relations:
        if relation.tags.get("building") not in _APARTMENT_FOOTPRINT_BUILDING_VALUES:
            continue
        polygon = elements.relation_polygon(relation)
        if polygon is not None:
            found.append(("relation", relation.osm_id, polygon))

    found.sort(key=lambda item: (item[0], item[1]))
    return ApartmentFootprints(polygons=tuple(polygon for _, _, polygon in found))


__all__ = ["ApartmentFootprints", "build_apartment_footprints"]
