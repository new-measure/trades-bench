from __future__ import annotations

import json

import pytest

pytest.importorskip("shapely")

from geo_build.errors import GateFailure  # noqa: E402
from geo_build.gates import (  # noqa: E402
    SEGMENT_CLASSES,
    GateAddress,
    check_stratification,
    count_stratification,
    percentile,
    route_distance_km,
    run_accuracy_gate,
    sample_pairs,
)
from geo_build.routing import FakeBackend  # noqa: E402
from trades_bench.config import load_run_config  # noqa: E402

ZONE_IDS = list(range(1, 11))


def _config(**overrides):
    return load_run_config(scenario_json=json.dumps({"keys": overrides}))


def _row(zone_id, segments=("residential",), status="usable"):
    return {
        "zone_id": zone_id,
        "segment_eligibility": ",".join(segments),
        "status": status,
        "anchor_id": 1,
        "access_distance_km": "0.100",
    }


def _catalog(per_zone: int, outlying: int, segments=SEGMENT_CLASSES):
    rows = []
    for zone_id in ZONE_IDS:
        rows.extend(_row(zone_id, segments) for _ in range(per_zone))
    rows.extend(_row(None) for _ in range(outlying))
    return rows


def test_counts_partition_zones_and_the_outlying_ring():
    counts = count_stratification(_catalog(per_zone=2, outlying=3), ZONE_IDS)
    assert counts.total_usable == 23
    assert counts.outlying == 3
    assert counts.by_zone[1] == 2
    assert counts.by_zone_segment[(1, "commercial")] == 2
    assert sum(counts.by_zone.values()) + counts.outlying == counts.total_usable


def test_unusable_rows_are_not_counted_toward_any_floor():
    rows = _catalog(per_zone=1, outlying=1) + [_row(1, status="unusable")]
    counts = count_stratification(rows, ZONE_IDS)
    assert counts.by_zone[1] == 1
    assert counts.total_usable == 11


def test_all_floors_met_passes():
    result = check_stratification(
        _catalog(per_zone=5, outlying=4),
        ZONE_IDS,
        _config(
            **{
                "geography.min_address_catalog_size": 54,
                "geography.min_addresses_per_zone": 5,
                "geography.min_addresses_per_zone_segment": 5,
                "geography.min_outlying_addresses": 4,
            }
        ),
    )
    assert result.passed
    assert result.failures == ()


def test_total_catalog_floor_failure_names_the_key():
    with pytest.raises(GateFailure, match="geography.min_address_catalog_size"):
        check_stratification(
            _catalog(per_zone=1, outlying=1),
            ZONE_IDS,
            _config(
                **{
                    "geography.min_address_catalog_size": 10_000,
                    "geography.min_addresses_per_zone": 0,
                    "geography.min_addresses_per_zone_segment": 0,
                    "geography.min_outlying_addresses": 0,
                }
            ),
        )


def test_per_zone_floor_failure_names_every_short_zone():
    rows = [_row(1) for _ in range(5)]
    with pytest.raises(GateFailure) as excinfo:
        check_stratification(
            rows,
            ZONE_IDS,
            _config(
                **{
                    "geography.min_address_catalog_size": 0,
                    "geography.min_addresses_per_zone": 3,
                    "geography.min_addresses_per_zone_segment": 0,
                    "geography.min_outlying_addresses": 0,
                }
            ),
        )
    message = str(excinfo.value)
    for zone_id in range(2, 11):
        assert f"zone {zone_id} has 0 usable addresses" in message
    assert "zone 1 has" not in message


def test_per_zone_segment_floor_failure_names_the_segment():
    rows = _catalog(per_zone=4, outlying=1, segments=("residential",))
    with pytest.raises(GateFailure) as excinfo:
        check_stratification(
            rows,
            ZONE_IDS,
            _config(
                **{
                    "geography.min_address_catalog_size": 0,
                    "geography.min_addresses_per_zone": 0,
                    "geography.min_addresses_per_zone_segment": 2,
                    "geography.min_outlying_addresses": 0,
                }
            ),
        )
    message = str(excinfo.value)
    assert "segment 'commercial'" in message
    assert "segment 'apartment'" in message
    assert "segment 'residential'" not in message


def test_outlying_floor_failure_names_the_ring():
    with pytest.raises(GateFailure, match="outlying ring has 0 usable addresses"):
        check_stratification(
            _catalog(per_zone=3, outlying=0),
            ZONE_IDS,
            _config(
                **{
                    "geography.min_address_catalog_size": 0,
                    "geography.min_addresses_per_zone": 0,
                    "geography.min_addresses_per_zone_segment": 0,
                    "geography.min_outlying_addresses": 1,
                }
            ),
        )


def test_every_failed_floor_is_reported_in_one_message():
    with pytest.raises(GateFailure) as excinfo:
        check_stratification(
            [_row(1)],
            ZONE_IDS,
            _config(
                **{
                    "geography.min_address_catalog_size": 100,
                    "geography.min_addresses_per_zone": 5,
                    "geography.min_addresses_per_zone_segment": 2,
                    "geography.min_outlying_addresses": 3,
                }
            ),
        )
    message = str(excinfo.value)
    assert "geography.min_address_catalog_size" in message
    assert "geography.min_addresses_per_zone " in message
    assert "geography.min_addresses_per_zone_segment" in message
    assert "geography.min_outlying_addresses" in message


