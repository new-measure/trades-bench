from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from pathlib import Path

from .anchors import ANCHOR_COLUMNS, ANCHOR_DECIMAL_PLACES, Anchor
from .models import RejectedCandidate
from .strip import COORDINATE_DECIMAL_PLACES
from .zones import PHOENIX_SPLIT_LATITUDE, PHOENIX_SPLIT_PROVENANCE_NOTE, ZoneExtractionResult

CATALOG_SORT_KEY = "canonical_key"

_SOURCE_TYPE_RANK = {"node": 0, "way": 1, "relation": 2}


def _rejected_sort_key(row: RejectedCandidate) -> tuple[int, int]:
    return (_SOURCE_TYPE_RANK[row.source_type], row.source_id)


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_csv_cell(row.get(c)) for c in columns])


def assign_address_ids(rows: list[dict[str, object]]) -> list[dict[str, object]]:
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
    if isinstance(obj, (list, tuple)):
        if obj and all(isinstance(x, (int, float)) for x in obj):
            return [round(float(x), COORDINATE_DECIMAL_PLACES) for x in obj]
        return [_round_coords(x) for x in obj]
    return obj


def canonical_geometry_payload(geometry_mapping: dict) -> dict[str, object]:
    return {
        "type": geometry_mapping["type"],
        "coordinates": _round_coords(geometry_mapping["coordinates"]),
    }


def write_zone_geojson(path: Path, zone_result: ZoneExtractionResult) -> None:
    from shapely.geometry import mapping

    features = []
    for zone in sorted(zone_result.zones, key=lambda z: z.zone_id):
        geom = canonical_geometry_payload(mapping(zone.polygon))
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

    overlap_payload = sorted(
        (dict(r) for r in zone_result.overlap_reconciliations),
        key=lambda r: (str(r["kept_by"]), str(r["ceded_by"])),
    )

    payload = {
        "zones": zones_payload,
        "municipal_relations": municipal_payload,
        "overlap_reconciliations": overlap_payload,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")


def write_anchors_csv(path: Path, anchors: Sequence[Anchor]) -> None:
    rows = [
        {
            "anchor_id": anchor.anchor_id,
            "latitude": f"{anchor.lat:.{ANCHOR_DECIMAL_PLACES}f}",
            "longitude": f"{anchor.lon:.{ANCHOR_DECIMAL_PLACES}f}",
            "selection_cell": anchor.selection_cell,
        }
        for anchor in sorted(anchors, key=lambda a: a.anchor_id)
    ]
    write_csv(path, ANCHOR_COLUMNS, rows)


def write_abbreviation_table(path: Path, table: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(table, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")


__all__ = [
    "CATALOG_SORT_KEY",
    "REJECTED_COLUMNS",
    "assign_address_ids",
    "canonical_geometry_payload",
    "rejected_to_row",
    "sort_rejected",
    "write_abbreviation_table",
    "write_anchors_csv",
    "write_csv",
    "write_zone_geojson",
    "write_zone_provenance",
]
