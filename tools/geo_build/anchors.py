from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .geometry import KM_PER_DEG_LAT, haversine_km, km_per_deg_lon
from .routing import Coordinate, RoutingBackend, assert_finite_km, table_chunked

ANCHOR_COLUMNS: tuple[str, ...] = ("anchor_id", "latitude", "longitude", "selection_cell")

ANCHOR_DECIMAL_PLACES = 6

ACCESS_DISTANCE_DECIMAL_PLACES = 3

DEFAULT_ANCHOR_GRID_KM = 6.0

DEFAULT_ANCHOR_CANDIDATE_K = 8


@dataclass(frozen=True)
class Anchor:
    anchor_id: int
    lon: float
    lat: float
    selection_cell: str

    def coordinate(self) -> Coordinate:
        return (self.lon, self.lat)


@dataclass(frozen=True)
class AnchorGrid:

    origin_lon: float
    origin_lat: float
    ref_lat: float
    cell_km: float

    @classmethod
    def over(cls, bounds: tuple[float, float, float, float], cell_km: float) -> AnchorGrid:
        min_lon, min_lat, _, max_lat = bounds
        if cell_km <= 0:
            raise ValueError(f"anchor grid cell size must be positive, got {cell_km}")
        return cls(
            origin_lon=min_lon,
            origin_lat=min_lat,
            ref_lat=(min_lat + max_lat) / 2.0,
            cell_km=float(cell_km),
        )

    def cell_of(self, lon: float, lat: float) -> tuple[int, int]:
        x_km = (lon - self.origin_lon) * km_per_deg_lon(self.ref_lat)
        y_km = (lat - self.origin_lat) * KM_PER_DEG_LAT
        return (math.floor(y_km / self.cell_km), math.floor(x_km / self.cell_km))

    def cell_centroid(self, row: int, col: int) -> Coordinate:
        x_km = (col + 0.5) * self.cell_km
        y_km = (row + 0.5) * self.cell_km
        lon = self.origin_lon + x_km / km_per_deg_lon(self.ref_lat)
        lat = self.origin_lat + y_km / KM_PER_DEG_LAT
        return (lon, lat)

    @staticmethod
    def cell_label(row: int, col: int) -> str:
        return f"r{row:+05d}c{col:+05d}"

    def describe(self) -> dict[str, object]:
        return {
            "kind": "fixed_grid_cell_centroid_snap",
            "cell_km": self.cell_km,
            "origin_lon": round(self.origin_lon, ANCHOR_DECIMAL_PLACES),
            "origin_lat": round(self.origin_lat, ANCHOR_DECIMAL_PLACES),
            "reference_latitude": round(self.ref_lat, ANCHOR_DECIMAL_PLACES),
            "occupied_cell_rule": "at least one snapped usable address in the cell",
            "anchor_rule": "backend snap of the cell's geometric centroid",
            "ordering": "ascending (row, col); anchor_id is 1-based in that order",
            "duplicate_rule": "cells snapping to the same point collapse to the earlier cell",
        }


def select_anchors(
    grid: AnchorGrid, points: Sequence[Coordinate], backend: RoutingBackend
) -> list[Anchor]:
    occupied = sorted({grid.cell_of(lon, lat) for lon, lat in points})
    anchors: list[Anchor] = []
    seen: dict[tuple[float, float], int] = {}
    for row, col in occupied:
        centroid = grid.cell_centroid(row, col)
        snapped = backend.snap(*centroid)
        if snapped is None:
            continue
        key = (
            round(snapped.lon, ANCHOR_DECIMAL_PLACES),
            round(snapped.lat, ANCHOR_DECIMAL_PLACES),
        )
        if key in seen:
            continue
        seen[key] = len(anchors)
        anchors.append(
            Anchor(
                anchor_id=len(anchors) + 1,
                lon=key[0],
                lat=key[1],
                selection_cell=grid.cell_label(row, col),
            )
        )
    return anchors


def renumber(anchors: Sequence[Anchor], keep: Sequence[int]) -> list[Anchor]:
    return [
        Anchor(
            anchor_id=i + 1,
            lon=anchors[position].lon,
            lat=anchors[position].lat,
            selection_cell=anchors[position].selection_cell,
        )
        for i, position in enumerate(keep)
    ]


@dataclass(frozen=True)
class AccessAssignment:

    anchor_id: int | None
    access_distance_km: float | None


