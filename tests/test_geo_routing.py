from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build.geometry import haversine_km  # noqa: E402
from geo_build.routing import (  # noqa: E402
    BackendInfo,
    FakeBackend,
    RoutingBackend,
    SnapResult,
    assert_finite_km,
    table_chunked,
)

A = (-111.79, 33.55)
B = (-111.77, 33.57)


def test_fake_backend_satisfies_the_protocol():
    assert isinstance(FakeBackend(), RoutingBackend)


def test_fake_backend_is_deterministic_across_instances():
    one, two = FakeBackend(), FakeBackend()
    assert one.snap(*A) == two.snap(*A)
    assert one.route(A, B) == two.route(A, B)
    assert one.table([A], [B]) == two.table([A], [B])


def test_distances_are_straight_line_times_the_detour_factor():
    backend = FakeBackend(detour_factor=1.4)
    expected = haversine_km(A[0], A[1], B[0], B[1]) * 1.4
    assert backend.route(A, B) == pytest.approx(expected)
    assert backend.route(A, B) == pytest.approx(backend.route(B, A))
    via = (-111.78, 33.60)
    assert backend.route(A, via) + backend.route(via, B) >= backend.route(A, B)


def test_snap_quantizes_to_the_lattice_with_a_real_distance():
    backend = FakeBackend(lattice_degrees=0.01)
    snapped = backend.snap(-111.7938, 33.5541)
    assert isinstance(snapped, SnapResult)
    assert snapped.lon == pytest.approx(-111.79)
    assert snapped.lat == pytest.approx(33.55)
    assert 0 < snapped.distance_meters < 1000


def test_forced_unsnappable_and_unroutable_points():
    backend = FakeBackend(unsnappable=[A], unroutable=[B])
    assert backend.snap(*A) is None
    assert backend.snap(*B) is not None
    assert backend.route(A, B) is None
    assert backend.route(B, A) is None
    assert backend.route(A, (-111.80, 33.60)) is not None


def test_backend_info_marks_the_fake_non_packageable():
    info = FakeBackend().info
    assert isinstance(info, BackendInfo)
    assert info.packageable is False
    assert info.image_digest is None


def test_assert_finite_km_rejects_none_and_infinity():
    assert assert_finite_km(1.5)
    assert assert_finite_km(0.0)
    assert not assert_finite_km(None)
    assert not assert_finite_km(float("inf"))
    assert not assert_finite_km(float("nan"))
    assert not assert_finite_km(-1.0)


def test_table_chunked_matches_one_big_call_regardless_of_the_limit():
    points = [(-111.80 + 0.01 * i, 33.55 + 0.005 * i) for i in range(9)]
    reference = FakeBackend(max_table_coordinates=1000).table(points, points)
    for limit in (4, 6, 10, 1000):
        chunked = table_chunked(FakeBackend(max_table_coordinates=limit), points, points)
        assert chunked == reference, f"limit={limit}"


def test_table_over_the_limit_without_chunking_is_a_loud_failure():
    backend = FakeBackend(max_table_coordinates=4)
    points = [(-111.80 + 0.01 * i, 33.55) for i in range(5)]
    with pytest.raises(ValueError, match="table_chunked"):
        backend.table(points, points)


def test_table_chunked_preserves_none_entries():
    backend = FakeBackend(max_table_coordinates=4, unroutable=[B])
    result = table_chunked(backend, [A, B], [A, B])
    assert result[0][0] is not None
    assert result[0][1] is None
    assert result[1][0] is None
    assert result[1][1] is None
