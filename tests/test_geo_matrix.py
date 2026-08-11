from __future__ import annotations

import array
import struct
import sys

import pytest

pytest.importorskip("shapely")

from geo_build.anchors import Anchor, AnchorGrid, select_anchors  # noqa: E402
from geo_build.errors import GeoBuildError  # noqa: E402
from geo_build.matrix import (  # noqa: E402
    MATRIX_ITEM_BYTES,
    MATRIX_SCALE,
    build_anchor_matrix,
    largest_mutually_routable_set,
    pack_matrix,
    quantize,
    quantized,
    read_packed_matrix,
    symmetrize,
    write_packed_matrix,
)
from geo_build.routing import FakeBackend  # noqa: E402


def _anchors(n: int) -> list[Anchor]:
    return [Anchor(i + 1, -112.0 + 0.05 * i, 33.5 + 0.02 * i, f"c{i}") for i in range(n)]


def test_build_anchor_matrix_is_square_with_a_zero_diagonal():
    anchors = _anchors(4)
    raw = build_anchor_matrix(anchors, FakeBackend())
    assert len(raw) == 4
    assert all(len(row) == 4 for row in raw)
    assert all(raw[i][i] == 0.0 for i in range(4))


def test_symmetrize_averages_both_directions_exactly():
    raw = [[0.0, 10.0], [12.0, 0.0]]
    sym = symmetrize(raw)
    assert sym == [[0.0, 11.0], [11.0, 0.0]]


def test_symmetrize_is_exactly_symmetric_not_approximately():
    raw = [[0.0, 0.1, 0.2], [0.3, 0.0, 0.7], [0.9, 1.1, 0.0]]
    sym = symmetrize(raw)
    for i in range(3):
        for j in range(3):
            assert sym[i][j] == sym[j][i]


def test_symmetrize_refuses_an_unrouted_pair():
    with pytest.raises(GeoBuildError, match="no finite routed distance"):
        symmetrize([[0.0, None], [5.0, 0.0]])


def test_pruning_keeps_the_largest_mutually_routable_group():
    n = 4
    raw = [[0.0 if i == j else 1.0 for j in range(n)] for i in range(n)]
    for i in range(3):
        raw[i][3] = None
        raw[3][i] = None
    assert largest_mutually_routable_set(raw) == [0, 1, 2]


def test_pruning_breaks_a_tie_toward_the_lowest_index():
    n = 4
    raw = [[0.0 if i == j else None for j in range(n)] for i in range(n)]
    raw[0][1] = raw[1][0] = 2.0
    raw[2][3] = raw[3][2] = 2.0
    assert largest_mutually_routable_set(raw) == [0, 1]


def test_pruning_needs_both_directions_finite():
    raw = [[0.0, 1.0], [None, 0.0]]
    assert largest_mutually_routable_set(raw) == [0]


def test_pruning_a_healthy_matrix_keeps_everything():
    raw = build_anchor_matrix(_anchors(5), FakeBackend())
    assert largest_mutually_routable_set(raw) == [0, 1, 2, 3, 4]


def test_quantize_is_half_up_not_bankers_rounding():
    assert quantize(0.005) == 1
    assert quantize(0.015) == 2
    assert quantize(0.025) == 3
    assert quantize(0.0) == 0
    with pytest.raises(ValueError, match="negative"):
        quantize(-0.1)


def test_quantized_view_matches_what_the_packed_file_reads_back(tmp_path):
    sym = symmetrize(build_anchor_matrix(_anchors(6), FakeBackend()))
    path = tmp_path / "anchor_distance.bin"
    write_packed_matrix(path, sym)
    assert read_packed_matrix(path, 6) == quantized(sym)


def test_packed_round_trip_through_the_module_reader(tmp_path):
    sym = symmetrize(build_anchor_matrix(_anchors(7), FakeBackend()))
    path = tmp_path / "anchor_distance.bin"
    write_packed_matrix(path, sym)
    read_back = read_packed_matrix(path, 7)
    for i in range(7):
        for j in range(7):
            assert read_back[i][j] == pytest.approx(sym[i][j], abs=1.0 / MATRIX_SCALE)
            assert read_back[i][j] == read_back[j][i]


def test_packed_round_trip_through_a_bare_stdlib_reader(tmp_path):
    grid = AnchorGrid.over((-112.4, 33.3, -111.7, 33.7), 8.0)
    backend = FakeBackend()
    points = [(-112.3 + 0.1 * i, 33.35 + 0.04 * i) for i in range(6)]
    anchors = select_anchors(grid, points, backend)
    n = len(anchors)
    sym = symmetrize(build_anchor_matrix(anchors, backend))
    path = tmp_path / "anchor_distance.bin"
    write_packed_matrix(path, sym)

    assert path.stat().st_size == MATRIX_ITEM_BYTES * n * n

    values = array.array("I")
    with open(path, "rb") as f:
        values.fromfile(f, n * n)
    if sys.byteorder == "big":
        values.byteswap()
    for i in range(n):
        for j in range(n):
            assert values[i * n + j] / 100.0 == pytest.approx(sym[i][j], abs=0.01)
            assert values[i * n + j] == values[j * n + i]
    assert all(values[i * n + i] == 0 for i in range(n))

    raw = path.read_bytes()
    assert struct.unpack_from("<I", raw, 0)[0] == 0
    assert struct.unpack_from("<I", raw, MATRIX_ITEM_BYTES)[0] == values[1]


def test_packed_file_is_little_endian_on_every_host():
    packed = pack_matrix([[0.0, 1.23], [1.23, 0.0]])
    assert packed[:4] == b"\x00\x00\x00\x00"
    assert struct.unpack_from("<I", packed, 4)[0] == 123


def test_pack_refuses_a_non_square_matrix():
    with pytest.raises(GeoBuildError, match="square"):
        pack_matrix([[0.0, 1.0], [1.0]])


def test_empty_matrix_packs_to_an_empty_file(tmp_path):
    path = tmp_path / "anchor_distance.bin"
    write_packed_matrix(path, [])
    assert path.read_bytes() == b""
    assert read_packed_matrix(path, 0) == []
