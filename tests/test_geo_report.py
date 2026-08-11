from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build.report import (  # noqa: E402
    ARIZONA_ZIP_MAX,
    ARIZONA_ZIP_MIN,
    anchor_statistics,
    run_sanity_checks,
)


@pytest.fixture
def build_area():
    from shapely.geometry import box

    return box(-112.0, 33.4, -111.7, 33.7)


def _row(lon, lat, *, postal="85251", street="Main St", status="usable", anchor=1, access="0.500"):
    return {
        "longitude": f"{lon:.6f}",
        "latitude": f"{lat:.6f}",
        "postal_code": postal,
        "street": street,
        "status": status,
        "anchor_id": anchor,
        "access_distance_km": access,
    }


def test_a_clean_catalog_passes_every_sanity_check(build_area):
    rows = [_row(-111.9, 33.5), _row(-111.8, 33.6, street="Elm St")]
    checks = run_sanity_checks(rows, build_area, dedupe_radius_meters=10.0)
    assert checks.passed
    assert checks.rows_checked == 2
    assert checks.to_dict()["pass"] is True


def test_a_point_outside_the_crop_is_caught(build_area):
    checks = run_sanity_checks([_row(-113.0, 33.5)], build_area, dedupe_radius_meters=10.0)
    assert checks.outside_crop == 1
    assert not checks.passed


def test_a_zip_outside_the_arizona_range_is_caught(build_area):
    rows = [_row(-111.9, 33.5, postal=str(ARIZONA_ZIP_MIN - 1))]
    assert run_sanity_checks(rows, build_area, dedupe_radius_meters=10.0).zip_out_of_range == 1
    rows = [_row(-111.9, 33.5, postal=str(ARIZONA_ZIP_MAX + 1))]
    assert run_sanity_checks(rows, build_area, dedupe_radius_meters=10.0).zip_out_of_range == 1
    rows = [_row(-111.9, 33.5, postal="not-a-zip")]
    assert run_sanity_checks(rows, build_area, dedupe_radius_meters=10.0).zip_out_of_range == 1


def test_near_coincident_same_street_rows_are_caught(build_area):
    rows = [_row(-111.900000, 33.500000), _row(-111.900010, 33.500000)]
    checks = run_sanity_checks(rows, build_area, dedupe_radius_meters=10.0)
    assert checks.duplicate_coordinate_pairs == 1
    assert not checks.passed


def test_two_rows_at_the_identical_coordinate_are_caught(build_area):
    rows = [_row(-111.900000, 33.500000), _row(-111.900000, 33.500000)]
    checks = run_sanity_checks(rows, build_area, dedupe_radius_meters=10.0)
    assert checks.duplicate_coordinate_pairs == 1
    assert not checks.passed


def test_three_rows_at_the_identical_coordinate_count_all_three_pairs(build_area):
    rows = [_row(-111.9, 33.5)] * 3
    checks = run_sanity_checks(rows, build_area, dedupe_radius_meters=10.0)
    assert checks.duplicate_coordinate_pairs == 3


def test_a_longitude_separated_pair_inside_the_radius_is_caught(build_area):
    import math

    from geo_build.geometry import KM_PER_DEG_LAT, haversine_km

    radius_m = 10.0
    radius_km = radius_m / 1000.0
    old_cell = radius_km / KM_PER_DEG_LAT

    separation = 1.1 * old_cell
    boundary = math.ceil(-111.9 / old_cell) * old_cell
    left = boundary - 0.01 * old_cell
    rows = [_row(left, 33.5), _row(left + separation, 33.5)]

    assert haversine_km(left, 33.5, left + separation, 33.5) < radius_km
    old_columns = (int(left // old_cell), int((left + separation) // old_cell))
    assert old_columns[1] - old_columns[0] == 2

    checks = run_sanity_checks(rows, build_area, dedupe_radius_meters=radius_m)
    assert checks.duplicate_coordinate_pairs == 1


def test_a_duplicate_pair_straddling_a_bucket_boundary_is_still_found(build_area):
    radius_m = 20.0
    from geo_build.geometry import KM_PER_DEG_LAT

    cell_deg = (radius_m / 1000.0) / KM_PER_DEG_LAT
    edge = 33.5 - (33.5 % cell_deg)
    rows = [_row(-111.9, edge - cell_deg / 4), _row(-111.9, edge + cell_deg / 4)]
    checks = run_sanity_checks(rows, build_area, dedupe_radius_meters=radius_m)
    assert checks.duplicate_coordinate_pairs == 1


def test_each_duplicate_pair_is_counted_once(build_area):
    rows = [_row(-111.900000, 33.500000), _row(-111.900010, 33.500000)]
    assert (
        run_sanity_checks(rows, build_area, dedupe_radius_meters=10.0).duplicate_coordinate_pairs
        == 1
    )


def test_the_duplicate_scan_is_scoped_to_one_street(build_area):
    rows = [_row(-111.9, 33.5, street="Main St"), _row(-111.9, 33.5, street="Elm St")]
    assert run_sanity_checks(rows, build_area, dedupe_radius_meters=10.0).passed


def test_anchor_statistics_summarize_the_access_distribution():
    rows = [
        _row(-111.9, 33.5, anchor=1, access="0.100"),
        _row(-111.8, 33.5, anchor=1, access="0.300"),
        _row(-111.7, 33.5, anchor=2, access="0.900"),
    ]
    stats = anchor_statistics(rows, anchor_count=4)
    assert stats.anchor_count == 4
    assert stats.assigned_addresses == 3
    assert stats.access_median_km == pytest.approx(0.300)
    assert stats.access_max_km == pytest.approx(0.900)
    assert stats.addresses_per_anchor_max == 2
    assert stats.anchors_with_no_address == 2


def test_unusable_rows_are_excluded_from_the_anchor_statistics():
    rows = [
        _row(-111.9, 33.5, anchor=1, access="0.100"),
        _row(-111.8, 33.5, status="unusable", anchor=None, access=None),
    ]
    stats = anchor_statistics(rows, anchor_count=1)
    assert stats.assigned_addresses == 1
    assert stats.addresses_per_anchor_max == 1
