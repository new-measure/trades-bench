"""Zone polygon extraction (research/osm-data.md §4.7 provenance half; CSR-003
decision 6): the eight municipal-relation zones, the composite
``west_valley_goodyear``, and the Central/North Phoenix split — plus the
"which raw municipal relation contains this point" lookup that
:mod:`tools.geo_build.extract` needs for §4.2's ``city`` derivation (a
different, finer-grained thing than the ten *zone* polygons — the seeded
``cities_json`` shows why: a Goodyear address should derive ``city ==
"Goodyear"``, not the zone name ``"West Valley/Goodyear"``, even though both
resolve to the same ``zone_id``).

pyosmium assembles administrative-relation ring geometry automatically: when
a handler defines an ``area()`` callback, ``apply_file(..., locations=True)``
does the two-pass multipolygon assembly internally (verified empirically —
see the CSR-003.md implementation note) and hands back an ``Area`` whose
``orig_id()`` is the *relation's* id and whose tags are the relation's tags.
:func:`osmium.geom.GeoJSONFactory.create_multipolygon` turns that into a
GeoJSON string this module feeds straight to ``shapely.geometry.shape`` — no
hand-rolled ring/multi-segment merging needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import osmium
import osmium.geom
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import split, unary_union

from csr_bench import constants
from csr_bench.db import in_memory
from csr_bench.migrations import migrate

from . import geometry

#: 03 §2.1's canonical zone order/codes, mirrored here so callers never
#: recompute it. Asserted against `constants.ZONE_NAMES` (same order, same
#: count) and against the migrated `_zones.code` column in tests — this is
#: the one place geo_build hardcodes the taxonomy shared with the rest of the
#: engine.
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

#: Central Phoenix / North Phoenix dividing line (CSR-003 decision 6):
#: "pinned latitude line at Northern Ave (~33.553°N)". Not an OSM boundary —
#: recorded here as "user-decided v1 constant" per the same decision, and
#: reproduced verbatim in every emitted `zone_provenance.json`.
PHOENIX_SPLIT_LATITUDE = 33.553
PHOENIX_SPLIT_PROVENANCE_NOTE = "user-decided v1 constant"

#: Zone codes sourced from exactly one OSM administrative relation each
#: (research/osm-data.md §4.7 / CSR-003 decision 6's "the other eight zones
#: come from OSM municipal admin relations" — eight, counting Phoenix's own
#: relation before the split; the codes below are the seven that are *also*
#: single zones with no split or union).
_SINGLE_RELATION_ZONE_CODES: tuple[str, ...] = (
    "scottsdale",
    "tempe",
    "mesa",
    "chandler",
    "gilbert",
    "glendale",
    "peoria",
)

#: The zone code whose relation is split rather than used whole.
_PHOENIX_SPLIT_ZONE_CODES: tuple[str, str] = ("central_phoenix", "north_phoenix")

#: The composite zone code, built from a union of several relations.
_COMPOSITE_ZONE_CODE = "west_valley_goodyear"


def seeded_zone_cities() -> dict[str, list[str]]:
    """``zone code -> cities_json`` list, read from a freshly migrated
    in-memory database — the seeded ``_zones`` rows of
    ``schema/0001_baseline.sql:226`` are decision 6's "composition source of
    truth" for ``west_valley_goodyear``, and this module reuses the same rows
    to name every other zone's expected OSM relation(s) rather than
    hardcoding city names a second time.
    """
    conn = in_memory()
    try:
        migrate(conn)
        rows = conn.execute("SELECT code, cities_json FROM _zones ORDER BY id").fetchall()
        return {row["code"]: json.loads(row["cities_json"]) for row in rows}
    finally:
        conn.close()


@dataclass(frozen=True)
class MunicipalRelation:
    """One raw OSM administrative relation, before any zone-level split or
    union — the granularity §4.2 city derivation needs (e.g. "Goodyear", not
    "West Valley/Goodyear").
    """

    name: str
    osm_relation_id: int
    polygon: BaseGeometry


@dataclass(frozen=True)
class ZonePolygon:
    """One of the ten final 03 §2.1 zones."""

    zone_id: int
    code: str
    name: str
    polygon: BaseGeometry
    #: Source OSM relation ids that fed this zone (1 for the seven singles, 1
    #: for each Phoenix half, 3 for the composite).
    source_relation_ids: tuple[int, ...]


@dataclass(frozen=True)
class ZoneExtractionResult:
    zones: tuple[ZonePolygon, ...]
    municipal_relations: tuple[MunicipalRelation, ...]

    def zone_polygons_in_order(self) -> list[BaseGeometry]:
        """Zone polygons ordered by ``zone_id`` (1..10) — the order every
        containment/nearest-zone lookup in :mod:`tools.geo_build.zone_assign`
        relies on for its "ties broken by lowest zone_id" rule.
        """
        return [z.polygon for z in sorted(self.zones, key=lambda z: z.zone_id)]


#: US incorporated-municipality admin_level (OSM tagging convention). Without
#: this filter, county (`admin_level=6`) and state (`admin_level=4`)
#: relations sharing a name-adjacent tag would also be collected — real
#: Geofabrik extracts carry all of these layered on the same territory.
_MUNICIPALITY_ADMIN_LEVEL = "8"


class _AdminAreaHandler(osmium.SimpleHandler):
    """Collects every administrative-boundary relation's assembled polygon.

    Filters on ``a.from_way() is False`` — pyosmium's area assembler also
    independently re-emits any closed way that happens to satisfy its own
    "looks like an area" tag heuristic (verified empirically: a closed way
    tagged ``area=yes`` gets its own ``Area`` alongside the relation's). This
    module only wants the relation-derived polygon, further filtered to
    ``admin_level=8`` (incorporated municipalities) — an unfiltered read
    would also collect county/state relations, which commonly reuse or
    contain the same names. A duplicate name *within* the filtered
    (municipality-only) set is a build error, not a silent overwrite: two
    same-named incorporated municipalities in one build area would make
    `extract_zones`'s name-based lookup ambiguous with no way to tell which
    one was kept.
    """

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
            # pyosmium's own area assembler does its own ring-validity check
            # and can reject a self-intersecting ring (a "bowtie") before
            # shapely ever sees it — verified empirically: reordering one
            # rectangle way's corners into a bowtie raises exactly this,
            # "invalid area (area_id=...)", with no relation identity in the
            # message. Re-raised here as the same clear, relation-naming
            # error `validate_or_repair_polygon` gives for the geometries
            # that *do* reach shapely — the build must fail loudly either
            # way, not with a bare, unlabeled RuntimeError.
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
    """Build all ten 03 §2.1 zone polygons from the administrative relations
    found in ``osm_path``.

    Raises ``ValueError`` naming the missing relation if an expected
    municipality is absent, or (via :func:`tools.geo_build.geometry.assert_pairwise_disjoint`)
    if the resulting zones are not pairwise disjoint.
    """
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

    # -- the seven single-relation zones --------------------------------
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

    # -- the Central/North Phoenix split ---------------------------------
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

    split_line_bounds = phoenix_rel.polygon.bounds  # (minx, miny, maxx, maxy)
    # A generously overlong east-west line at the pinned latitude — must
    # fully cross the polygon regardless of its longitude extent.
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

    # -- the composite west_valley_goodyear -------------------------------
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

    geometry.assert_pairwise_disjoint({z.code: z.polygon for z in zones})

    return ZoneExtractionResult(
        zones=tuple(zones),
        municipal_relations=tuple(municipal_relations),
    )


__all__ = [
    "PHOENIX_SPLIT_LATITUDE",
    "PHOENIX_SPLIT_PROVENANCE_NOTE",
    "ZONE_CODES",
    "MunicipalRelation",
    "ZoneExtractionResult",
    "ZonePolygon",
    "extract_zones",
    "seeded_zone_cities",
]
