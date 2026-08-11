from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import csr_bench

from . import __version__ as GEO_BUILD_VERSION
from .licensing import ATTRIBUTION_LINE, LICENSE_ID

PLACEHOLDER = "PLACEHOLDER"

BUILDER_VERSION = f"csr_bench/{csr_bench.SPEC_VERSION}+geo_build/{GEO_BUILD_VERSION}"

REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = (
    "artifact_id",
    "artifact_sha256",
    "built_at",
    "builder_version",
    "source.url",
    "source.published_at",
    "source.bytes",
    "source.sha256",
    "crop.polygon_sha256",
    "crop.buffer_km",
    "tools",
    "zone_definitions_sha256",
    "counts.candidates",
    "counts.accepted",
    "counts.rejected_by_reason",
    "counts.usable_by_zone",
    "counts.usable_by_zone_segment",
    "counts.outlying",
    "anchors.count",
    "anchors.selection_procedure",
    "anchor_matrix_sha256",
    "accuracy_gate.sample_size",
    "accuracy_gate.seed",
    "accuracy_gate.median_pct",
    "accuracy_gate.p95_pct",
    "accuracy_gate.thresholds",
    "accuracy_gate.pass",
    "license",
    "accuracy_gate.evaluated_pairs",
    "accuracy_gate.skipped_pairs",
    "accuracy_gate.min_evaluated_fraction",
    "accuracy_gate.min_evaluated_pairs",
)


@dataclass(frozen=True)
class SourceSnapshot:

    url: str = PLACEHOLDER
    published_at: str = PLACEHOLDER
    bytes: int = 0
    sha256: str = PLACEHOLDER

    @property
    def is_placeholder(self) -> bool:
        return (
            self.url == PLACEHOLDER
            or self.sha256 == PLACEHOLDER
            or self.published_at == PLACEHOLDER
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "published_at": self.published_at,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "placeholder": self.is_placeholder,
        }


@dataclass(frozen=True)
class ToolRecord:

    name: str
    version: str
    image_digest: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "version": self.version, "image_digest": self.image_digest}


def tool_records(backend_name: str, backend_version: str, image_digest: str | None) -> list[dict]:
    import platform

    tools = [
        ToolRecord(name="python", version=platform.python_version()),
        ToolRecord(name=backend_name, version=backend_version, image_digest=image_digest),
    ]
    for distribution, label in (("osmium", "pyosmium"), ("shapely", "shapely")):
        tools.append(ToolRecord(name=label, version=_distribution_version(distribution)))
    return [t.to_dict() for t in sorted(tools, key=lambda t: t.name)]


def _distribution_version(name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:  # pragma: no cover - the geo group is installed
        return "unknown"


def build_manifest(
    *,
    artifact_id: str,
    artifact_sha256: str,
    source: SourceSnapshot,
    crop_polygon_sha256: str,
    crop_buffer_km: float,
    tools: list[dict],
    zone_definitions_sha256: str,
    counts: dict[str, object],
    anchor_count: int,
    anchor_selection_procedure: dict[str, object],
    anchor_matrix_sha256: str,
    accuracy_gate: dict[str, object],
    routing_backend: dict[str, object],
    packageable: bool,
    built_at: str | None = None,
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "artifact_sha256": artifact_sha256,
        "built_at": built_at if built_at is not None else _now_iso(),
        "builder_version": BUILDER_VERSION,
        "source": source.to_dict(),
        "crop": {"polygon_sha256": crop_polygon_sha256, "buffer_km": crop_buffer_km},
        "tools": tools,
        "zone_definitions_sha256": zone_definitions_sha256,
        "counts": counts,
        "anchors": {"count": anchor_count, "selection_procedure": anchor_selection_procedure},
        "anchor_matrix_sha256": anchor_matrix_sha256,
        "accuracy_gate": accuracy_gate,
        "license": {"id": LICENSE_ID, "attribution": ATTRIBUTION_LINE},
        "routing_backend": routing_backend,
        "packageable": packageable,
    }


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, sort_keys=True, indent=2)
        f.write("\n")


def missing_required_fields(manifest: dict[str, object]) -> list[str]:
    missing = []
    for dotted in REQUIRED_MANIFEST_FIELDS:
        node: object = manifest
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                missing.append(dotted)
                break
            node = node[part]
    return missing


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "BUILDER_VERSION",
    "PLACEHOLDER",
    "REQUIRED_MANIFEST_FIELDS",
    "SourceSnapshot",
    "ToolRecord",
    "build_manifest",
    "missing_required_fields",
    "tool_records",
    "write_manifest",
]
