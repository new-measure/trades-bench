from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from csr_bench.config import RunConfig

from .errors import GateFailure
from .routing import RoutingBackend, assert_finite_km

SEGMENT_CLASSES: tuple[str, ...] = ("apartment", "commercial", "residential")


@dataclass(frozen=True)
class StratificationCounts:

    total_usable: int
    outlying: int
    by_zone: dict[int, int]
    by_zone_segment: dict[tuple[int, str], int]

    def to_dict(self) -> dict[str, object]:
        return {
            "total_usable": self.total_usable,
            "outlying": self.outlying,
            "by_zone": {str(z): c for z, c in sorted(self.by_zone.items())},
            "by_zone_segment": {
                f"{z}:{s}": c for (z, s), c in sorted(self.by_zone_segment.items())
            },
        }


@dataclass(frozen=True)
class StratificationResult:
    counts: StratificationCounts
    floors: dict[str, int]
    failures: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, object]:
        return {
            "counts": self.counts.to_dict(),
            "floors": dict(sorted(self.floors.items())),
            "failures": list(self.failures),
            "pass": self.passed,
        }


def count_stratification(
    catalog_rows: Sequence[dict[str, object]], zone_ids: Sequence[int]
) -> StratificationCounts:
    total = 0
    outlying = 0
    by_zone = dict.fromkeys(zone_ids, 0)
    by_zone_segment = {(z, s): 0 for z in zone_ids for s in SEGMENT_CLASSES}
    for row in catalog_rows:
        if row.get("status") != "usable":
            continue
        total += 1
        zone = row.get("zone_id")
        if zone is None or zone == "":
            outlying += 1
            continue
        zone_id = int(zone)
        by_zone[zone_id] = by_zone.get(zone_id, 0) + 1
        segments = str(row.get("segment_eligibility") or "")
        for segment in segments.split(","):
            if segment:
                key = (zone_id, segment)
                by_zone_segment[key] = by_zone_segment.get(key, 0) + 1
    return StratificationCounts(
        total_usable=total,
        outlying=outlying,
        by_zone=by_zone,
        by_zone_segment=by_zone_segment,
    )


def check_stratification(
    catalog_rows: Sequence[dict[str, object]],
    zone_ids: Sequence[int],
    config: RunConfig,
) -> StratificationResult:
    floors = {
        "min_address_catalog_size": config.get_int("geography.min_address_catalog_size"),
        "min_addresses_per_zone": config.get_int("geography.min_addresses_per_zone"),
        "min_addresses_per_zone_segment": config.get_int(
            "geography.min_addresses_per_zone_segment"
        ),
        "min_outlying_addresses": config.get_int("geography.min_outlying_addresses"),
    }
    counts = count_stratification(catalog_rows, zone_ids)

    failures: list[str] = []
    if counts.total_usable < floors["min_address_catalog_size"]:
        failures.append(
            f"total usable addresses {counts.total_usable} < "
            f"geography.min_address_catalog_size {floors['min_address_catalog_size']}"
        )
    for zone_id in sorted(zone_ids):
        have = counts.by_zone.get(zone_id, 0)
        if have < floors["min_addresses_per_zone"]:
            failures.append(
                f"zone {zone_id} has {have} usable addresses < "
                f"geography.min_addresses_per_zone {floors['min_addresses_per_zone']}"
            )
    for zone_id in sorted(zone_ids):
        for segment in SEGMENT_CLASSES:
            have = counts.by_zone_segment.get((zone_id, segment), 0)
            if have < floors["min_addresses_per_zone_segment"]:
                failures.append(
                    f"zone {zone_id} segment {segment!r} has {have} usable addresses < "
                    "geography.min_addresses_per_zone_segment "
                    f"{floors['min_addresses_per_zone_segment']}"
                )
    if counts.outlying < floors["min_outlying_addresses"]:
        failures.append(
            f"outlying ring has {counts.outlying} usable addresses < "
            f"geography.min_outlying_addresses {floors['min_outlying_addresses']}"
        )

    result = StratificationResult(counts=counts, floors=floors, failures=tuple(failures))
    if failures:
        raise GateFailure(
            "stratification gate failed (research/osm-data.md §4.9, 03 §3.2); "
            f"{len(failures)} floor(s) unmet:\n  - " + "\n  - ".join(failures)
        )
    return result


