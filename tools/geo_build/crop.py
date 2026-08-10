"""Crop stage (research/osm-data.md §4.1).

"Compute the build area as the union of the ten zone polygons buffered by
``geography.outlying_buffer_km``. Crop the snapshot to that polygon.
Everything downstream reads only the crop." This module does both halves:
computing the build-area polygon, and filtering already-read OSM elements
(:mod:`tools.geo_build.osm_reader`) down to the ones whose centroid falls
inside it — an in-memory filter rather than an ``osmium extract`` subprocess,
which keeps the whole pipeline pyosmium/shapely and avoids a "binary missing"
failure mode the ticket asks to gate behind if it ever became necessary.
"""

from __future__ import annotations

from decimal import Decimal

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .geometry import buffer_km
from .osm_reader import OsmElements, OsmNode, OsmRelation, OsmWay


def build_area(zone_polygons: list[BaseGeometry], outlying_buffer_km: Decimal) -> BaseGeometry:
    """The service core (union of zone polygons) buffered outward by
    ``outlying_buffer_km`` (03 §2.2: "The artifact also covers an outlying
    ring: everything inside the cropped build area but outside the service
    core").
    """
    service_core = unary_union(zone_polygons)
    return buffer_km(service_core, outlying_buffer_km)


def _element_centroid(element: OsmNode | OsmWay | OsmRelation, elements: OsmElements):
    if isinstance(element, OsmRelation):
        return elements.relation_centroid(element)
    return element.centroid


def filter_to_build_area(elements: OsmElements, area: BaseGeometry) -> OsmElements:
    """Keep only nodes/ways/relations whose centroid falls inside ``area``.

    ``covers`` (boundary-inclusive) so a point exactly on the buffer edge is
    kept rather than arbitrarily dropped.
    """
    kept_nodes = tuple(n for n in elements.nodes if area.covers(Point(*n.centroid)))
    kept_ways = tuple(w for w in elements.ways if area.covers(Point(*w.centroid)))
    kept_relations = tuple(
        r
        for r in elements.relations
        if (c := elements.relation_centroid(r)) is not None and area.covers(Point(*c))
    )
    return OsmElements(nodes=kept_nodes, ways=kept_ways, relations=kept_relations)


__all__ = ["build_area", "filter_to_build_area"]
