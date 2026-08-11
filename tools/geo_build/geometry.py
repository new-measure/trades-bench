from __future__ import annotations

import math
from decimal import Decimal

from shapely.affinity import affine_transform
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points
from shapely.validation import make_valid

EARTH_RADIUS_KM = 6371.0088

_KM_PER_DEG_LAT = 110.574


def _km_per_deg_lon(lat_deg: float) -> float:
    return 111.320 * math.cos(math.radians(lat_deg))


KM_PER_DEG_LAT = _KM_PER_DEG_LAT
km_per_deg_lon = _km_per_deg_lon


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _to_km_matrix(ref_lat: float) -> list[float]:
    return [_km_per_deg_lon(ref_lat), 0, 0, _KM_PER_DEG_LAT, 0, 0]


def _from_km_matrix(ref_lat: float) -> list[float]:
    km_per_lon = _km_per_deg_lon(ref_lat)
    return [1 / km_per_lon, 0, 0, 1 / _KM_PER_DEG_LAT, 0, 0]


def buffer_km(geometry: BaseGeometry, km: Decimal | float, *, ref_lat: float | None = None):
    if ref_lat is None:
        ref_lat = geometry.centroid.y
    to_km = affine_transform(geometry, _to_km_matrix(ref_lat))
    buffered = to_km.buffer(float(km))
    return affine_transform(buffered, _from_km_matrix(ref_lat))


def containing_index(lon: float, lat: float, polygons: list[BaseGeometry]) -> int | None:
    from shapely.geometry import Point

    pt = Point(lon, lat)
    for i, poly in enumerate(polygons):
        if poly.covers(pt):
            return i
    return None


def nearest_index(lon: float, lat: float, polygons: list[BaseGeometry]) -> int:
    from shapely.geometry import Point

    ref_lat = lat
    to_km = _to_km_matrix(ref_lat)
    from_km = _from_km_matrix(ref_lat)
    query_km = affine_transform(Point(lon, lat), to_km)

    best_i: int | None = None
    best_km: float | None = None
    for i, poly in enumerate(polygons):
        poly_km = affine_transform(poly, to_km)
        nearest_on_poly_km, _ = nearest_points(poly_km, query_km)
        nearest_on_poly = affine_transform(nearest_on_poly_km, from_km)
        d = haversine_km(lon, lat, nearest_on_poly.x, nearest_on_poly.y)
        if best_km is None or d < best_km:
            best_km = d
            best_i = i
    assert best_i is not None
    return best_i


_DISJOINT_AREA_TOLERANCE = 1e-12


def assert_pairwise_disjoint(named_polygons: dict[str, BaseGeometry]) -> None:
    names = sorted(named_polygons)
    for i, name_a in enumerate(names):
        poly_a = named_polygons[name_a]
        for name_b in names[i + 1 :]:
            poly_b = named_polygons[name_b]
            overlap_area = poly_a.intersection(poly_b).area
            if overlap_area > _DISJOINT_AREA_TOLERANCE:
                raise ValueError(
                    f"zone polygons are not pairwise disjoint: {name_a!r} and "
                    f"{name_b!r} overlap (intersection area {overlap_area:.3e} deg^2)"
                )


def validate_or_repair_polygon(polygon: BaseGeometry, *, label: str) -> BaseGeometry:
    if polygon.is_valid:
        return polygon
    repaired = make_valid(polygon)
    if not repaired.is_valid or repaired.is_empty or repaired.area <= 0:
        raise ValueError(
            f"{label}: assembled polygon is invalid (self-intersecting ring?) "
            "and could not be repaired by shapely.make_valid into a positive-area polygon"
        )
    return repaired


__all__ = [
    "EARTH_RADIUS_KM",
    "KM_PER_DEG_LAT",
    "assert_pairwise_disjoint",
    "buffer_km",
    "containing_index",
    "haversine_km",
    "km_per_deg_lon",
    "nearest_index",
    "validate_or_repair_polygon",
]