def route_distance_km(
    access_a_km: float,
    anchor_a: int,
    access_b_km: float,
    anchor_b: int,
    anchor_distance_km: Sequence[Sequence[float]],
    *,
    min_route_distance_km: float,
    intra_cell_detour_factor: float,
) -> float:
    if anchor_a == anchor_b:
        return max(
            min_route_distance_km,
            (access_a_km + access_b_km) * intra_cell_detour_factor,
        )
    return access_a_km + anchor_distance_km[anchor_a - 1][anchor_b - 1] + access_b_km


DEFAULT_ACCURACY_SAMPLE_SIZE = 500

DEFAULT_ACCURACY_SEED = "csr-003c/accuracy-gate/v1"

DEFAULT_ACCURACY_MIN_EVALUATED_FRACTION = 0.9


@dataclass(frozen=True)
class AccuracyGateResult:
    sample_size: int
    seed: str
    median_pct: float
    p95_pct: float
    thresholds: dict[str, float]
    passed: bool
    distribution: dict[str, float] = field(default_factory=dict)
    histogram: dict[str, int] = field(default_factory=dict)
    evaluated_pairs: int = 0
    skipped_pairs: int = 0
    min_evaluated_fraction: float = DEFAULT_ACCURACY_MIN_EVALUATED_FRACTION
    min_evaluated_pairs: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_size": self.sample_size,
            "seed": self.seed,
            "median_pct": self.median_pct,
            "p95_pct": self.p95_pct,
            "thresholds": dict(sorted(self.thresholds.items())),
            "pass": self.passed,
            "distribution": dict(sorted(self.distribution.items())),
            "histogram": dict(sorted(self.histogram.items())),
            "evaluated_pairs": self.evaluated_pairs,
            "skipped_pairs": self.skipped_pairs,
            "min_evaluated_fraction": self.min_evaluated_fraction,
            "min_evaluated_pairs": self.min_evaluated_pairs,
        }


def sample_pairs(population: int, sample_size: int, seed: str) -> list[tuple[int, int]]:
    if population < 2:
        return []
    pairs: list[tuple[int, int]] = []
    for index in range(sample_size):
        first = _draw_index(seed, index, "a", 0, population)
        second = _draw_index(seed, index, "b", 0, population)
        salt = 1
        while second == first:
            second = _draw_index(seed, index, "b", salt, population)
            salt += 1
        pairs.append((first, second))
    return pairs


