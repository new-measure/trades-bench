from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from shapely.geometry import Polygon  # noqa: E402

from geo_build.zone_assign import assign_zone  # noqa: E402


def _squares():
    return [
        Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
        Polygon([(2, 0), (3, 0), (3, 1), (2, 1)]),
        Polygon([(4, 0), (5, 0), (5, 1), (4, 1)]),
    ]


def test_containing_point_gets_matching_zone_id_and_matching_effective_zone_id():
    zone_id, effective_zone_id = assign_zone(0.5, 0.5, _squares())
    assert zone_id == 1
    assert effective_zone_id == 1


def test_containing_point_in_third_zone():
    zone_id, effective_zone_id = assign_zone(4.5, 0.5, _squares())
    assert zone_id == 3
    assert effective_zone_id == 3


def test_outlying_point_gets_null_zone_id_and_nearest_effective_zone_id():
    zone_id, effective_zone_id = assign_zone(1.2, 0.5, _squares())
    assert zone_id is None
    assert effective_zone_id == 1


def test_outlying_point_nearest_zone_tie_break_prefers_lowest_zone_id():
    zone_id, effective_zone_id = assign_zone(1.5, 0.5, _squares())
    assert zone_id is None
    assert effective_zone_id == 1


def test_far_outlying_point_still_resolves_to_some_zone():
    zone_id, effective_zone_id = assign_zone(100.0, 100.0, _squares())
    assert zone_id is None
    assert effective_zone_id in {1, 2, 3}
