from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .funnel import FunnelCounts
from .gates import AccuracyGateResult, StratificationResult
from .geometry import KM_PER_DEG_LAT, haversine_km, km_per_deg_lon
from .licensing import ATTRIBUTION_LINE, LICENSE_ID
from .manifest import SourceSnapshot

ARIZONA_ZIP_MIN = 85000
ARIZONA_ZIP_MAX = 86599


@dataclass(frozen=True)
class SanityChecks:

    rows_checked: int
    outside_crop: int
    zip_out_of_range: int
    duplicate_coordinate_pairs: int
    dedupe_radius_meters: float

    @property
    def passed(self) -> bool:
        return (
            self.outside_crop == 0
            and self.zip_out_of_range == 0
            and self.duplicate_coordinate_pairs == 0
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "rows_checked": self.rows_checked,
            "outside_crop": self.outside_crop,
            "zip_out_of_range": self.zip_out_of_range,
            "duplicate_coordinate_pairs": self.duplicate_coordinate_pairs,
            "dedupe_radius_meters": self.dedupe_radius_meters,
            "pass": self.passed,
        }


def run_sanity_checks(
    catalog_rows: Sequence[dict[str, object]],
    build_area,
    dedupe_radius_meters: float,
) -> SanityChecks:
    from shapely.geometry import Point

    radius_km = dedupe_radius_meters / 1000.0
    lat_cell_deg, lon_cell_deg = _bucket_size_degrees(build_area, radius_km)

    outside = 0
    bad_zip = 0
    buckets: dict[tuple[str, int, int], list[tuple[int, float, float]]] = {}
    for index, row in enumerate(catalog_rows):
        lon = float(str(row["longitude"]))
        lat = float(str(row["latitude"]))
        if not build_area.covers(Point(lon, lat)):
            outside += 1
        postal = str(row.get("postal_code") or "")
        if not (postal.isdigit() and ARIZONA_ZIP_MIN <= int(postal) <= ARIZONA_ZIP_MAX):
            bad_zip += 1
        street = str(row.get("street") or "")
        key = (street, int(lat // lat_cell_deg), int(lon // lon_cell_deg))
        buckets.setdefault(key, []).append((index, lon, lat))

    duplicates = 0
    seen_pairs: set[tuple[int, int]] = set()
    for (street, brow, bcol), entries in buckets.items():
        neighbourhood: list[tuple[int, float, float]] = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                neighbourhood.extend(buckets.get((street, brow + dr, bcol + dc), ()))
        for a_index, a_lon, a_lat in entries:
            for b_index, b_lon, b_lat in neighbourhood:
                if a_index == b_index:
                    continue
                pair = (a_index, b_index) if a_index < b_index else (b_index, a_index)
                if pair in seen_pairs:
                    continue
                if haversine_km(a_lon, a_lat, b_lon, b_lat) < radius_km:
                    seen_pairs.add(pair)
                    duplicates += 1
    return SanityChecks(
        rows_checked=len(catalog_rows),
        outside_crop=outside,
        zip_out_of_range=bad_zip,
        duplicate_coordinate_pairs=duplicates,
        dedupe_radius_meters=dedupe_radius_meters,
    )


def _bucket_size_degrees(build_area, radius_km: float) -> tuple[float, float]:
    _, min_lat, _, max_lat = build_area.bounds
    worst_lat = max(abs(min_lat), abs(max_lat))
    lat_cell = max(radius_km / KM_PER_DEG_LAT, 1e-9)
    lon_cell = max(radius_km / km_per_deg_lon(worst_lat), 1e-9)
    return lat_cell, lon_cell


@dataclass(frozen=True)
class AnchorStatistics:

    anchor_count: int
    assigned_addresses: int
    access_median_km: float
    access_p95_km: float
    access_max_km: float
    addresses_per_anchor_median: float
    addresses_per_anchor_max: int
    anchors_with_no_address: int

    def to_dict(self) -> dict[str, object]:
        return {
            "anchor_count": self.anchor_count,
            "assigned_addresses": self.assigned_addresses,
            "access_median_km": self.access_median_km,
            "access_p95_km": self.access_p95_km,
            "access_max_km": self.access_max_km,
            "addresses_per_anchor_median": self.addresses_per_anchor_median,
            "addresses_per_anchor_max": self.addresses_per_anchor_max,
            "anchors_with_no_address": self.anchors_with_no_address,
        }


def anchor_statistics(
    catalog_rows: Sequence[dict[str, object]], anchor_count: int
) -> AnchorStatistics:
    from .gates import percentile

    distances: list[float] = []
    per_anchor: dict[int, int] = {}
    for row in catalog_rows:
        if row.get("status") != "usable":
            continue
        anchor = row.get("anchor_id")
        access = row.get("access_distance_km")
        if anchor in (None, "") or access in (None, ""):
            continue
        per_anchor[int(str(anchor))] = per_anchor.get(int(str(anchor)), 0) + 1
        distances.append(float(str(access)))
    ordered = sorted(distances)
    loads = sorted(per_anchor.values())
    median_load = 0.0
    if loads:
        median_load = (
            float(loads[len(loads) // 2])
            if len(loads) % 2
            else (loads[len(loads) // 2 - 1] + loads[len(loads) // 2]) / 2.0
        )
    return AnchorStatistics(
        anchor_count=anchor_count,
        assigned_addresses=len(distances),
        access_median_km=percentile(ordered, 0.5),
        access_p95_km=percentile(ordered, 0.95),
        access_max_km=ordered[-1] if ordered else 0.0,
        addresses_per_anchor_median=median_load,
        addresses_per_anchor_max=max(loads) if loads else 0,
        anchors_with_no_address=anchor_count - len(per_anchor),
    )


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return lines


def render_data_quality(
    *,
    artifact_id: str,
    source: SourceSnapshot,
    funnel: FunnelCounts,
    stratification: StratificationResult,
    sanity: SanityChecks,
    anchors: AnchorStatistics,
    accuracy: AccuracyGateResult,
    zone_names: dict[int, str],
    unusable_by_reason: dict[str, int],
) -> str:
    lines: list[str] = [f"# Data quality report — geography artifact `{artifact_id}`", ""]
    lines += [
        "Generated by `tools/geo_build` (CSR-003). Every number below comes from",
        "the build that produced this directory; nothing here is hand-maintained.",
        "",
    ]

    lines += ["## 1. Source snapshot identity and checksums", ""]
    if source.is_placeholder:
        lines += [
            "> **Placeholder source.** This artifact was built from a committed",
            "> fixture, not from a pinned Geofabrik snapshot. The `source` block of",
            "> `manifest.json` is marked `placeholder: true` and this artifact must",
            "> not be pinned as `geography.artifact_sha256`.",
            "",
        ]
    lines += _table(
        ["field", "value"],
        [
            ["url", f"`{source.url}`"],
            ["published_at", source.published_at],
            ["bytes", source.bytes],
            ["sha256", f"`{source.sha256}`"],
        ],
    )
    lines.append("")

    lines += ["## 2. Funnel counts", ""]
    lines += _table(
        ["stage", "count"],
        [
            ["candidates (elements carrying at least one `addr:*` tag, post-crop)",
             funnel.candidates_total],
            ["accepted (usable)", funnel.accepted],
            ["rejected (all reasons)", funnel.total_rejected()],
        ],
    )
    lines.append("")
    lines += _table(
        ["rejection reason", "count", "retained in catalog.csv"],
        [
            [reason, count, "yes (`status = unusable`)" if reason in unusable_by_reason else "no"]
            for reason, count in sorted(funnel.rejected_by_reason.items())
        ],
    )
    lines += [
        "",
        "Reasons marked *retained* are the §4.8 routability failures: those rows stay",
        "in `catalog.csv` with `status = unusable` and a recorded `unusable_reason`,",
        "so 03 invariant 3 (\"every packaged address is either usable and routable, or",
        "unusable and excluded from sampling\") is verifiable from the catalog alone.",
        "Every earlier rejection leaves the catalog entirely and appears only in",
        "`addresses/rejected.csv`.",
        "",
    ]

    counts = stratification.counts
    floors = stratification.floors
    lines += ["## 3. Usable addresses by zone, segment, and zone x segment", ""]
    zone_rows = []
    for zone_id in sorted(counts.by_zone):
        zone_rows.append(
            [
                zone_id,
                zone_names.get(zone_id, "?"),
                counts.by_zone[zone_id],
                floors["min_addresses_per_zone"],
                "ok" if counts.by_zone[zone_id] >= floors["min_addresses_per_zone"] else "FAIL",
            ]
        )
    lines += _table(["zone_id", "zone", "usable", "floor", "gate"], zone_rows)
    lines.append("")
    lines += _table(
        ["scope", "count", "floor", "gate"],
        [
            [
                "total usable",
                counts.total_usable,
                floors["min_address_catalog_size"],
                "ok" if counts.total_usable >= floors["min_address_catalog_size"] else "FAIL",
            ],
            [
                "outlying ring (`zone_id` null)",
                counts.outlying,
                floors["min_outlying_addresses"],
                "ok" if counts.outlying >= floors["min_outlying_addresses"] else "FAIL",
            ],
        ],
    )
    lines.append("")
    segment_rows = []
    for (zone_id, segment), count in sorted(counts.by_zone_segment.items()):
        segment_rows.append(
            [
                zone_id,
                segment,
                count,
                floors["min_addresses_per_zone_segment"],
                "ok" if count >= floors["min_addresses_per_zone_segment"] else "FAIL",
            ]
        )
    lines += _table(["zone_id", "segment", "usable", "floor", "gate"], segment_rows)
    lines.append("")
    lines += ["### Tag provenance (§4.2 derivation breakdown)", ""]
    lines += _table(
        ["field", "source", "count"],
        [["city", k, v] for k, v in sorted(funnel.city_source_counts.items())]
        + [["state", k, v] for k, v in sorted(funnel.state_source_counts.items())],
    )
    lines.append("")

    lines += ["## 4. Coordinate and ZIP sanity checks", ""]
    lines += _table(
        ["check", "violations", "result"],
        [
            ["every catalog point inside the crop", sanity.outside_crop,
             "ok" if sanity.outside_crop == 0 else "FAIL"],
            [f"every ZIP in {ARIZONA_ZIP_MIN}-{ARIZONA_ZIP_MAX}", sanity.zip_out_of_range,
             "ok" if sanity.zip_out_of_range == 0 else "FAIL"],
            [
                f"no same-street pair within {sanity.dedupe_radius_meters:g} m",
                sanity.duplicate_coordinate_pairs,
                "ok" if sanity.duplicate_coordinate_pairs == 0 else "FAIL",
            ],
        ],
    )
    lines.append("")

    lines += ["## 5. Anchor statistics", ""]
    lines += _table(
        ["statistic", "value"],
        [
            ["anchors", anchors.anchor_count],
            ["usable addresses with an anchor", anchors.assigned_addresses],
            ["access distance, median (km)", f"{anchors.access_median_km:.3f}"],
            ["access distance, p95 (km)", f"{anchors.access_p95_km:.3f}"],
            ["access distance, max (km)", f"{anchors.access_max_km:.3f}"],
            ["addresses per anchor, median", f"{anchors.addresses_per_anchor_median:g}"],
            ["addresses per anchor, max", anchors.addresses_per_anchor_max],
            ["anchors with no address assigned", anchors.anchors_with_no_address],
        ],
    )
    lines.append("")

    lines += ["## 6. Accuracy gate (§5.3)", ""]
    lines += _table(
        ["field", "value"],
        [
            ["sample size (drawn)", accuracy.sample_size],
            ["pairs evaluated", accuracy.evaluated_pairs],
            ["pairs skipped (no finite exact route)", accuracy.skipped_pairs],
            [
                "minimum evaluated pairs required",
                f"{accuracy.min_evaluated_pairs} "
                f"({accuracy.min_evaluated_fraction:g} of the drawn sample)",
            ],
            ["seed", f"`{accuracy.seed}`"],
            ["median absolute error", f"{accuracy.median_pct:.3f}%"],
            ["p95 absolute error", f"{accuracy.p95_pct:.3f}%"],
            ["median threshold", f"{accuracy.thresholds['median_pct']:.3f}%"],
            ["p95 threshold", f"{accuracy.thresholds['p95_pct']:.3f}%"],
            ["result", "PASS" if accuracy.passed else "FAIL"],
        ],
    )
    lines.append("")
    if accuracy.distribution:
        lines += _table(
            ["percentile", "absolute error"],
            [[k, f"{v:.3f}%"] for k, v in accuracy.distribution.items()],
        )
        lines.append("")
    if accuracy.histogram:
        lines += _table(
            ["absolute error bucket (%)", "pairs"],
            [[k, v] for k, v in accuracy.histogram.items()],
        )
        lines.append("")

    lines += [
        "## 7. Known limitations",
        "",
        "- **OSM address coverage is uneven and denser in urban cores.** Per-zone",
        "  address density in this catalog reflects how thoroughly a neighbourhood",
        "  has been mapped, not how many people live there. The §4.9 stratification",
        "  floors bound the worst case; they do not make the density realistic.",
        "- **The outlying ring is sparser than reality.** It is a buffer around the",
        "  service core, not a real settlement pattern, so out-of-area calls are",
        "  drawn from a thinner and more clustered population than Arizona has.",
        "- **Route distances are symmetric by construction** (§5.2): the anchor",
        "  matrix stores `(d(i,j) + d(j,i)) / 2`, so genuine one-way asymmetry is",
        "  averaged away. This is a deliberate v1 approximation that keeps the",
        "  agent's map lookup and the engine's two legs from ever disagreeing.",
        "- **Distances are an anchor decomposition, not exact pair routing.** Every",
        "  distance the simulation serves is `access + anchor-to-anchor + access`;",
        "  the §5.3 gate above bounds how far that lands from exact road routing.",
        "- **Addresses are real buildings; occupants are not.** No contributor",
        "  metadata, occupant identity, or contact tag enters the artifact.",
        "",
    ]

    lines += [
        "## 8. Attribution and license",
        "",
        ATTRIBUTION_LINE,
        "",
        f"This derived database is licensed under `{LICENSE_ID}`; the full text ships",
        "as `LICENSE` at the artifact root, and `ATTRIBUTION.md` carries the same",
        "attribution line.",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "ARIZONA_ZIP_MAX",
    "ARIZONA_ZIP_MIN",
    "AnchorStatistics",
    "SanityChecks",
    "anchor_statistics",
    "render_data_quality",
    "run_sanity_checks",
]
