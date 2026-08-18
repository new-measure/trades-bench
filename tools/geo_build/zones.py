from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import osmium
import osmium.geom
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import split, unary_union

from trades_bench import constants
from trades_bench.db import in_memory
from trades_bench.migrations import migrate

from . import geometry

ZONE_CODES: tuple[str, ...] = (
    "central_phoenix",
    "north_phoenix",
    "scottsdale",
    "tempe",
    "mesa",
    "chandler",
    "gilbert",
    "glendale",
    "peoria",
    "west_valley_goodyear",
)
assert len(ZONE_CODES) == len(constants.ZONE_NAMES) == 10

PHOENIX_SPLIT_LATITUDE = 33.553
PHOENIX_SPLIT_PROVENANCE_NOTE = "user-decided v1 constant"

_SINGLE_RELATION_ZONE_CODES: tuple[str, ...] = (
    "scottsdale",
    "tempe",
    "mesa",
    "chandler",
    "gilbert",
    "glendale",
    "peoria",
)

_PHOENIX_SPLIT_ZONE_CODES: tuple[str, str] = ("central_phoenix", "north_phoenix")

_COMPOSITE_ZONE_CODE = "west_valley_goodyear"


def seeded_zone_cities() -> dict[str, list[str]]:
    conn = in_memory()
    try:
        migrate(conn)
        rows = conn.execute("SELECT code, cities_json FROM _zones ORDER BY id").fetchall()
        return {row["code"]: json.loads(row["cities_json"]) for row in rows}
    finally:
        conn.close()


@dataclass(frozen=True)
class MunicipalRelation:

    name: str
    osm_relation_id: int
    polygon: BaseGeometry


@dataclass(frozen=True)
class ZonePolygon:

    zone_id: int
    code: str
    name: str
    polygon: BaseGeometry
    source_relation_ids: tuple[int, ...]


@dataclass(frozen=True)
class ZoneExtractionResult:
    zones: tuple[ZonePolygon, ...]
    municipal_relations: tuple[MunicipalRelation, ...]
    overlap_reconciliations: tuple[dict[str, object], ...] = ()

    def zone_polygons_in_order(self) -> list[BaseGeometry]:
        return [z.polygon for z in sorted(self.zones, key=lambda z: z.zone_id)]


_MUNICIPALITY_ADMIN_LEVEL = "8"


class _AdminAreaHandler(osmium.SimpleHandler):

    def __init__(self) -> None:
        super().__init__()
        self._factory = osmium.geom.GeoJSONFactory()
        self.relations_by_name: dict[str, MunicipalRelation] = {}

    def area(self, a: object) -> None:
        if a.from_way():  # type: ignore[attr-defined]
            return
        tags = dict(a.tags)  # type: ignore[attr-defined]
        if tags.get("boundary") != "administrative":
            return
        if tags.get("admin_level") != _MUNICIPALITY_ADMIN_LEVEL:
            return
        name = tags.get("name")
        if not name:
            return
        relation_id = a.orig_id()  # type: ignore[attr-defined]
        if name in self.relations_by_name:
            raise ValueError(
                f"duplicate admin_level={_MUNICIPALITY_ADMIN_LEVEL} relation name "
                f"{name!r}: relation {self.relations_by_name[name].osm_relation_id} and "
                f"relation {relation_id} both claim it"
            )
        label = f"relation {relation_id} ({name!r})"
        try:
            geojson_text = self._factory.create_multipolygon(a)  # type: ignore[attr-defined]
        except RuntimeError as exc:
            raise ValueError(f"{label}: pyosmium rejected the assembled area ({exc})") from exc
        polygon = shape(json.loads(geojson_text))
        polygon = geometry.validate_or_repair_polygon(polygon, label=label)
        self.relations_by_name[name] = MunicipalRelation(
            name=name,
            osm_relation_id=relation_id,
            polygon=polygon,
        )


def _read_admin_relations(osm_path: str) -> dict[str, MunicipalRelation]:
    handler = _AdminAreaHandler()
    handler.apply_file(osm_path, locations=True)
    return handler.relations_by_name


