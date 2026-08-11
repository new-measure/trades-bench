from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build.anchors import (  # noqa: E402
    AccessAssignment,
    Anchor,
    AnchorGrid,
    assign_access_distances,
    renumber,
    select_anchors,
)
from geo_build.routing import FakeBackend  # noqa: E402

BOUNDS = (-112.4, 33.3, -111.7, 33.7)


def _grid(cell_km: float = 5.0) -> AnchorGrid:
    return AnchorGrid.over(BOUNDS, cell_km)


def test_grid_cells_are_square_in_kilometres_not_degrees():
    grid = _grid(5.0)
    from geo_build.geometry import haversine_km

    origin = grid.cell_centroid(0, 0)
    east = grid.cell_centroid(0, 1)
    north = grid.cell_centroid(1, 0)
    assert haversine_km(*origin, *east) == pytest.approx(5.0, rel=0.02)
    assert haversine_km(*origin, *north) == pytest.approx(5.0, rel=0.02)


def test_cell_of_and_cell_centroid_are_inverse_on_the_cell():
    grid = _grid(5.0)
    for row, col in ((0, 0), (3, 7), (11, 2)):
        assert grid.cell_of(*grid.cell_centroid(row, col)) == (row, col)


def test_grid_origin_depends_only_on_the_build_area():
    assert _grid(5.0) == AnchorGrid.over(BOUNDS, 5.0)
    assert _grid(5.0).origin_lon == BOUNDS[0]
    assert _grid(5.0).origin_lat == BOUNDS[1]


def test_zero_or_negative_cell_size_is_refused():
    with pytest.raises(ValueError, match="positive"):
        AnchorGrid.over(BOUNDS, 0.0)


def test_selection_is_deterministic_and_ordered_by_cell():
    grid = _grid(5.0)
    backend = FakeBackend()
    points = [(-112.30, 33.35), (-111.80, 33.65), (-112.00, 33.50)]
    first = select_anchors(grid, points, backend)
    second = select_anchors(grid, list(reversed(points)), backend)
    assert [a.anchor_id for a in first] == [1, 2, 3]
    assert first == second
    assert [a.selection_cell for a in first] == sorted(a.selection_cell for a in first)


def test_an_anchor_is_the_cell_centroid_snapped_never_an_address():
    grid = _grid(5.0)
    backend = FakeBackend(lattice_degrees=0.0005)
    address = (-112.00, 33.50)
    anchors = select_anchors(grid, [address], backend)
    assert len(anchors) == 1
    assert (anchors[0].lon, anchors[0].lat) != address
    row, col = grid.cell_of(*address)
    expected = backend.snap(*grid.cell_centroid(row, col))
    assert anchors[0].lon == pytest.approx(expected.lon)
    assert anchors[0].lat == pytest.approx(expected.lat)


def test_two_cells_snapping_to_the_same_point_collapse_to_one_anchor():
    grid = _grid(1.0)
    backend = FakeBackend(lattice_degrees=0.5)
    points = [(-112.00, 33.50), (-111.995, 33.50), (-111.99, 33.50)]
    anchors = select_anchors(grid, points, backend)
    assert len(anchors) == 1
    assert anchors[0].anchor_id == 1


def test_a_cell_whose_centroid_cannot_snap_contributes_no_anchor():
    grid = _grid(5.0)
    address = (-112.00, 33.50)
    row, col = grid.cell_of(*address)
    backend = FakeBackend(unsnappable=[grid.cell_centroid(row, col)])
    assert select_anchors(grid, [address], backend) == []


def test_renumber_keeps_order_and_reissues_contiguous_ids():
    anchors = [Anchor(i + 1, -112.0 + i * 0.01, 33.5, f"c{i}") for i in range(5)]
    kept = renumber(anchors, [0, 2, 4])
    assert [a.anchor_id for a in kept] == [1, 2, 3]
    assert [a.selection_cell for a in kept] == ["c0", "c2", "c4"]


def test_access_assignment_picks_the_nearest_anchor_by_routed_distance():
    grid = _grid(5.0)
    backend = FakeBackend()
    points = [(-112.30, 33.35), (-111.80, 33.65)]
    anchors = select_anchors(grid, points, backend)
    assignments = assign_access_distances(points, anchors, backend, grid=grid, k=8)
    assert len(assignments) == 2
    for point, assignment in zip(points, assignments, strict=True):
        assert assignment.anchor_id is not None
        best = min(
            anchors,
            key=lambda a: backend.route(point, (a.lon, a.lat)),
        )
        assert assignment.anchor_id == best.anchor_id
        assert assignment.access_distance_km == pytest.approx(
            backend.route(point, (best.lon, best.lat)), abs=1e-3
        )