def assign_access_distances(
    points: Sequence[Coordinate],
    anchors: Sequence[Anchor],
    backend: RoutingBackend,
    *,
    grid: AnchorGrid,
    k: int = DEFAULT_ANCHOR_CANDIDATE_K,
) -> list[AccessAssignment]:
    if not anchors:
        return [AccessAssignment(None, None) for _ in points]

    by_cell = _anchors_by_cell(grid, anchors)
    extent = _cell_extent(by_cell)
    candidates = [
        _candidate_anchor_positions(grid, by_cell, extent, anchors, p, k) for p in points
    ]

    groups: dict[tuple[int, ...], list[int]] = {}
    for index, candidate in enumerate(candidates):
        groups.setdefault(tuple(candidate), []).append(index)

    results: list[AccessAssignment | None] = [None] * len(points)
    for candidate_positions, indices in sorted(groups.items()):
        if not candidate_positions:
            for index in indices:
                results[index] = AccessAssignment(None, None)
            continue
        sources = [points[i] for i in indices]
        destinations = [anchors[p].coordinate() for p in candidate_positions]
        table = table_chunked(backend, sources, destinations)
        for row_index, index in enumerate(indices):
            best_km: float | None = None
            best_anchor: int | None = None
            for column, position in enumerate(candidate_positions):
                value = table[row_index][column]
                if not assert_finite_km(value):
                    continue
                if best_km is None or value < best_km:
                    best_km = value
                    best_anchor = anchors[position].anchor_id
            if best_km is None:
                results[index] = AccessAssignment(None, None)
            else:
                results[index] = AccessAssignment(
                    anchor_id=best_anchor,
                    access_distance_km=round(best_km, ACCESS_DISTANCE_DECIMAL_PLACES),
                )
    return [r if r is not None else AccessAssignment(None, None) for r in results]


def _anchors_by_cell(
    grid: AnchorGrid, anchors: Sequence[Anchor]
) -> dict[tuple[int, int], list[int]]:
    index: dict[tuple[int, int], list[int]] = {}
    for position, anchor in enumerate(anchors):
        index.setdefault(grid.cell_of(anchor.lon, anchor.lat), []).append(position)
    return index


def _cell_extent(
    by_cell: dict[tuple[int, int], list[int]],
) -> tuple[int, int, int, int] | None:
    if not by_cell:
        return None
    rows = [r for r, _ in by_cell]
    cols = [c for _, c in by_cell]
    return (min(rows), max(rows), min(cols), max(cols))


def _candidate_anchor_positions(
    grid: AnchorGrid,
    by_cell: dict[tuple[int, int], list[int]],
    extent: tuple[int, int, int, int] | None,
    anchors: Sequence[Anchor],
    point: Coordinate,
    k: int,
) -> list[int]:
    if k <= 0:
        raise ValueError(f"anchor candidate k must be positive, got {k}")
    row, col = grid.cell_of(*point)
    collected: list[int] = []
    radius = 0
    max_radius = _max_ring_radius(extent, row, col)
    extra_ring_taken = False
    while True:
        for cell in _ring_cells(row, col, radius):
            collected.extend(by_cell.get(cell, ()))
        if len(collected) >= k:
            if extra_ring_taken:
                break
            extra_ring_taken = True
        if radius > max_radius:
            break
        radius += 1

    if not collected:
        collected = list(range(len(anchors)))
    ranked = sorted(
        collected,
        key=lambda position: (
            haversine_km(point[0], point[1], anchors[position].lon, anchors[position].lat),
            position,
        ),
    )
    return sorted(ranked[:k])


def _ring_cells(row: int, col: int, radius: int) -> list[tuple[int, int]]:
    if radius == 0:
        return [(row, col)]
    cells: list[tuple[int, int]] = []
    for d in range(-radius, radius + 1):
        cells.append((row - radius, col + d))
        cells.append((row + radius, col + d))
    for d in range(-radius + 1, radius):
        cells.append((row + d, col - radius))
        cells.append((row + d, col + radius))
    return cells


def _max_ring_radius(extent: tuple[int, int, int, int] | None, row: int, col: int) -> int:
    if extent is None:
        return 0
    min_row, max_row, min_col, max_col = extent
    return max(abs(min_row - row), abs(max_row - row), abs(min_col - col), abs(max_col - col))


__all__ = [
    "ACCESS_DISTANCE_DECIMAL_PLACES",
    "ANCHOR_COLUMNS",
    "ANCHOR_DECIMAL_PLACES",
    "DEFAULT_ANCHOR_CANDIDATE_K",
    "DEFAULT_ANCHOR_GRID_KM",
    "AccessAssignment",
    "Anchor",
    "AnchorGrid",
    "assign_access_distances",
    "renumber",
    "select_anchors",
]
