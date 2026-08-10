"""Geometry helpers shared by the zone, crop, and zone-assignment stages.

Hand-rolled haversine plus a local equirectangular projection stand in for a
full geodesic stack (pyproj), per the ticket's guidance: this build area is a
single Arizona metro, small enough that a locally-scaled planar approximation
introduces negligible error for a buffer distance or a nearest-zone tie-break,
and stdlib `math` + the already-required `shapely` cover it without adding a
third geo dependency.
"""

from __future__ import annotations

import math
from decimal import Decimal

from shapely.affinity import affine_transform
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points
from shapely.validation import make_valid

#: Mean Earth radius (km) for the haversine formula (IUGS/WGS-84 mean radius).
EARTH_RADIUS_KM = 6371.0088

#: Kilometres per degree of latitude — constant to good approximation everywhere.
_KM_PER_DEG_LAT = 110.574


def _km_per_deg_lon(lat_deg: float) -> float:
    """Kilometres per degree of longitude at a given latitude (shrinks toward
    the poles as meridians converge; exact at the equator).
    """
    return 111.320 * math.cos(math.radians(lat_deg))


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in km between two WGS-84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _to_km_matrix(ref_lat: float) -> list[float]:
    """``affine_transform`` matrix mapping (lon, lat) degrees to a local
    kilometre-space centered on ``ref_lat`` — the anisotropic degree grid
    (1° lon != 1° lat except at the equator) rescaled so both axes are
    approximately isotropic in real distance near that latitude.
    """
    return [_km_per_deg_lon(ref_lat), 0, 0, _KM_PER_DEG_LAT, 0, 0]


def _from_km_matrix(ref_lat: float) -> list[float]:
    km_per_lon = _km_per_deg_lon(ref_lat)
    return [1 / km_per_lon, 0, 0, 1 / _KM_PER_DEG_LAT, 0, 0]


def buffer_km(geometry: BaseGeometry, km: Decimal | float, *, ref_lat: float | None = None):
    """Buffer a WGS-84 geometry outward by ``km``, approximately.

    Scales lon/lat into a local kilometre-space centered on ``ref_lat`` (the
    geometry's own centroid latitude by default), buffers there with GEOS,
    then scales back. This is a deliberate approximation (no true geodesic
    buffer): good enough for a "wide enough to matter" outlying ring, not
    claimed to be survey-grade.
    """
    if ref_lat is None:
        ref_lat = geometry.centroid.y
    to_km = affine_transform(geometry, _to_km_matrix(ref_lat))
    buffered = to_km.buffer(float(km))
    return affine_transform(buffered, _from_km_matrix(ref_lat))


def containing_index(lon: float, lat: float, polygons: list[BaseGeometry]) -> int | None:
    """Index of the first polygon (in list order) covering ``(lon, lat)``, or
    ``None`` if none does.

    ``covers`` (boundary-inclusive) rather than ``contains`` so a point
    exactly on a shared edge between two disjoint-by-interior zones still
    resolves — to the earlier (lower-index) polygon, consistent with this
    file's "ties broken by lowest zone_id" convention elsewhere.
    """
    from shapely.geometry import Point

    pt = Point(lon, lat)
    for i, poly in enumerate(polygons):
        if poly.covers(pt):
            return i
    return None


def nearest_index(lon: float, lat: float, polygons: list[BaseGeometry]) -> int:
    """Index of the geodesically nearest polygon to ``(lon, lat)``, ties
    broken by the lowest index (03 §2.4: "ties broken by lowest zone_id" —
    callers pass polygons in ascending zone_id order).

    The nearest-point *search* runs in the same locally-metric kilometre
    frame :func:`buffer_km` builds (``ref_lat`` = the query point's own
    latitude), not the raw lon/lat plane: degrees are anisotropic at any
    latitude away from the equator (at Phoenix's ~33.5°N, 1° lon is ~92.8 km
    against 1° lat's ~110.6 km), so a planar-degree-space nearest-point
    search can pick the wrong foot on a non-axis-aligned boundary edge — the
    foot that minimizes raw degree distance is not the one that minimizes
    real km distance unless the edge happens to run exactly north-south or
    east-west. That wrong foot can in turn make an objectively farther
    polygon appear nearer (a real regression test below constructs exactly
    this flip: a 45°-oriented boundary edge vs. an axis-aligned one, ordered
    correctly here and incorrectly under a raw-degree-space search). Ranking
    is still by exact haversine — from the *found* foot back in lon/lat
    degrees to the query point — so distances stay geodesic-accurate; only
    the search step changes frame.
    """
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
    assert best_i is not None  # polygons is never empty by construction
    return best_i


#: Area (in squared degrees) below which two polygons are treated as merely
#: touching, not overlapping — GEOS boundary-touch intersections carry tiny
#: floating-point residue rather than exact zero.
_DISJOINT_AREA_TOLERANCE = 1e-12


def assert_pairwise_disjoint(named_polygons: dict[str, BaseGeometry]) -> None:
    """Raise ``ValueError`` naming the first overlapping pair found, in a
    deterministic (sorted-name) scan order, or return silently if every pair
    is disjoint (touching edges are fine; overlapping interiors are not).
    """
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
    """Guard against a self-intersecting (or otherwise invalid) assembled
    polygon slipping silently into split/union/disjointness math.

    GEOS operations (``intersection``, ``area``, ``union``, ...) assume
    valid input; run on an invalid geometry — a self-intersecting "bowtie"
    ring is the common real-world OSM admin-relation shape — they can return
    silently wrong results (e.g. an ``intersection().area`` that neither
    matches the true overlap nor raises) rather than an error, which is
    exactly the failure mode :func:`assert_pairwise_disjoint` must not be
    fooled by. If ``polygon`` is already valid it is returned unchanged;
    otherwise this attempts ``shapely.make_valid`` exactly once and returns
    the repaired geometry (a bowtie generally repairs cleanly into a valid
    ``MultiPolygon`` of its non-crossing pieces); if the repair attempt is
    itself invalid or degenerates to nothing, this raises loudly naming
    ``label`` rather than letting a garbage geometry continue.
    """
    if polygon.is_valid:
        return polygon
    repaired = make_valid(polygon)
    # A repair that is technically GEOS-valid but has degenerated to zero
    # area (a Point or LineString rather than a Polygon/MultiPolygon — seen
    # from an all-coincident-vertex ring) is just as unusable as an outright
    # invalid result for something that must be a real municipal polygon.
    if not repaired.is_valid or repaired.is_empty or repaired.area <= 0:
        raise ValueError(
            f"{label}: assembled polygon is invalid (self-intersecting ring?) "
            "and could not be repaired by shapely.make_valid into a positive-area polygon"
        )
    return repaired


__all__ = [
    "EARTH_RADIUS_KM",
    "assert_pairwise_disjoint",
    "buffer_km",
    "containing_index",
    "haversine_km",
    "nearest_index",
    "validate_or_repair_polygon",
]
