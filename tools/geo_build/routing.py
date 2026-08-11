from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .geometry import haversine_km

Coordinate = tuple[float, float]


@dataclass(frozen=True)
class SnapResult:

    lon: float
    lat: float
    distance_meters: float


@dataclass(frozen=True)
class BackendInfo:

    name: str
    version: str
    image_digest: str | None = None
    packageable: bool = False


@runtime_checkable
class RoutingBackend(Protocol):

    @property
    def info(self) -> BackendInfo:
        pass

    @property
    def max_table_coordinates(self) -> int:
        pass

    def snap(self, lon: float, lat: float) -> SnapResult | None:
        pass

    def table(
        self, sources: Sequence[Coordinate], destinations: Sequence[Coordinate]
    ) -> list[list[float | None]]:
        pass

    def route(self, a: Coordinate, b: Coordinate) -> float | None:
        pass


def table_chunked(
    backend: RoutingBackend,
    sources: Sequence[Coordinate],
    destinations: Sequence[Coordinate],
) -> list[list[float | None]]:
    limit = max(2, backend.max_table_coordinates)
    block = max(1, limit // 2)
    result: list[list[float | None]] = [[None] * len(destinations) for _ in sources]
    for s0 in range(0, len(sources), block):
        src_block = sources[s0 : s0 + block]
        for d0 in range(0, len(destinations), block):
            dst_block = destinations[d0 : d0 + block]
            sub = backend.table(src_block, dst_block)
            for i, row in enumerate(sub):
                result[s0 + i][d0 : d0 + len(dst_block)] = row
    return result


class FakeBackend:

    _KEY_DECIMALS = 6

    def __init__(
        self,
        *,
        detour_factor: float = 1.25,
        lattice_degrees: float = 0.0005,
        unsnappable: Sequence[Coordinate] = (),
        unroutable: Sequence[Coordinate] = (),
        snap_distance_floor_meters: float = 0.0,
        max_table_coordinates: int = 10_000,
    ) -> None:
        self.detour_factor = detour_factor
        self.lattice_degrees = lattice_degrees
        self._unsnappable = {self._key(*c) for c in unsnappable}
        self._unroutable = {self._key(*c) for c in unroutable}
        self._snap_distance_floor_meters = snap_distance_floor_meters
        self._max_table_coordinates = max_table_coordinates


    @property
    def info(self) -> BackendInfo:
        return BackendInfo(
            name="fake-straight-line",
            version=f"detour={self.detour_factor};lattice_deg={self.lattice_degrees}",
            image_digest=None,
            packageable=False,
        )

    @property
    def max_table_coordinates(self) -> int:
        return self._max_table_coordinates


    def snap(self, lon: float, lat: float) -> SnapResult | None:
        if self._key(lon, lat) in self._unsnappable:
            return None
        step = self.lattice_degrees
        slon = round(round(lon / step) * step, 9)
        slat = round(round(lat / step) * step, 9)
        meters = haversine_km(lon, lat, slon, slat) * 1000.0
        return SnapResult(
            lon=slon, lat=slat, distance_meters=max(meters, self._snap_distance_floor_meters)
        )

    def table(
        self, sources: Sequence[Coordinate], destinations: Sequence[Coordinate]
    ) -> list[list[float | None]]:
        if len(sources) + len(destinations) > self._max_table_coordinates:
            raise ValueError(
                "FakeBackend.table called with "
                f"{len(sources) + len(destinations)} coordinates, over its "
                f"{self._max_table_coordinates} limit — callers must chunk "
                "through routing.table_chunked"
            )
        return [[self.route(s, d) for d in destinations] for s in sources]

    def route(self, a: Coordinate, b: Coordinate) -> float | None:
        if self._key(*a) in self._unroutable or self._key(*b) in self._unroutable:
            return None
        return haversine_km(a[0], a[1], b[0], b[1]) * self.detour_factor


    @classmethod
    def _key(cls, lon: float, lat: float) -> tuple[float, float]:
        return (round(lon, cls._KEY_DECIMALS), round(lat, cls._KEY_DECIMALS))


def assert_finite_km(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value >= 0.0


__all__ = [
    "BackendInfo",
    "Coordinate",
    "FakeBackend",
    "RoutingBackend",
    "SnapResult",
    "assert_finite_km",
    "table_chunked",
]
