from __future__ import annotations

from decimal import Decimal

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .geometry import buffer_km
from .osm_reader import OsmElements, OsmNode, OsmRelation, OsmWay


def build_area(zone_polygons: list[BaseGeometry], outlying_buffer_km: Decimal) -> BaseGeometry:
    service_core = unary_union(zone_polygons)
    return buffer_km(service_core, outlying_buffer_km)


def _element_centroid(element: OsmNode | OsmWay | OsmRelation, elements: OsmElements):
    if isinstance(element, OsmRelation):
        return elements.relation_centroid(element)
    return element.centroid


def filter_to_build_area(elements: OsmElements, area: BaseGeometry) -> OsmElements:
    kept_nodes = tuple(n for n in elements.nodes if area.covers(Point(*n.centroid)))
    kept_ways = tuple(w for w in elements.ways if area.covers(Point(*w.centroid)))
    kept_relations = tuple(
        r
        for r in elements.relations
        if (c := elements.relation_centroid(r)) is not None and area.covers(Point(*c))
    )
    return OsmElements(nodes=kept_nodes, ways=kept_ways, relations=kept_relations)


def build_area_sha256(area: BaseGeometry) -> str:
    import hashlib
    import json

    from shapely.geometry import mapping

    from .serialize import canonical_geometry_payload

    payload = canonical_geometry_payload(mapping(area))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["build_area", "build_area_sha256", "filter_to_build_area"]
