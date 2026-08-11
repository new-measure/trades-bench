from __future__ import annotations

import array
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import geo_artifact
from .errors import EngineError, ErrorCode

CATALOG_RELATIVE_PATH = "addresses/catalog.csv"
ANCHORS_RELATIVE_PATH = "routing/anchors.csv"
ANCHOR_MATRIX_RELATIVE_PATH = "routing/anchor_distance.bin"
ZONE_DEFINITIONS_RELATIVE_PATH = "zones/zone_definitions.geojson"
ABBREVIATION_TABLE_RELATIVE_PATH = "normalization/usps_abbreviations.json"

ANCHOR_MATRIX_FIXED_POINT_SCALE = 100


@dataclass(frozen=True)
class CatalogRow:

    address_id: int
    dataset_version: str
    house_number: str
    street: str
    city: str
    state: str
    postal_code: str
    formatted_address: str
    canonical_key: str
    latitude: float
    longitude: float
    zone_id: int | None
    effective_zone_id: int
    anchor_id: int | None
    access_distance_km: float | None
    segment_eligibility: str
    status: str
    unusable_reason: str | None


@dataclass(frozen=True)
class AnchorRow:

    anchor_id: int
    latitude: float
    longitude: float
    selection_cell: str


@dataclass(frozen=True)
class ZoneCentroid:

    zone_id: int
    code: str
    centroid_lat: float
    centroid_lon: float


@dataclass(frozen=True)
class GeoArtifact:

    manifest: dict
    catalog: tuple[CatalogRow, ...]
    anchors: tuple[AnchorRow, ...]
    anchor_matrix: array.array
    zone_centroids: tuple[ZoneCentroid, ...]
    abbreviation_table: dict[str, dict[str, str]]

    def anchor_distance_km(self, anchor_id_a: int, anchor_id_b: int) -> float:
        n = len(self.anchors)
        i, j = anchor_id_a - 1, anchor_id_b - 1
        if not (0 <= i < n) or not (0 <= j < n):
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                "anchor_id is out of range for the loaded anchor matrix",
                details={
                    "anchor_id_a": anchor_id_a,
                    "anchor_id_b": anchor_id_b,
                    "anchor_count": n,
                },
            )
        return self.anchor_matrix[i * n + j] / ANCHOR_MATRIX_FIXED_POINT_SCALE


def read_manifest(artifact_dir: Path | str) -> dict:
    path = Path(artifact_dir) / geo_artifact.MANIFEST_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def _empty_to_none(value: str) -> str | None:
    return value if value != "" else None


def read_catalog(artifact_dir: Path | str) -> tuple[CatalogRow, ...]:
    path = Path(artifact_dir) / CATALOG_RELATIVE_PATH
    rows: list[CatalogRow] = []
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            rows.append(
                CatalogRow(
                    address_id=int(raw["address_id"]),
                    dataset_version=raw["dataset_version"],
                    house_number=raw["house_number"],
                    street=raw["street"],
                    city=raw["city"],
                    state=raw["state"],
                    postal_code=raw["postal_code"],
                    formatted_address=raw["formatted_address"],
                    canonical_key=raw["canonical_key"],
                    latitude=float(raw["latitude"]),
                    longitude=float(raw["longitude"]),
                    zone_id=int(raw["zone_id"]) if raw["zone_id"] != "" else None,
                    effective_zone_id=int(raw["effective_zone_id"]),
                    anchor_id=int(raw["anchor_id"]) if raw["anchor_id"] != "" else None,
                    access_distance_km=(
                        float(raw["access_distance_km"])
                        if raw["access_distance_km"] != ""
                        else None
                    ),
                    segment_eligibility=raw["segment_eligibility"],
                    status=raw["status"],
                    unusable_reason=_empty_to_none(raw["unusable_reason"]),
                )
            )
    return tuple(rows)