def test_route_distance_uses_the_matrix_for_different_anchors():
    matrix = [[0.0, 8.0], [8.0, 0.0]]
    assert route_distance_km(
        1.0, 1, 2.0, 2, matrix, min_route_distance_km=0.5, intra_cell_detour_factor=1.3
    ) == pytest.approx(11.0)


def test_route_distance_same_anchor_uses_the_detour_factor_and_the_floor():
    matrix = [[0.0]]
    assert route_distance_km(
        1.0, 1, 2.0, 1, matrix, min_route_distance_km=0.5, intra_cell_detour_factor=1.3
    ) == pytest.approx(3.9)
    assert route_distance_km(
        0.0, 1, 0.0, 1, matrix, min_route_distance_km=0.5, intra_cell_detour_factor=1.3
    ) == pytest.approx(0.5)


def test_route_distance_is_symmetric():
    matrix = [[0.0, 8.0], [8.0, 0.0]]
    forward = route_distance_km(
        1.0, 1, 2.0, 2, matrix, min_route_distance_km=0.5, intra_cell_detour_factor=1.3
    )
    backward = route_distance_km(
        2.0, 2, 1.0, 1, matrix, min_route_distance_km=0.5, intra_cell_detour_factor=1.3
    )
    assert forward == backward


def test_sample_pairs_are_deterministic_and_never_degenerate():
    first = sample_pairs(50, 100, "seed-a")
    assert first == sample_pairs(50, 100, "seed-a")
    assert first != sample_pairs(50, 100, "seed-b")
    assert len(first) == 100
    assert all(i != j for i, j in first)
    assert all(0 <= i < 50 and 0 <= j < 50 for i, j in first)


def test_sample_pairs_needs_at_least_two_addresses():
    assert sample_pairs(1, 10, "seed") == []
    assert sample_pairs(0, 10, "seed") == []


def test_percentile_is_nearest_rank():
    values = list(range(1, 21))
    assert percentile(values, 0.95) == 19
    assert percentile(values, 0.5) == 10
    assert percentile(values, 1.0) == 20
    assert percentile([], 0.95) == 0.0


def _addresses(n: int, *, anchor_id: int = 1, access_km: float = 0.2):
    return [
        GateAddress(
            lon=-112.0 + 0.02 * i, lat=33.5 + 0.01 * i, anchor_id=anchor_id, access_distance_km=access_km
        )
        for i in range(n)
    ]


def _gate_config(median_pct, p95_pct):
    return _config(
        **{
            "geography.distance_model_max_median_error_pct": median_pct,
            "geography.distance_model_max_p95_error_pct": p95_pct,
        }
    )


def test_accuracy_gate_passes_when_anchors_sit_on_the_addresses():
    backend = FakeBackend()
    addresses = [
        GateAddress(lon=a.lon, lat=a.lat, anchor_id=i + 1, access_distance_km=0.0)
        for i, a in enumerate(_addresses(6))
    ]
    matrix = [
        [
            round(backend.route((a.lon, a.lat), (b.lon, b.lat)) * 100) / 100
            for b in addresses
        ]
        for a in addresses
    ]
    result = run_accuracy_gate(
        addresses, matrix, backend, _gate_config(10.0, 25.0), sample_size=50, seed="s"
    )
    assert result.passed
    assert result.median_pct < 1.0
    assert result.evaluated_pairs == 50
    assert result.distribution and result.histogram


def test_accuracy_gate_fails_the_median_threshold():
    backend = FakeBackend()
    addresses = _addresses(6, anchor_id=1, access_km=5.0)
    with pytest.raises(GateFailure, match="median absolute error"):
        run_accuracy_gate(
            addresses, [[0.0]], backend, _gate_config(10.0, 10_000.0), sample_size=40, seed="s"
        )


def test_accuracy_gate_fails_the_p95_threshold_alone():
    backend = FakeBackend()
    addresses = _addresses(6, anchor_id=1, access_km=5.0)
    with pytest.raises(GateFailure) as excinfo:
        run_accuracy_gate(
            addresses, [[0.0]], backend, _gate_config(10_000.0, 10.0), sample_size=40, seed="s"
        )
    message = str(excinfo.value)
    assert "p95 absolute error" in message
    assert "median absolute error" not in message
    assert "never a looser gate" in message


def test_accuracy_gate_fails_when_every_pair_was_skipped():
    addresses = [
        GateAddress(lon=a.lon, lat=a.lat, anchor_id=i + 1, access_distance_km=0.0)
        for i, a in enumerate(_addresses(3))
    ]
    backend = FakeBackend(unroutable=[(a.lon, a.lat) for a in addresses])
    with pytest.raises(GateFailure) as excinfo:
        run_accuracy_gate(
            addresses, [[0.0]] * 3, backend, _gate_config(10.0, 25.0), sample_size=30, seed="s"
        )
    message = str(excinfo.value)
    assert "only 0 of 30 drawn pairs produced a measurement" in message
    assert "30 skipped" in message


