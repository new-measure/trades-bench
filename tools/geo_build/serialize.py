"""Deterministic serialization (CSR-003 decision 3): every tabular artifact
file is sorted CSV with LF line endings and a stated sort key, coordinates
and JSON both at fixed decimal precision, and no wall-clock timestamps
inside any hashed/emitted content — running a stage twice on the same input
Must produce byte-identical files.

CSV was chosen over parquet because the runtime (`src/csr_bench`) is
stdlib-only and must be able to read the artifact without a third-party
dependency (decision 3; research/osm-data.md §6 records the same choice).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .funnel import FunnelCounts
from .models import RejectedCandidate
from .strip import COORDINATE_DECIMAL_PLACES
from .zones import PHOENIX_SPLIT_LATITUDE, PHOENIX_SPLIT_PROVENANCE_NOTE, ZoneExtractionResult

#: catalog.csv's sort key (research/osm-data.md §7: "deterministic sorting
#: ... not tolerated" otherwise). `canonical_key` is unique post-dedupe and
#: orders the catalog by its own formatted address — a human skimming the
#: file finds addresses in a legible, address-like order for free.
CATALOG_SORT_KEY = "canonical_key"

#: rejected.csv's sort key: the same (source_type, source_id) order dedupe
#: already walks candidates in (node < way < relation, then ascending id).
_SOURCE_TYPE_RANK = {"node": 0, "way": 1, "relation": 2}


def _rejected_sort_key(row: RejectedCandidate) -> tuple[int, int]:
    return (_SOURCE_TYPE_RANK[row.source_type], row.source_id)


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    """Write ``rows`` (already in final sorted order) as CSV with a fixed
    ``\\n`` line terminator — the csv module's own default is ``\\r\\n``,
    which would make the artifact's byte identity depend on the writer's
    platform default rather than only on its content.
    """
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_csv_cell(row.get(c)) for c in columns])


def assign_address_ids(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Sort catalog rows by :data:`CATALOG_SORT_KEY` and assign the opaque,
    1-based ``address_id`` from that final order — a pure function of the
    deterministic row order, never of any OSM element id (03 §3.1: "no OSM
    ids" in the catalog).
    """
    ordered = sorted(rows, key=lambda r: r[CATALOG_SORT_KEY])
    for i, row in enumerate(ordered, start=1):
        row["address_id"] = i
    return ordered


def sort_rejected(rows: list[RejectedCandidate]) -> list[RejectedCandidate]:
    return sorted(rows, key=_rejected_sort_key)


def rejected_to_row(rejected: RejectedCandidate) -> dict[str, object]:
    return {
        "source_type": rejected.source_type,
        "source_id": rejected.source_id,
        "reason": rejected.reason,
        "detail": rejected.detail,
    }


REJECTED_COLUMNS: tuple[str, ...] = ("source_type", "source_id", "reason", "detail")


def _round_coords(obj: object) -> object:
    """Recursively round every numeric leaf of a GeoJSON ``coordinates``
    structure to :data:`tools.geo_build.strip.COORDINATE_DECIMAL_PLACES` —
    the same fixed precision the catalog uses, so the whole artifact agrees
    on coordinate resolution.
    """
    if isinstance(obj, (list, tuple)):
        if obj and all(isinstance(x, (int, float)) for x in obj):
            return [round(float(x), COORDINATE_DECIMAL_PLACES) for x in obj]
        return [_round_coords(x) for x in obj]
    return obj


def write_zone_geojson(path: Path, zone_result: ZoneExtractionResult) -> None:
    """``zones/zone_definitions.geojson`` (research/osm-data.md §6): one
    Feature per zone, ordered by ``zone_id``, coordinates at fixed precision.
    """
    from shapely.geometry import mapping

    features = []
    for zone in sorted(zone_result.zones, key=lambda z: z.zone_id):
        geom = mapping(zone.polygon)
        geom = {"type": geom["type"], "coordinates": _round_coords(geom["coordinates"])}
        features.append(
            {
                "type": "Feature",
                "properties": {"zone_id": zone.zone_id, "code": zone.code, "name": zone.name},
                "geometry": geom,
            }
        )
    payload = {"type": "FeatureCollection", "features": features}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")


def write_zone_provenance(path: Path, zone_result: ZoneExtractionResult) -> None:
    """``zones/zone_provenance.json`` (research/osm-data.md §4.7: "the OSM
    administrative relation identifiers used for each incorporated
    municipality, and ... the pinned dividing geometry").
    """
    zones_payload = []
    for zone in sorted(zone_result.zones, key=lambda z: z.zone_id):
        entry: dict[str, object] = {
            "zone_id": zone.zone_id,
            "code": zone.code,
            "name": zone.name,
            "source_relation_ids": sorted(zone.source_relation_ids),
        }
        if zone.code in ("central_phoenix", "north_phoenix"):
            entry["dividing_geometry"] = {
                "kind": "latitude_line",
                "latitude": PHOENIX_SPLIT_LATITUDE,
                "provenance": PHOENIX_SPLIT_PROVENANCE_NOTE,
            }
        zones_payload.append(entry)

    municipal_payload = [
        {"name": m.name, "osm_relation_id": m.osm_relation_id}
        for m in sorted(zone_result.municipal_relations, key=lambda m: m.osm_relation_id)
    ]

    payload = {"zones": zones_payload, "municipal_relations": municipal_payload}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")


def write_funnel_csv(path: Path, funnel: FunnelCounts) -> None:
    rows: list[tuple[str, str, int]] = [
        ("summary", "candidates_total", funnel.candidates_total),
        ("summary", "accepted", funnel.accepted),
    ]
    for k, v in sorted(funnel.rejected_by_reason.items()):
        rows.append(("rejected_by_reason", k, v))
    for k, v in sorted(funnel.city_source_counts.items()):
        rows.append(("city_source", k, v))
    for k, v in sorted(funnel.state_source_counts.items()):
        rows.append(("state_source", k, v))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["category", "key", "count"])
        for category, key, count in rows:
            writer.writerow([category, key, count])


def write_funnel_json(path: Path, funnel: FunnelCounts) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(funnel.to_dict(), f, sort_keys=True, separators=(",", ":"))
        f.write("\n")


__all__ = [
    "CATALOG_SORT_KEY",
    "REJECTED_COLUMNS",
    "assign_address_ids",
    "rejected_to_row",
    "sort_rejected",
    "write_csv",
    "write_funnel_csv",
    "write_funnel_json",
    "write_zone_geojson",
    "write_zone_provenance",
]
