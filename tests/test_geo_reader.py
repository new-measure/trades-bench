from __future__ import annotations

import pytest

from trades_bench.errors import EngineError, ErrorCode
from trades_bench.geo_reader import _polygon_centroid, read_anchors, verify_artifact_sha256


def test_polygon_centroid_multipolygon_with_opposite_wound_parts():
    ccw_square = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
    cw_square = [[10, 0], [10, 2], [12, 2], [12, 0], [10, 0]]
    geometry = {"type": "MultiPolygon", "coordinates": [[ccw_square], [cw_square]]}
    lon, lat = _polygon_centroid(geometry)
    assert lon == pytest.approx(3.8)
    assert lat == pytest.approx(1.8)


def test_polygon_centroid_hole_wound_same_direction_as_shell_is_still_subtracted():
    cw_shell = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]
    cw_hole = [[2, 2], [2, 4], [4, 4], [4, 2], [2, 2]]
    geometry = {"type": "Polygon", "coordinates": [cw_shell, cw_hole]}
    lon, lat = _polygon_centroid(geometry)
    assert lon == pytest.approx(5.083333333333333)
    assert lat == pytest.approx(5.083333333333333)


def test_polygon_centroid_two_equal_area_opposite_wound_parts_does_not_divide_by_zero():
    part_a = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
    part_b = [[10, 0], [10, 4], [14, 4], [14, 0], [10, 0]]
    geometry = {"type": "MultiPolygon", "coordinates": [[part_a], [part_b]]}
    lon, lat = _polygon_centroid(geometry)
    assert lon == pytest.approx(7.0)
    assert lat == pytest.approx(2.0)


def test_polygon_centroid_degenerate_zero_area_geometry_raises_engine_error():
    collinear_ring = [[0, 0], [1, 0], [2, 0], [0, 0]]
    geometry = {"type": "Polygon", "coordinates": [collinear_ring]}
    with pytest.raises(EngineError) as ei:
        _polygon_centroid(geometry)
    assert ei.value.code is ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH


def test_verify_artifact_sha256_nonexistent_directory_raises_artifact_mismatch(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(EngineError) as ei:
        verify_artifact_sha256(missing, "a" * 64)
    assert ei.value.code is ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH


def test_verify_artifact_sha256_file_where_directory_expected_raises_artifact_mismatch(tmp_path):
    a_file = tmp_path / "not-a-directory"
    a_file.write_text("hello")
    with pytest.raises(EngineError) as ei:
        verify_artifact_sha256(a_file, "a" * 64)
    assert ei.value.code is ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH


def test_read_anchors_tolerates_column_aligned_padding_like_the_pinned_artifact(tmp_path):
    routing = tmp_path / "routing"
    routing.mkdir()
    (routing / "anchors.csv").write_text(
        "anchor_id, latitude , longitude  , selection_cell\n"
        "        1, 32.942027, -112.148575, r+0004c+0031\n"
        "        2, 33.394950, -111.938012, r+1902c+2226\n",
        encoding="utf-8",
    )
    anchors = read_anchors(tmp_path)
    assert [a.anchor_id for a in anchors] == [1, 2]
    assert anchors[0].latitude == 32.942027
    assert anchors[0].longitude == -112.148575
    assert anchors[0].selection_cell == "r+0004c+0031"
    assert anchors[1].selection_cell == "r+1902c+2226"