def read_anchors(artifact_dir: Path | str) -> tuple[AnchorRow, ...]:
    path = Path(artifact_dir) / ANCHORS_RELATIVE_PATH
    rows: list[AnchorRow] = []
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            rows.append(
                AnchorRow(
                    anchor_id=int(raw["anchor_id"]),
                    latitude=float(raw["latitude"]),
                    longitude=float(raw["longitude"]),
                    selection_cell=raw["selection_cell"],
                )
            )
    return tuple(rows)


def read_anchor_matrix(artifact_dir: Path | str, anchor_count: int) -> array.array:
    path = Path(artifact_dir) / ANCHOR_MATRIX_RELATIVE_PATH
    expected_size = 4 * anchor_count * anchor_count
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise EngineError(
            ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
            "anchor_distance.bin size does not match 4*n*n for the anchor count",
            details={
                "anchor_count": anchor_count,
                "expected_size": expected_size,
                "actual_size": actual_size,
            },
        )
    values = array.array("I")
    with open(path, "rb") as f:
        values.fromfile(f, anchor_count * anchor_count)
    if sys.byteorder == "big":
        values.byteswap()
    return values


def _ring_area_and_centroid(ring: list[list[float]]) -> tuple[float, float, float]:
    area = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area /= 2
    if area == 0:
        return 0.0, ring[0][0], ring[0][1]
    return area, cx / (6 * area), cy / (6 * area)


def _part_area_and_weighted_centroid(part: list[list[list[float]]]) -> tuple[float, float, float]:
    shell_area, shell_cx, shell_cy = _ring_area_and_centroid(part[0])
    net_area = abs(shell_area)
    net_cx = abs(shell_area) * shell_cx
    net_cy = abs(shell_area) * shell_cy
    for hole in part[1:]:
        hole_area, hole_cx, hole_cy = _ring_area_and_centroid(hole)
        net_area -= abs(hole_area)
        net_cx -= abs(hole_area) * hole_cx
        net_cy -= abs(hole_area) * hole_cy
    return net_area, net_cx, net_cy


def _polygon_centroid(geometry: dict) -> tuple[float, float]:
    parts = (
        geometry["coordinates"]
        if geometry["type"] == "MultiPolygon"
        else [geometry["coordinates"]]
    )
    total_area = 0.0
    total_cx = 0.0
    total_cy = 0.0
    for part in parts:
        part_area, part_cx_sum, part_cy_sum = _part_area_and_weighted_centroid(part)
        weight = abs(part_area)
        total_area += weight
        if weight == 0:
            continue
        total_cx += weight * (part_cx_sum / part_area)
        total_cy += weight * (part_cy_sum / part_area)
    if total_area == 0:
        raise EngineError(
            ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
            "zone polygon centroid is undefined: geometry has zero total area",
        )
    return total_cx / total_area, total_cy / total_area


def read_zone_centroids(artifact_dir: Path | str) -> tuple[ZoneCentroid, ...]:
    path = Path(artifact_dir) / ZONE_DEFINITIONS_RELATIVE_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    centroids: list[ZoneCentroid] = []
    for feature in data["features"]:
        properties = feature["properties"]
        lon, lat = _polygon_centroid(feature["geometry"])
        centroids.append(
            ZoneCentroid(
                zone_id=int(properties["zone_id"]),
                code=str(properties["code"]),
                centroid_lat=lat,
                centroid_lon=lon,
            )
        )
    return tuple(sorted(centroids, key=lambda c: c.zone_id))


def read_abbreviation_table(artifact_dir: Path | str) -> dict[str, dict[str, str]]:
    path = Path(artifact_dir) / ABBREVIATION_TABLE_RELATIVE_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def load_artifact(artifact_dir: Path | str) -> GeoArtifact:
    manifest = read_manifest(artifact_dir)
    anchors = read_anchors(artifact_dir)
    anchor_matrix = read_anchor_matrix(artifact_dir, len(anchors))
    return GeoArtifact(
        manifest=manifest,
        catalog=read_catalog(artifact_dir),
        anchors=anchors,
        anchor_matrix=anchor_matrix,
        zone_centroids=read_zone_centroids(artifact_dir),
        abbreviation_table=read_abbreviation_table(artifact_dir),
    )


