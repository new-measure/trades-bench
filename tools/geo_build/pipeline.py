from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from csr_bench.address import load_default_abbreviation_table
from csr_bench.config import load_run_config
from csr_bench.geo_artifact import canonical_artifact_sha256, file_sha256

from . import (
    crop,
    dedupe,
    eligibility,
    extract,
    footprints,
    matrix,
    normalize_stage,
    osm_reader,
    routability,
    strip,
    zone_assign,
)
from .anchors import (
    DEFAULT_ANCHOR_CANDIDATE_K,
    DEFAULT_ANCHOR_GRID_KM,
    Anchor,
    AnchorGrid,
    assign_access_distances,
    renumber,
    select_anchors,
)
from .errors import GeoBuildError
from .funnel import FunnelCounts
from .gates import (
    DEFAULT_ACCURACY_MIN_EVALUATED_FRACTION,
    DEFAULT_ACCURACY_SAMPLE_SIZE,
    DEFAULT_ACCURACY_SEED,
    AccuracyGateResult,
    GateAddress,
    check_stratification,
    run_accuracy_gate,
)
from .licensing import write_license_files
from .manifest import SourceSnapshot, build_manifest, missing_required_fields
from .manifest import write_manifest as _write_manifest
from .models import Candidate, RejectedCandidate
from .routing import RoutingBackend
from .serialize import (
    assign_address_ids,
    sort_rejected,
    write_abbreviation_table,
    write_anchors_csv,
    write_csv,
    write_zone_geojson,
    write_zone_provenance,
)
from .strip import CATALOG_COLUMNS, PENDING_STATUS
from .zones import ZoneExtractionResult, extract_zones, load_zone_definitions


@dataclass(frozen=True)
class BuildParameters:

    anchor_grid_km: float = DEFAULT_ANCHOR_GRID_KM
    anchor_candidate_k: int = DEFAULT_ANCHOR_CANDIDATE_K
    accuracy_sample_size: int = DEFAULT_ACCURACY_SAMPLE_SIZE
    accuracy_seed: str = DEFAULT_ACCURACY_SEED
    accuracy_min_evaluated_fraction: float = DEFAULT_ACCURACY_MIN_EVALUATED_FRACTION


@dataclass(frozen=True)
class RouteSurface:

    anchors: list[Anchor]
    anchor_distance_km: list[list[float]]


@dataclass(frozen=True)
class RoutingResult:

    surface: RouteSurface
    accuracy: AccuracyGateResult

    @property
    def anchors(self) -> list[Anchor]:
        return self.surface.anchors

    @property
    def anchor_distance_km(self) -> list[list[float]]:
        return self.surface.anchor_distance_km


@dataclass(frozen=True)
class PipelineResult:
    zone_result: ZoneExtractionResult
    catalog_rows: list[dict[str, object]]
    rejected_rows: list[RejectedCandidate]
    funnel: FunnelCounts
    routing: RoutingResult
    dataset_version: str
    abbreviation_table: dict = field(default_factory=dict)
    duplicate_coordinate_meters: Decimal = Decimal("0")


def run_pipeline(
    osm_path: str,
    *,
    dataset_version: str,
    backend: RoutingBackend,
    scenario_json: str | bytes | None = None,
    abbreviation_table: dict | None = None,
    parameters: BuildParameters | None = None,
    zones_from_file: str | Path | None = None,
) -> PipelineResult:
    config = load_run_config(scenario_json=scenario_json)
    params = parameters or BuildParameters()
    outlying_buffer_km = config.get_decimal("geography.outlying_buffer_km")
    duplicate_coordinate_meters = config.get_decimal("geography.duplicate_coordinate_meters")
    max_snap_distance_meters = config.get_int("geography.max_snap_distance_meters")
    if abbreviation_table is None:
        abbreviation_table = load_default_abbreviation_table()

    zone_result = (
        load_zone_definitions(zones_from_file)
        if zones_from_file is not None
        else extract_zones(osm_path)
    )
    zone_polygons_in_order = zone_result.zone_polygons_in_order()

    area = crop.build_area(zone_polygons_in_order, outlying_buffer_km)
    elements = osm_reader.read_elements(osm_path)
    cropped = crop.filter_to_build_area(elements, area)

    funnel = FunnelCounts()

    ctx = extract.build_extraction_context(zone_result)
    candidates, rejected_extract = extract.extract_candidates(cropped, ctx)
    funnel.record_extraction(candidates, rejected_extract)

    normalized: list[Candidate] = []
    rejected_normalize: list[RejectedCandidate] = []
    for candidate in candidates:
        rejection = normalize_stage.normalize_candidate(candidate, abbreviation_table)
        if rejection is not None:
            rejected_normalize.append(rejection)
        else:
            normalized.append(candidate)
    funnel.record_rejections(rejected_normalize)

    deduped, rejected_dedupe = dedupe.dedupe(normalized, duplicate_coordinate_meters)
    funnel.record_rejections(rejected_dedupe)

    apartment_footprints = footprints.build_apartment_footprints(cropped)
    for candidate in deduped:
        candidate.segment_eligibility = eligibility.classify_eligibility(
            candidate.tags,
            unit_tag_present=candidate.unit_tag_present,
            inside_apartment_footprint=apartment_footprints.covers_point(
                candidate.longitude, candidate.latitude
            ),
        )

    for candidate in deduped:
        zone_id, effective_zone_id = zone_assign.assign_zone(
            candidate.longitude, candidate.latitude, zone_polygons_in_order
        )
        candidate.zone_id = zone_id
        candidate.effective_zone_id = effective_zone_id

    surface = _build_route_surface(
        deduped,
        area=area,
        backend=backend,
        params=params,
        max_snap_distance_meters=max_snap_distance_meters,
    )
    funnel.record_unusable(
        [c.unusable_reason for c in deduped if c.unusable_reason is not None]
    )

    raw_rows = [strip.to_catalog_row(c, dataset_version) for c in deduped]
    catalog_rows = assign_address_ids(raw_rows)
    funnel.record_final_accepted(sum(1 for c in deduped if c.status == routability.STATUS_USABLE))

    zone_ids = sorted(zone.zone_id for zone in zone_result.zones)
    check_stratification(catalog_rows, zone_ids, config)
    accuracy = run_accuracy_gate(
        [
            GateAddress(
                lon=c.snapped_lon,
                lat=c.snapped_lat,
                anchor_id=c.anchor_id,
                access_distance_km=c.access_distance_km,
            )
            for c in deduped
            if c.status == routability.STATUS_USABLE
        ],
        surface.anchor_distance_km,
        backend,
        config,
        sample_size=params.accuracy_sample_size,
        seed=params.accuracy_seed,
        min_evaluated_fraction=params.accuracy_min_evaluated_fraction,
    )
    routing_result = RoutingResult(surface=surface, accuracy=accuracy)

    all_rejected = sort_rejected(rejected_extract + rejected_normalize + rejected_dedupe)

    return PipelineResult(
        zone_result=zone_result,
        catalog_rows=catalog_rows,
        rejected_rows=all_rejected,
        funnel=funnel,
        routing=routing_result,
        dataset_version=dataset_version,
        abbreviation_table=abbreviation_table,
        duplicate_coordinate_meters=duplicate_coordinate_meters,
    )


