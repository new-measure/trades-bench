from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .licensing import ATTRIBUTION_LINE, LICENSE_ID

PLACEHOLDER = "PLACEHOLDER"

REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = (
    "artifact_id",
    "artifact_sha256",
    "source.url",
    "source.published_at",
    "source.bytes",
    "source.sha256",
    "zone_definitions_sha256",
    "counts.catalog_rows",
    "anchors.count",
    "license",
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
        }


def build_manifest(
    *,
    artifact_id: str,
    artifact_sha256: str,
    source: SourceSnapshot,
    zone_definitions_sha256: str,
    catalog_rows: int,
    anchor_count: int,
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "artifact_sha256": artifact_sha256,
        "source": source.to_dict(),
        "zone_definitions_sha256": zone_definitions_sha256,
        "counts": {"catalog_rows": catalog_rows},
        "anchors": {"count": anchor_count},
        "license": {"id": LICENSE_ID, "attribution": ATTRIBUTION_LINE},
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


__all__ = [
    "PLACEHOLDER",
    "REQUIRED_MANIFEST_FIELDS",
    "SourceSnapshot",
    "build_manifest",
    "missing_required_fields",
    "write_manifest",
]