def _exhaustive_nearest(points, anchors, backend):
    results = []
    for point in points:
        best_id, best_km = None, None
        for anchor in sorted(anchors, key=lambda a: a.anchor_id):
            km = backend.route(point, (anchor.lon, anchor.lat))
            if km is not None and (best_km is None or km < best_km):
                best_id, best_km = anchor.anchor_id, km
        results.append((best_id, None if best_km is None else round(best_km, 3)))
    return results


def _search_fixture():
    grid = _grid(1.0)
    backend = FakeBackend(lattice_degrees=0.02)
    points = [(-112.30 + 0.008 * i, 33.35 + 0.006 * j) for i in range(8) for j in range(8)]
    anchors = select_anchors(grid, points, backend)
    return grid, backend, points, anchors


def test_bounded_candidate_search_finds_the_true_nearest_anchor():
    grid, backend, points, anchors = _search_fixture()
    k = 4
    assert len(anchors) > k * 2, f"fixture must have many more anchors than k, got {len(anchors)}"

    expected = _exhaustive_nearest(points, anchors, backend)
    assert len({anchor_id for anchor_id, _ in expected}) > 1
    own_cell_nearest = sum(
        1
        for point, (anchor_id, _) in zip(points, expected, strict=True)
        if any(
            a.anchor_id == anchor_id and a.selection_cell == grid.cell_label(*grid.cell_of(*point))
            for a in anchors
        )
    )
    assert own_cell_nearest < len(points) * 0.75

    bounded = assign_access_distances(points, anchors, backend, grid=grid, k=k)
    assert [(a.anchor_id, a.access_distance_km) for a in bounded] == expected


def test_the_ring_search_alone_finds_the_nearest_anchor_at_k_equals_one():
    grid, backend, points, anchors = _search_fixture()
    expected = _exhaustive_nearest(points, anchors, backend)
    bounded = assign_access_distances(points, anchors, backend, grid=grid, k=1)
    assert [(a.anchor_id, a.access_distance_km) for a in bounded] == expected


class _TollBackend(FakeBackend):

    def __init__(self, expensive):
        super().__init__()
        self._expensive = {self._key(*c) for c in expensive}

    def route(self, a, b):
        base = super().route(a, b)
        if base is None:
            return None
        return base * (10.0 if self._key(*b) in self._expensive else 1.0)


def test_the_exhaustive_baseline_can_actually_disagree_with_the_bounded_search():
    grid, _, points, anchors = _search_fixture()
    backend = _TollBackend([(a.lon, a.lat) for a in anchors[::2]])

    expected = _exhaustive_nearest(points, anchors, backend)
    degenerate = assign_access_distances(points, anchors, backend, grid=grid, k=1)
    assert [(a.anchor_id, a.access_distance_km) for a in degenerate] != expected

    wide = assign_access_distances(points, anchors, backend, grid=grid, k=len(anchors))
    assert [(a.anchor_id, a.access_distance_km) for a in wide] == expected


def test_access_assignment_batching_does_not_change_the_answer():
    grid = _grid(2.0)
    points = [(-112.30 + 0.05 * i, 33.35 + 0.03 * i) for i in range(8)]
    anchors = select_anchors(grid, points, FakeBackend())
    one_call = assign_access_distances(
        points, anchors, FakeBackend(max_table_coordinates=10_000), grid=grid, k=4
    )
    many_calls = assign_access_distances(
        points, anchors, FakeBackend(max_table_coordinates=4), grid=grid, k=4
    )
    assert one_call == many_calls


def test_an_address_no_anchor_can_reach_is_unassigned():
    grid = _grid(5.0)
    point = (-112.00, 33.50)
    backend = FakeBackend(unroutable=[point])
    anchors = select_anchors(grid, [point], FakeBackend())
    assert assign_access_distances([point], anchors, backend, grid=grid, k=8) == [
        AccessAssignment(None, None)
    ]


def test_no_anchors_at_all_leaves_every_address_unassigned():
    grid = _grid(5.0)
    assert assign_access_distances([(-112.0, 33.5)], [], FakeBackend(), grid=grid, k=8) == [
        AccessAssignment(None, None)
    ]


def test_candidate_k_must_be_positive():
    grid = _grid(5.0)
    anchors = select_anchors(grid, [(-112.0, 33.5)], FakeBackend())
    with pytest.raises(ValueError, match="positive"):
        assign_access_distances([(-112.0, 33.5)], anchors, FakeBackend(), grid=grid, k=0)


def test_selection_procedure_is_recorded_for_the_manifest():
    described = _grid(6.0).describe()
    assert described["kind"] == "fixed_grid_cell_centroid_snap"
    assert described["cell_km"] == 6.0
    for key in ("origin_lon", "origin_lat", "occupied_cell_rule", "anchor_rule", "ordering"):
        assert key in described
