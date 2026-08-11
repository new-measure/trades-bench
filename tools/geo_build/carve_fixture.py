from __future__ import annotations

import argparse
from pathlib import Path

import osmium
import osmium.filter

DRIVABLE_HIGHWAY_VALUES: frozenset[str] = frozenset(
    {
        "motorway",
        "trunk",
        "primary",
        "secondary",
        "tertiary",
        "unclassified",
        "residential",
        "living_street",
        "service",
        "motorway_link",
        "trunk_link",
        "primary_link",
        "secondary_link",
        "tertiary_link",
    }
)

TEMPE_BBOX: tuple[float, float, float, float] = (-111.945, 33.395, -111.925, 33.410)


def _has_any_addr_tag(tags: dict[str, str]) -> bool:
    return any(k.startswith("addr:") for k in tags)


def _in_bbox(lon: float, lat: float, bbox: tuple[float, float, float, float]) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def _scan(source: Path, bbox: tuple[float, float, float, float]) -> tuple[set[int], set[int]]:
    keep_node_ids: set[int] = set()
    keep_way_ids: set[int] = set()

    processor = (
        osmium.FileProcessor(str(source))
        .with_locations()
        .with_filter(osmium.filter.EmptyTagFilter())
    )
    for obj in processor:
        tags = dict(obj.tags)
        if obj.is_node():
            loc = obj.location
            if loc.valid() and _has_any_addr_tag(tags) and _in_bbox(loc.lon, loc.lat, bbox):
                keep_node_ids.add(obj.id)
            continue
        if not obj.is_way():
            continue

        addr_tagged = _has_any_addr_tag(tags)
        drivable = tags.get("highway") in DRIVABLE_HIGHWAY_VALUES
        if not addr_tagged and not drivable:
            continue

        node_refs = [nd.ref for nd in obj.nodes]
        valid_coords = [
            (nd.location.lon, nd.location.lat) for nd in obj.nodes if nd.location.valid()
        ]
        if not valid_coords:
            continue

        keep = drivable and any(_in_bbox(lon, lat, bbox) for lon, lat in valid_coords)
        if addr_tagged:
            centroid_lon = sum(c[0] for c in valid_coords) / len(valid_coords)
            centroid_lat = sum(c[1] for c in valid_coords) / len(valid_coords)
            keep = keep or _in_bbox(centroid_lon, centroid_lat, bbox)

        if keep:
            keep_way_ids.add(obj.id)
            keep_node_ids.update(node_refs)

    return keep_node_ids, keep_way_ids


def _copy(source: Path, destination: Path, keep_node_ids: set[int], keep_way_ids: set[int]) -> None:
    if destination.exists():
        destination.unlink()
    writer = osmium.SimpleWriter(str(destination))
    try:
        for obj in osmium.FileProcessor(str(source)):
            if obj.is_node() and obj.id in keep_node_ids:
                writer.add_node(obj)
            elif obj.is_way() and obj.id in keep_way_ids:
                writer.add_way(obj)
    finally:
        writer.close()


def carve(
    source: str | Path,
    destination: str | Path,
    bbox: tuple[float, float, float, float] = TEMPE_BBOX,
) -> dict[str, int]:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    keep_node_ids, keep_way_ids = _scan(source, bbox)
    _copy(source, destination, keep_node_ids, keep_way_ids)

    return {"nodes": len(keep_node_ids), "ways": len(keep_way_ids)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m geo_build.carve_fixture",
        description="Carve a small, deterministic .osm.pbf fixture out of a full Geofabrik "
        "extract: addr:*-tagged nodes/ways plus drivable highway=* ways and their nodes, "
        "inside a bounding box. Pure pyosmium read + SimpleWriter write, no osmium-tool CLI.",
    )
    parser.add_argument("source", help="path to the full .osm.pbf snapshot")
    parser.add_argument("destination", help="path to write the carved .osm.pbf to")
    parser.add_argument("--min-lon", type=float, default=TEMPE_BBOX[0])
    parser.add_argument("--min-lat", type=float, default=TEMPE_BBOX[1])
    parser.add_argument("--max-lon", type=float, default=TEMPE_BBOX[2])
    parser.add_argument("--max-lat", type=float, default=TEMPE_BBOX[3])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bbox = (args.min_lon, args.min_lat, args.max_lon, args.max_lat)
    counts = carve(args.source, args.destination, bbox)
    size = Path(args.destination).stat().st_size
    print(
        f"carve_fixture: kept {counts['nodes']} nodes / {counts['ways']} ways "
        f"-> {args.destination} ({size} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["TEMPE_BBOX", "DRIVABLE_HIGHWAY_VALUES", "build_parser", "carve", "main"]