def zone_place_data(
    catalog: tuple[CatalogRow, ...],
) -> dict[int, tuple[list[str], list[str]]]:
    cities: dict[int, set[str]] = {}
    postal_codes: dict[int, set[str]] = {}
    for row in catalog:
        if row.zone_id is None:
            continue
        cities.setdefault(row.zone_id, set()).add(row.city)
        postal_codes.setdefault(row.zone_id, set()).add(row.postal_code)
    return {
        zone_id: (sorted(cities[zone_id]), sorted(postal_codes[zone_id]))
        for zone_id in sorted(cities)
    }


def verify_artifact_sha256(artifact_dir: Path | str | None, expected_sha256: str) -> str:
    if not expected_sha256:
        raise EngineError(
            ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
            "geography.artifact_sha256 is empty: no geography artifact is pinned",
        )
    if artifact_dir is None:
        raise EngineError(
            ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
            "no geography artifact directory was supplied to verify against "
            "geography.artifact_sha256",
            details={"expected_sha256": expected_sha256},
        )
    try:
        computed_sha256 = geo_artifact.canonical_artifact_sha256(artifact_dir)
    except EngineError as exc:
        if exc.code is ErrorCode.INVALID_ARGUMENT:
            raise EngineError(
                ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
                "geography artifact directory does not exist or is not a directory",
                details={"path": str(artifact_dir)},
            ) from exc
        raise
    if computed_sha256 != expected_sha256:
        raise EngineError(
            ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
            "geography artifact hash does not match geography.artifact_sha256",
            details={"expected_sha256": expected_sha256, "computed_sha256": computed_sha256},
        )
    return computed_sha256


def verify_manifest_consistency(
    artifact: GeoArtifact, computed_sha256: str, dataset_version: str
) -> None:
    manifest_sha256 = str(artifact.manifest.get("artifact_sha256", ""))
    if manifest_sha256 != computed_sha256:
        raise EngineError(
            ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
            "manifest.json's recorded artifact_sha256 does not match the recomputed "
            "artifact hash",
            details={"manifest_artifact_sha256": manifest_sha256, "computed_sha256": computed_sha256},
        )
    manifest_catalog_rows = artifact.manifest.get("counts", {}).get("catalog_rows")
    if manifest_catalog_rows is not None and int(manifest_catalog_rows) != len(artifact.catalog):
        raise EngineError(
            ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
            "manifest.json's counts.catalog_rows does not match the loaded catalog row "
            "count",
            details={
                "manifest_catalog_rows": manifest_catalog_rows,
                "loaded_catalog_rows": len(artifact.catalog),
            },
        )
    manifest_anchor_count = artifact.manifest.get("anchors", {}).get("count")
    if manifest_anchor_count is not None and int(manifest_anchor_count) != len(artifact.anchors):
        raise EngineError(
            ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
            "manifest.json's anchors.count does not match the loaded anchor row count",
            details={
                "manifest_anchor_count": manifest_anchor_count,
                "loaded_anchor_count": len(artifact.anchors),
            },
        )
    mismatched = sorted(
        {row.dataset_version for row in artifact.catalog if row.dataset_version != dataset_version}
    )
    if mismatched:
        raise EngineError(
            ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
            "addresses/catalog.csv contains dataset_version value(s) that do not match "
            "the resolved geography dataset_version",
            details={"resolved_dataset_version": dataset_version, "found": mismatched},
        )


__all__ = [
    "AnchorRow",
    "CatalogRow",
    "GeoArtifact",
    "ZoneCentroid",
    "load_artifact",
    "read_abbreviation_table",
    "read_anchor_matrix",
    "read_anchors",
    "read_catalog",
    "read_manifest",
    "read_zone_centroids",
    "verify_artifact_sha256",
    "verify_manifest_consistency",
    "zone_place_data",
]