def test_accuracy_gate_fails_just_below_the_required_fraction():
    addresses = [
        GateAddress(lon=a.lon, lat=a.lat, anchor_id=i + 1, access_distance_km=0.0)
        for i, a in enumerate(_addresses(4))
    ]
    matrix = [
        [round(FakeBackend().route((a.lon, a.lat), (b.lon, b.lat)) * 100) / 100 for b in addresses]
        for a in addresses
    ]
    backend = FakeBackend(unroutable=[(addresses[0].lon, addresses[0].lat)])
    with pytest.raises(GateFailure) as excinfo:
        run_accuracy_gate(
            addresses, matrix, backend, _gate_config(10.0, 25.0), sample_size=40, seed="s"
        )
    message = str(excinfo.value)
    assert "drawn pairs produced a measurement" in message
    assert "at least 36 are required" in message
    assert "min_evaluated_fraction 0.9" in message


def test_accuracy_gate_evaluates_normally_at_the_required_fraction():
    addresses = [
        GateAddress(lon=a.lon, lat=a.lat, anchor_id=i + 1, access_distance_km=0.0)
        for i, a in enumerate(_addresses(6))
    ]
    backend = FakeBackend()
    matrix = [
        [round(backend.route((a.lon, a.lat), (b.lon, b.lat)) * 100) / 100 for b in addresses]
        for a in addresses
    ]
    result = run_accuracy_gate(
        addresses,
        matrix,
        backend,
        _gate_config(10.0, 25.0),
        sample_size=40,
        seed="s",
        min_evaluated_fraction=1.0,
    )
    assert result.passed
    assert result.evaluated_pairs == 40
    assert result.min_evaluated_pairs == 40
    assert result.skipped_pairs == 0


def test_a_lowered_fraction_still_never_accepts_zero_measurements():
    addresses = [
        GateAddress(lon=a.lon, lat=a.lat, anchor_id=i + 1, access_distance_km=0.0)
        for i, a in enumerate(_addresses(2))
    ]
    backend = FakeBackend(unroutable=[(a.lon, a.lat) for a in addresses])
    with pytest.raises(GateFailure, match="only 0 of 10 drawn pairs"):
        run_accuracy_gate(
            addresses,
            [[0.0]] * 2,
            backend,
            _gate_config(10.0, 25.0),
            sample_size=10,
            seed="s",
            min_evaluated_fraction=0.0,
        )


def test_a_catalog_too_small_to_sample_cannot_pass_the_gate():
    addresses = [GateAddress(lon=-112.0, lat=33.5, anchor_id=1, access_distance_km=0.0)]
    with pytest.raises(GateFailure, match="only 0 of 20 drawn pairs"):
        run_accuracy_gate(
            addresses, [[0.0]], FakeBackend(), _gate_config(10.0, 25.0), sample_size=20, seed="s"
        )


def test_accuracy_gate_skips_unroutable_pairs_and_records_the_count():
    addresses = [
        GateAddress(lon=a.lon, lat=a.lat, anchor_id=i + 1, access_distance_km=0.0)
        for i, a in enumerate(_addresses(3))
    ]
    backend = FakeBackend(unroutable=[(addresses[0].lon, addresses[0].lat)])
    matrix = [
        [
            round(FakeBackend().route((a.lon, a.lat), (b.lon, b.lat)) * 100) / 100
            for b in addresses
        ]
        for a in addresses
    ]
    result = run_accuracy_gate(
        addresses,
        matrix,
        backend,
        _gate_config(10.0, 25.0),
        sample_size=30,
        seed="s",
        min_evaluated_fraction=0.1,
    )
    assert result.skipped_pairs > 0
    assert result.evaluated_pairs + result.skipped_pairs == 30


def test_gate_result_serializes_every_field_section_7_requires():
    backend = FakeBackend()
    addresses = [
        GateAddress(lon=a.lon, lat=a.lat, anchor_id=i + 1, access_distance_km=0.0)
        for i, a in enumerate(_addresses(4))
    ]
    matrix = [
        [round(backend.route((a.lon, a.lat), (b.lon, b.lat)) * 100) / 100 for b in addresses]
        for a in addresses
    ]
    payload = run_accuracy_gate(
        addresses, matrix, backend, _gate_config(10.0, 25.0), sample_size=20, seed="pinned"
    ).to_dict()
    assert payload["sample_size"] == 20
    assert payload["seed"] == "pinned"
    assert payload["pass"] is True
    assert set(payload["thresholds"]) == {"median_pct", "p95_pct"}
    assert "median_pct" in payload and "p95_pct" in payload
    assert payload["evaluated_pairs"] == 20
    assert payload["skipped_pairs"] == 0
    assert payload["min_evaluated_pairs"] == 18
    assert payload["min_evaluated_fraction"] == 0.9