def extract_zones(osm_path: str) -> ZoneExtractionResult:
    seeded_cities = seeded_zone_cities()
    relations_by_name = _read_admin_relations(osm_path)

    def _require(name: str) -> MunicipalRelation:
        rel = relations_by_name.get(name)
        if rel is None:
            raise ValueError(
                f"no administrative relation named {name!r} found in {osm_path!r} "
                "(boundary=administrative, matching name tag required)"
            )
        return rel

    municipal_relations: list[MunicipalRelation] = []
    zones: list[ZonePolygon] = []
    code_to_zone_id = {code: i + 1 for i, code in enumerate(ZONE_CODES)}

    for code in _SINGLE_RELATION_ZONE_CODES:
        cities = seeded_cities[code]
        if len(cities) != 1:
            raise ValueError(
                f"zone {code!r}: expected exactly one seeded city name for a "
                f"single-relation zone, got {cities!r}"
            )
        rel = _require(cities[0])
        municipal_relations.append(rel)
        zones.append(
            ZonePolygon(
                zone_id=code_to_zone_id[code],
                code=code,
                name=constants.ZONE_NAMES[code_to_zone_id[code] - 1],
                polygon=rel.polygon,
                source_relation_ids=(rel.osm_relation_id,),
            )
        )

    central_cities = set(seeded_cities["central_phoenix"])
    north_cities = set(seeded_cities["north_phoenix"])
    shared = central_cities & north_cities
    if len(shared) != 1:
        raise ValueError(
            "central_phoenix and north_phoenix seeded cities_json must share "
            f"exactly one underlying municipality name, got {sorted(shared)!r}"
        )
    phoenix_name = next(iter(shared))
    phoenix_rel = _require(phoenix_name)
    municipal_relations.append(phoenix_rel)

    split_line_bounds = phoenix_rel.polygon.bounds
    from shapely.geometry import LineString

    span = max(1.0, split_line_bounds[2] - split_line_bounds[0])
    splitter = LineString(
        [
            (split_line_bounds[0] - span, PHOENIX_SPLIT_LATITUDE),
            (split_line_bounds[2] + span, PHOENIX_SPLIT_LATITUDE),
        ]
    )
    pieces = list(split(phoenix_rel.polygon, splitter).geoms)
    south_pieces = [g for g in pieces if g.centroid.y < PHOENIX_SPLIT_LATITUDE]
    north_pieces = [g for g in pieces if g.centroid.y >= PHOENIX_SPLIT_LATITUDE]
    if not south_pieces or not north_pieces:
        raise ValueError(
            f"the Phoenix relation ({phoenix_rel.osm_relation_id}) does not "
            f"straddle the pinned split latitude {PHOENIX_SPLIT_LATITUDE}"
        )
    central_polygon = unary_union(south_pieces)
    north_polygon = unary_union(north_pieces)
    for code, polygon in (("central_phoenix", central_polygon), ("north_phoenix", north_polygon)):
        zones.append(
            ZonePolygon(
                zone_id=code_to_zone_id[code],
                code=code,
                name=constants.ZONE_NAMES[code_to_zone_id[code] - 1],
                polygon=polygon,
                source_relation_ids=(phoenix_rel.osm_relation_id,),
            )
        )

    component_names = seeded_cities[_COMPOSITE_ZONE_CODE]
    component_rels = [_require(n) for n in component_names]
    municipal_relations.extend(component_rels)
    composite_polygon = unary_union([r.polygon for r in component_rels])
    zones.append(
        ZonePolygon(
            zone_id=code_to_zone_id[_COMPOSITE_ZONE_CODE],
            code=_COMPOSITE_ZONE_CODE,
            name=constants.ZONE_NAMES[code_to_zone_id[_COMPOSITE_ZONE_CODE] - 1],
            polygon=composite_polygon,
            source_relation_ids=tuple(r.osm_relation_id for r in component_rels),
        )
    )

    ordered = sorted(zones, key=lambda z: z.zone_id)
    reconciled_polygons, overlap_reconciliations = geometry.reconcile_pairwise_overlaps(
        [(z.code, z.polygon) for z in ordered]
    )
    ordered = [replace(z, polygon=reconciled_polygons[z.code]) for z in ordered]

    geometry.assert_pairwise_disjoint({z.code: z.polygon for z in ordered})

    return ZoneExtractionResult(
        zones=tuple(ordered),
        municipal_relations=tuple(municipal_relations),
        overlap_reconciliations=tuple(overlap_reconciliations),
    )


ZONE_PROVENANCE_FILENAME = "zone_provenance.json"


def load_zone_definitions(
    geojson_path: str | Path, provenance_path: str | Path | None = None
) -> ZoneExtractionResult:
    geojson_path = Path(geojson_path)
    if provenance_path is None:
        provenance_path = geojson_path.parent / ZONE_PROVENANCE_FILENAME
    else:
        provenance_path = Path(provenance_path)

    payload = json.loads(geojson_path.read_text(encoding="utf-8"))
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    source_relation_ids_by_zone_id = {
        int(entry["zone_id"]): tuple(int(r) for r in entry["source_relation_ids"])
        for entry in provenance["zones"]
    }

    zones: list[ZonePolygon] = []
    for feature in payload["features"]:
        props = feature["properties"]
        zone_id = int(props["zone_id"])
        code = str(props["code"])
        polygon = geometry.validate_or_repair_polygon(
            shape(feature["geometry"]), label=f"zones-from-file zone {code!r}"
        )
        zones.append(
            ZonePolygon(
                zone_id=zone_id,
                code=code,
                name=str(props["name"]),
                polygon=polygon,
                source_relation_ids=source_relation_ids_by_zone_id[zone_id],
            )
        )
    zones.sort(key=lambda z: z.zone_id)
    reconciled_polygons, overlap_reconciliations = geometry.reconcile_pairwise_overlaps(
        [(z.code, z.polygon) for z in zones]
    )
    zones = [replace(z, polygon=reconciled_polygons[z.code]) for z in zones]
    geometry.assert_pairwise_disjoint({z.code: z.polygon for z in zones})

    municipal_relations = tuple(
        MunicipalRelation(
            name=zone.name,
            osm_relation_id=zone.source_relation_ids[0],
            polygon=zone.polygon,
        )
        for zone in zones
    )
    return ZoneExtractionResult(
        zones=tuple(zones),
        municipal_relations=municipal_relations,
        overlap_reconciliations=tuple(overlap_reconciliations),
    )


__all__ = [
    "PHOENIX_SPLIT_LATITUDE",
    "PHOENIX_SPLIT_PROVENANCE_NOTE",
    "ZONE_CODES",
    "ZONE_PROVENANCE_FILENAME",
    "MunicipalRelation",
    "ZoneExtractionResult",
    "ZonePolygon",
    "extract_zones",
    "load_zone_definitions",
    "seeded_zone_cities",
]
