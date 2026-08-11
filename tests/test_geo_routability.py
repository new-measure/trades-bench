from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build.models import Candidate  # noqa: E402
from geo_build.routability import (  # noqa: E402
    REASON_UNROUTABLE,
    REASON_UNSNAPPABLE,
    STATUS_UNUSABLE,
    STATUS_USABLE,
    mark_unroutable,
    snap_candidates,
    usable,
)
from geo_build.routing import FakeBackend, SnapResult  # noqa: E402
from geo_build.strip import PENDING_STATUS  # noqa: E402


def _candidate(lon=-111.79, lat=33.55) -> Candidate:
    return Candidate(
        source_type="node",
        source_id=1,
        longitude=lon,
        latitude=lat,
        house_number_raw="100",
        street_raw="Main St",
        postal_code_raw="85251",
        city="Scottsdale",
        city_source="tag",
        state="AZ",
        state_source="crop",
    )


class _FixedSnapBackend(FakeBackend):

    def __init__(self, meters: float) -> None:
        super().__init__()
        self._meters = meters

    def snap(self, lon: float, lat: float) -> SnapResult:
        return SnapResult(lon=lon + 0.0001, lat=lat, distance_meters=self._meters)


def test_a_fresh_candidate_is_pending_until_the_stage_runs():
    assert _candidate().status == PENDING_STATUS


def test_snap_within_tolerance_marks_usable_and_records_the_snapped_point():
    candidate = _candidate()
    snap_candidates([candidate], FakeBackend(), max_snap_distance_meters=150)
    assert candidate.status == STATUS_USABLE
    assert candidate.unusable_reason is None
    assert candidate.snapped_lon is not None and candidate.snapped_lat is not None
    assert candidate.snap_distance_meters is not None
    assert (candidate.longitude, candidate.latitude) == (-111.79, 33.55)


def test_the_tolerance_boundary_is_inclusive():
    at_limit = _candidate()
    snap_candidates([at_limit], _FixedSnapBackend(150.0), max_snap_distance_meters=150)
    assert at_limit.status == STATUS_USABLE

    just_over = _candidate()
    snap_candidates([just_over], _FixedSnapBackend(150.0001), max_snap_distance_meters=150)
    assert just_over.status == STATUS_UNUSABLE
    assert just_over.unusable_reason == REASON_UNSNAPPABLE


def test_a_point_the_network_cannot_snap_at_all_is_unsnappable():
    candidate = _candidate()
    snap_candidates(
        [candidate],
        FakeBackend(unsnappable=[(-111.79, 33.55)]),
        max_snap_distance_meters=150,
    )
    assert candidate.status == STATUS_UNUSABLE
    assert candidate.unusable_reason == REASON_UNSNAPPABLE
    assert candidate.snap_distance_meters is None


def test_mark_unroutable_clears_the_routing_columns():
    candidate = _candidate()
    snap_candidates([candidate], FakeBackend(), max_snap_distance_meters=150)
    candidate.anchor_id = 4
    candidate.access_distance_km = 1.25
    mark_unroutable(candidate)
    assert candidate.status == STATUS_UNUSABLE
    assert candidate.unusable_reason == REASON_UNROUTABLE
    assert candidate.anchor_id is None
    assert candidate.access_distance_km is None


def test_usable_filters_to_the_snapped_survivors():
    good, bad = _candidate(), _candidate(lon=-111.70)
    snap_candidates(
        [good, bad],
        FakeBackend(unsnappable=[(-111.70, 33.55)]),
        max_snap_distance_meters=150,
    )
    assert usable([good, bad]) == [good]