def _draw_index(seed: str, index: int, side: str, salt: int, population: int) -> int:
    digest = hashlib.sha256(f"{seed}|{index}|{side}|{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % population


def percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    thousandths = int(round(fraction * 1000))
    rank = -(-(thousandths * n) // 1000)
    return float(sorted_values[max(1, min(n, rank)) - 1])


def _median(sorted_values: Sequence[float]) -> float:
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return float(sorted_values[n // 2])
    return (float(sorted_values[n // 2 - 1]) + float(sorted_values[n // 2])) / 2.0


@dataclass(frozen=True)
class GateAddress:

    lon: float
    lat: float
    anchor_id: int
    access_distance_km: float


def run_accuracy_gate(
    addresses: Sequence[GateAddress],
    anchor_distance_km: Sequence[Sequence[float]],
    backend: RoutingBackend,
    config: RunConfig,
    *,
    sample_size: int = DEFAULT_ACCURACY_SAMPLE_SIZE,
    seed: str = DEFAULT_ACCURACY_SEED,
    min_evaluated_fraction: float = DEFAULT_ACCURACY_MIN_EVALUATED_FRACTION,
) -> AccuracyGateResult:
    thresholds = {
        "median_pct": float(config.get_decimal("geography.distance_model_max_median_error_pct")),
        "p95_pct": float(config.get_decimal("geography.distance_model_max_p95_error_pct")),
    }
    min_route_distance_km = float(config.get_decimal("geography.min_route_distance_km"))
    intra_cell_detour_factor = float(config.get_decimal("geography.intra_cell_detour_factor"))

    errors: list[float] = []
    skipped = 0
    for i, j in sample_pairs(len(addresses), sample_size, seed):
        a, b = addresses[i], addresses[j]
        exact = backend.route((a.lon, a.lat), (b.lon, b.lat))
        if not assert_finite_km(exact) or exact == 0.0:
            skipped += 1
            continue
        approximate = route_distance_km(
            a.access_distance_km,
            a.anchor_id,
            b.access_distance_km,
            b.anchor_id,
            anchor_distance_km,
            min_route_distance_km=min_route_distance_km,
            intra_cell_detour_factor=intra_cell_detour_factor,
        )
        errors.append(abs(approximate - float(exact)) / float(exact) * 100.0)

    ordered = sorted(errors)
    median_pct = _median(ordered)
    p95_pct = percentile(ordered, 0.95)
    minimum_evaluated = _minimum_evaluated(sample_size, min_evaluated_fraction)

    failures: list[str] = []
    if len(ordered) == 0 or len(ordered) < minimum_evaluated:
        failures.append(
            f"only {len(ordered)} of {sample_size} drawn pairs produced a measurement "
            f"({skipped} skipped for having no finite exact route); at least "
            f"{minimum_evaluated} are required "
            f"(min_evaluated_fraction {min_evaluated_fraction:g}). An unmeasured "
            "sample cannot certify the surface"
        )
    else:
        if median_pct > thresholds["median_pct"]:
            failures.append(
                f"median absolute error {median_pct:.3f}% > "
                f"geography.distance_model_max_median_error_pct {thresholds['median_pct']:.3f}%"
            )
        if p95_pct > thresholds["p95_pct"]:
            failures.append(
                f"p95 absolute error {p95_pct:.3f}% > "
                f"geography.distance_model_max_p95_error_pct {thresholds['p95_pct']:.3f}%"
            )

    result = AccuracyGateResult(
        sample_size=sample_size,
        seed=seed,
        median_pct=median_pct,
        p95_pct=p95_pct,
        thresholds=thresholds,
        passed=not failures,
        distribution=_distribution(ordered),
        histogram=_histogram(ordered),
        evaluated_pairs=len(ordered),
        skipped_pairs=skipped,
        min_evaluated_fraction=min_evaluated_fraction,
        min_evaluated_pairs=minimum_evaluated,
    )
    if failures:
        raise GateFailure(
            "accuracy gate failed (research/osm-data.md §5.3, 03 §4.2); the remedy "
            "is more anchors or a finer partition, never a looser gate:\n  - "
            + "\n  - ".join(failures)
        )
    return result


def _minimum_evaluated(sample_size: int, fraction: float) -> int:
    thousandths = int(round(fraction * 1000))
    return max(1, -(-(thousandths * sample_size) // 1000))


def _distribution(ordered: Sequence[float]) -> dict[str, float]:
    if not ordered:
        return {}
    return {
        "min_pct": float(ordered[0]),
        "p25_pct": percentile(ordered, 0.25),
        "median_pct": _median(ordered),
        "p75_pct": percentile(ordered, 0.75),
        "p90_pct": percentile(ordered, 0.90),
        "p95_pct": percentile(ordered, 0.95),
        "p99_pct": percentile(ordered, 0.99),
        "max_pct": float(ordered[-1]),
        "mean_pct": sum(ordered) / len(ordered),
    }


_HISTOGRAM_EDGES: tuple[float, ...] = (1.0, 2.5, 5.0, 10.0, 25.0, 50.0, 100.0)


def _histogram(ordered: Sequence[float]) -> dict[str, int]:
    labels = []
    previous = 0.0
    for edge in _HISTOGRAM_EDGES:
        labels.append((f"[{previous:g},{edge:g})", previous, edge))
        previous = edge
    buckets = {label: 0 for label, _, _ in labels}
    overflow = f"[{previous:g},inf)"
    buckets[overflow] = 0
    for value in ordered:
        for label, low, high in labels:
            if low <= value < high:
                buckets[label] += 1
                break
        else:
            buckets[overflow] += 1
    return buckets


__all__ = [
    "DEFAULT_ACCURACY_MIN_EVALUATED_FRACTION",
    "DEFAULT_ACCURACY_SAMPLE_SIZE",
    "DEFAULT_ACCURACY_SEED",
    "SEGMENT_CLASSES",
    "AccuracyGateResult",
    "GateAddress",
    "StratificationCounts",
    "StratificationResult",
    "check_stratification",
    "count_stratification",
    "percentile",
    "route_distance_km",
    "run_accuracy_gate",
    "sample_pairs",
]
