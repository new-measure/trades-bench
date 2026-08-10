"""Zone assignment stage (research/osm-data.md §4.7; 03 §2.4).

Pure function of a point and the ten ordered zone polygons — no OSM parsing,
no candidate mutation here, so it is unit-testable with plain synthetic
polygons independent of every other stage.
"""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry

from . import geometry


def assign_zone(
    longitude: float, latitude: float, zone_polygons_in_order: list[BaseGeometry]
) -> tuple[int | None, int]:
    """``(zone_id, effective_zone_id)`` for one point.

    ``zone_polygons_in_order`` must be ordered by ascending ``zone_id``
    starting at 1 (:meth:`tools.geo_build.zones.ZoneExtractionResult.zone_polygons_in_order`
    is the intended source) — both the containment scan and the nearest-zone
    tie-break resolve to the *first* matching index, i.e. the lowest
    ``zone_id`` (03 §2.4: "ties broken by lowest zone_id").
    """
    idx = geometry.containing_index(longitude, latitude, zone_polygons_in_order)
    if idx is not None:
        zone_id = idx + 1
        return zone_id, zone_id
    nearest_idx = geometry.nearest_index(longitude, latitude, zone_polygons_in_order)
    return None, nearest_idx + 1


__all__ = ["assign_zone"]