def _build_route_surface(
    deduped: list[Candidate],
    *,
    area,
    backend: RoutingBackend,
    params: BuildParameters,
    max_snap_distance_meters: int,
) -> RouteSurface:
    routability.snap_candidates(
        deduped, backend, max_snap_distance_meters=max_snap_distance_meters
    )
    snapped = routability.usable(deduped)

    grid = AnchorGrid.over(area.bounds, params.anchor_grid_km)
    selected = select_anchors(
        grid, [(c.snapped_lon, c.snapped_lat) for c in snapped], backend
    )

    raw = matrix.build_anchor_matrix(selected, backend)
    keep = matrix.largest_mutually_routable_set(raw)
    anchor_set = renumber(selected, keep)
    raw_kept = [[raw[i][j] for j in keep] for i in keep]
    anchor_distance_km = matrix.quantized(matrix.symmetrize(raw_kept))

    assignments = assign_access_distances(
        [(c.snapped_lon, c.snapped_lat) for c in snapped],
        anchor_set,
        backend,
        grid=grid,
        k=params.anchor_candidate_k,
    )
    for candidate, assignment in zip(snapped, assignments, strict=True):
        if assignment.anchor_id is None or assignment.access_distance_km is None:
            routability.mark_unroutable(candidate)
        else:
            candidate.anchor_id = assignment.anchor_id
            candidate.access_distance_km = assignment.access_distance_km

    return RouteSurface(anchors=anchor_set, anchor_distance_km=anchor_distance_km)


def write_artifact(
    result: PipelineResult,
    output_dir: Path,
    *,
    artifact_id: str | None = None,
    source: SourceSnapshot | None = None,
) -> dict[str, object]:
    _refuse_pending(result)

    artifact_id = artifact_id or result.dataset_version
    source = source or SourceSnapshot()

    zones_dir = output_dir / "zones"
    addresses_dir = output_dir / "addresses"
    routing_dir = output_dir / "routing"
    normalization_dir = output_dir / "normalization"
    for d in (zones_dir, addresses_dir, routing_dir, normalization_dir):
        d.mkdir(parents=True, exist_ok=True)

    zone_definitions = zones_dir / "zone_definitions.geojson"
    write_zone_geojson(zone_definitions, result.zone_result)
    write_zone_provenance(zones_dir / "zone_provenance.json", result.zone_result)

    write_csv(addresses_dir / "catalog.csv", CATALOG_COLUMNS, result.catalog_rows)

    anchor_matrix_path = routing_dir / "anchor_distance.bin"
    write_anchors_csv(routing_dir / "anchors.csv", result.routing.anchors)
    matrix.write_packed_matrix(anchor_matrix_path, result.routing.anchor_distance_km)

    write_abbreviation_table(
        normalization_dir / "usps_abbreviations.json", result.abbreviation_table
    )

    write_license_files(output_dir)

    manifest = build_manifest(
        artifact_id=artifact_id,
        artifact_sha256=canonical_artifact_sha256(output_dir),
        source=source,
        zone_definitions_sha256=file_sha256(zone_definitions),
        catalog_rows=len(result.catalog_rows),
        anchor_count=len(result.routing.anchors),
    )
    missing = missing_required_fields(manifest)
    if missing:  # pragma: no cover - a build-time self-check, never expected
        raise GeoBuildError(
            "manifest is missing required research/osm-data.md §7 fields: "
            + ", ".join(missing)
        )
    _write_manifest(output_dir / "manifest.json", manifest)
    return manifest


def _refuse_pending(result: PipelineResult) -> None:
    pending = [row for row in result.catalog_rows if row.get("status") == PENDING_STATUS]
    if pending:
        raise GeoBuildError(
            f"{len(pending)} catalog row(s) still carry status={PENDING_STATUS!r}; "
            "every packaged address must be usable or explicitly unusable "
            "(03 §14 invariant 3)"
        )


__all__ = [
    "BuildParameters",
    "PipelineResult",
    "RouteSurface",
    "RoutingResult",
    "run_pipeline",
    "write_artifact",
]
