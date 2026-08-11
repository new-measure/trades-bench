from __future__ import annotations

import array
import sys
from collections.abc import Sequence
from pathlib import Path

from .anchors import Anchor
from .errors import GeoBuildError
from .routing import Coordinate, RoutingBackend, assert_finite_km, table_chunked

MATRIX_SCALE = 100

MATRIX_TYPECODE = "I"

MATRIX_ITEM_BYTES = 4

MATRIX_BYTE_ORDER = "little"


def build_anchor_matrix(
    anchors: Sequence[Anchor], backend: RoutingBackend
) -> list[list[float | None]]:
    coordinates: list[Coordinate] = [a.coordinate() for a in anchors]
    return table_chunked(backend, coordinates, coordinates)


def largest_mutually_routable_set(raw: Sequence[Sequence[float | None]]) -> list[int]:
    n = len(raw)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if assert_finite_km(raw[i][j]) and assert_finite_km(raw[j][i]):
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    if not groups:
        return []
    best = max(groups.values(), key=lambda members: (len(members), -members[0]))
    return sorted(best)


def symmetrize(raw: Sequence[Sequence[float | None]]) -> list[list[float]]:
    n = len(raw)
    out = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            forward, backward = raw[i][j], raw[j][i]
            if not assert_finite_km(forward) or not assert_finite_km(backward):
                raise GeoBuildError(
                    f"anchor pair ({i + 1}, {j + 1}) has no finite routed distance in "
                    "both directions; the anchor set was not pruned to a mutually "
                    "routable component before symmetrization"
                )
            mean = (float(forward) + float(backward)) / 2.0
            out[i][j] = mean
            out[j][i] = mean
    return out


def quantize(km: float) -> int:
    if km < 0:
        raise ValueError(f"negative distance cannot be packed: {km}")
    return int(km * MATRIX_SCALE + 0.5)


def quantized(symmetric_km: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[quantize(v) / float(MATRIX_SCALE) for v in row] for row in symmetric_km]


def pack_matrix(symmetric_km: Sequence[Sequence[float]]) -> bytes:
    n = len(symmetric_km)
    values = array.array(MATRIX_TYPECODE)
    if values.itemsize != MATRIX_ITEM_BYTES:
        raise GeoBuildError(
            f"array('{MATRIX_TYPECODE}') is {values.itemsize} bytes on this platform, "
            f"not {MATRIX_ITEM_BYTES}; the packed matrix layout is not portable here"
        )
    for row in symmetric_km:
        if len(row) != n:
            raise GeoBuildError("anchor matrix is not square")
        values.extend(quantize(v) for v in row)
    if sys.byteorder != MATRIX_BYTE_ORDER:
        values.byteswap()
    return values.tobytes()


def write_packed_matrix(path: Path, symmetric_km: Sequence[Sequence[float]]) -> None:
    path.write_bytes(pack_matrix(symmetric_km))


def read_packed_matrix(path: Path, n: int) -> list[list[float]]:
    values = array.array(MATRIX_TYPECODE)
    with open(path, "rb") as f:
        values.fromfile(f, n * n)
    if sys.byteorder != MATRIX_BYTE_ORDER:
        values.byteswap()
    return [
        [values[i * n + j] / float(MATRIX_SCALE) for j in range(n)] for i in range(n)
    ]


__all__ = [
    "MATRIX_BYTE_ORDER",
    "MATRIX_ITEM_BYTES",
    "MATRIX_SCALE",
    "MATRIX_TYPECODE",
    "build_anchor_matrix",
    "largest_mutually_routable_set",
    "pack_matrix",
    "quantize",
    "quantized",
    "read_packed_matrix",
    "symmetrize",
    "write_packed_matrix",
]
