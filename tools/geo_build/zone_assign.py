from __future__ import annotations

from shapely.geometry.base import BaseGeometry

from . import geometry


def assign_zone(
    longitude: float, latitude: float, zone_polygons_in_order: list[BaseGeometry]
) -> tuple[int | None, int]:
    idx = geometry.containing_index(longitude, latitude, zone_polygons_in_order)
    if idx is not None:
        zone_id = idx + 1
        return zone_id, zone_id
    nearest_idx = geometry.nearest_index(longitude, latitude, zone_polygons_in_order)
    return None, nearest_idx + 1


__all__ = ["assign_zone"]
