"""Wires every stage in order (research/osm-data.md §4.1-§4.7) into one
function, so :mod:`tools.geo_build.cli` — and CSR-003c/003g after it — call
one thing rather than re-deriving the stage order.

Numeric parameters (`geography.outlying_buffer_km`,
`geography.duplicate_coordinate_meters`) come from `csr_bench.config`'s
registry defaults with an optional scenario-JSON override (never a literal
duplicated here) — the same override path CSR-003d will use to lower the
floors for its tiny fixture build.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from csr_bench.address import load_default_abbreviation_table
from csr_bench.config import load_run_config

from . import crop, dedupe, eligibility, extract, normalize_stage, osm_reader, strip, zone_assign
from .funnel import FunnelCounts
from .models import Candidate, RejectedCandidate
from .serialize import (
    REJECTED_COLUMNS,
    assign_address_ids,
    rejected_to_row,
    sort_rejected,
    write_csv,
    write_funnel_csv,
    write_funnel_json,
    write_zone_geojson,
    write_zone_provenance,
)
from .strip import CATALOG_COLUMNS
from .zones import ZoneExtractionResult, extract_zones


@dataclass(frozen=True)
class PipelineResult:
    zone_result: ZoneExtractionResult
    catalog_rows: list[dict[str, object]]
    rejected_rows: list[RejectedCandidate]
    funnel: FunnelCounts


def run_pipeline(
    osm_path: str,
    *,
    dataset_version: str,
    scenario_json: str | bytes | None = None,
    abbreviation_table: dict | None = None,
) -> PipelineResult:
    """Run the full §4.1-§4.7 pipeline over ``osm_path``.

    ``dataset_version`` is the frozen artifact identity this build's rows are
    stamped with (03 §3.1's `dataset_version` column) — this sub-issue does
    not mint that identity (CSR-003g does, decision 7's footer), so callers
    (tests, the CLI, later sub-issues) must supply it explicitly.
    """
    config = load_run_config(scenario_json=scenario_json)
    outlying_buffer_km = config.get_decimal("geography.outlying_buffer_km")
    duplicate_coordinate_meters = config.get_decimal("geography.duplicate_coordinate_meters")
    if abbreviation_table is None:
        abbreviation_table = load_default_abbreviation_table()

    # §4.7's provenance half + the polygons every later stage needs.
    zone_result = extract_zones(osm_path)
    zone_polygons_in_order = zone_result.zone_polygons_in_order()

    # §4.1 crop.
    area = crop.build_area(zone_polygons_in_order, outlying_buffer_km)
    elements = osm_reader.read_elements(osm_path)
    cropped = crop.filter_to_build_area(elements, area)

    funnel = FunnelCounts()

    # §4.2 extract.
    ctx = extract.build_extraction_context(zone_result)
    candidates, rejected_extract = extract.extract_candidates(cropped, ctx)
    funnel.record_extraction(candidates, rejected_extract)

    # §4.3 normalize.
    normalized: list[Candidate] = []
    rejected_normalize: list[RejectedCandidate] = []
    for candidate in candidates:
        rejection = normalize_stage.normalize_candidate(candidate, abbreviation_table)
        if rejection is not None:
            rejected_normalize.append(rejection)
        else:
            normalized.append(candidate)
    funnel.record_rejections(rejected_normalize)

    # §4.4 dedupe.
    deduped, rejected_dedupe = dedupe.dedupe(normalized, duplicate_coordinate_meters)
    funnel.record_rejections(rejected_dedupe)

    # §4.5 eligibility (needs the raw tags §4.6 will then strip away).
    for candidate in deduped:
        candidate.segment_eligibility = eligibility.classify_eligibility(candidate.tags)

    # §4.7 zone assignment (final; extract's own preview computation for its
    # city-derivation fallback is independent of this).
    for candidate in deduped:
        zone_id, effective_zone_id = zone_assign.assign_zone(
            candidate.longitude, candidate.latitude, zone_polygons_in_order
        )
        candidate.zone_id = zone_id
        candidate.effective_zone_id = effective_zone_id

    # §4.6 strip + deterministic id assignment.
    raw_rows = [strip.to_catalog_row(c, dataset_version) for c in deduped]
    catalog_rows = assign_address_ids(raw_rows)
    funnel.record_final_accepted(len(catalog_rows))

    all_rejected = sort_rejected(rejected_extract + rejected_normalize + rejected_dedupe)

    return PipelineResult(
        zone_result=zone_result,
        catalog_rows=catalog_rows,
        rejected_rows=all_rejected,
        funnel=funnel,
    )


def write_artifact(result: PipelineResult, output_dir: Path) -> None:
    """Emit the sub-issue's slice of research/osm-data.md §6's artifact
    layout: `zones/`, `addresses/{catalog,rejected}.csv`, and
    `reports/funnel_counts.{csv,json}`. Routing (§4.8), `manifest.json`, the
    packaged `usps_abbreviations.json` copy, and `data_quality.md`/
    `ATTRIBUTION.md`/`LICENSE` are CSR-003c/003g.
    """
    zones_dir = output_dir / "zones"
    addresses_dir = output_dir / "addresses"
    reports_dir = output_dir / "reports"
    for d in (zones_dir, addresses_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    write_zone_geojson(zones_dir / "zone_definitions.geojson", result.zone_result)
    write_zone_provenance(zones_dir / "zone_provenance.json", result.zone_result)
    write_csv(addresses_dir / "catalog.csv", CATALOG_COLUMNS, result.catalog_rows)
    write_csv(
        addresses_dir / "rejected.csv",
        REJECTED_COLUMNS,
        [rejected_to_row(r) for r in result.rejected_rows],
    )
    write_funnel_csv(reports_dir / "funnel_counts.csv", result.funnel)
    write_funnel_json(reports_dir / "funnel_counts.json", result.funnel)


__all__ = ["PipelineResult", "run_pipeline", "write_artifact"]
