from __future__ import annotations

import argparse
from contextlib import nullcontext
from pathlib import Path

from .anchors import DEFAULT_ANCHOR_CANDIDATE_K, DEFAULT_ANCHOR_GRID_KM
from .gates import (
    DEFAULT_ACCURACY_MIN_EVALUATED_FRACTION,
    DEFAULT_ACCURACY_SAMPLE_SIZE,
    DEFAULT_ACCURACY_SEED,
)
from .manifest import PLACEHOLDER, SourceSnapshot
from .osrm import OsrmService
from .pipeline import BuildParameters, run_pipeline, write_artifact
from .routing import FakeBackend


def _build(args: argparse.Namespace) -> int:
    scenario_json: str | None = None
    if args.scenario:
        scenario_json = Path(args.scenario).read_text(encoding="utf-8")

    parameters = BuildParameters(
        anchor_grid_km=args.anchor_grid_km,
        anchor_candidate_k=args.anchor_candidate_k,
        accuracy_sample_size=args.accuracy_sample_size,
        accuracy_seed=args.accuracy_seed,
        accuracy_min_evaluated_fraction=args.accuracy_min_evaluated_fraction,
    )
    source = SourceSnapshot(
        url=args.source_url or PLACEHOLDER,
        published_at=args.source_published_at or PLACEHOLDER,
        bytes=args.source_bytes or 0,
        sha256=args.source_sha256 or PLACEHOLDER,
    )

    context = (
        nullcontext(FakeBackend()) if args.fake_backend else OsrmService(args.osm_input)
    )
    output_dir = Path(args.output_dir)
    with context as backend:
        result = run_pipeline(
            args.osm_input,
            dataset_version=args.dataset_version,
            backend=backend,
            scenario_json=scenario_json,
            parameters=parameters,
            zones_from_file=args.zones_from_file,
        )
        manifest = write_artifact(
            result,
            output_dir,
            artifact_id=args.artifact_id or args.dataset_version,
            source=source,
        )

    funnel = result.funnel
    accuracy = result.routing.accuracy
    print(
        f"geo_build: {funnel.accepted} usable / {funnel.candidates_total} candidates "
        f"({funnel.total_rejected()} rejected), {len(result.routing.anchors)} anchors, "
        f"accuracy median {accuracy.median_pct:.2f}% / p95 {accuracy.p95_pct:.2f}% -> "
        f"{output_dir}"
    )
    print(f"geo_build: artifact_sha256={manifest['artifact_sha256']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m geo_build",
        description="Build-time OSM geography pipeline (CSR-003).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build", help="run the full crop -> ... -> routing -> gates -> package pipeline"
    )
    build.add_argument("osm_input", help="path to a .osm or .osm.pbf source file")
    build.add_argument(
        "output_dir", help="directory to write the full §6 artifact tree into"
    )
    build.add_argument(
        "--dataset-version",
        required=True,
        help="the dataset_version stamped onto every catalog row",
    )
    build.add_argument(
        "--artifact-id",
        default=None,
        help="manifest artifact_id (defaults to --dataset-version)",
    )
    build.add_argument(
        "--scenario",
        default=None,
        help="optional scenario JSON overriding geography.* config defaults",
    )
    build.add_argument(
        "--zones-from-file",
        default=None,
        help="load zone_definitions.geojson (with a sibling zone_provenance.json) "
        "instead of extracting the ten municipal relations from osm_input; needed "
        "whenever osm_input does not carry all ten (e.g. a small carved fixture)",
    )

    routing = build.add_argument_group("routing backend")
    routing.add_argument(
        "--fake-backend",
        action="store_true",
        help="use the deterministic synthetic backend instead of Dockerized OSRM; "
        "straight-line distances only, for fast local iteration",
    )
    routing.add_argument(
        "--anchor-grid-km",
        type=float,
        default=DEFAULT_ANCHOR_GRID_KM,
        help=f"anchor grid cell size in km (default {DEFAULT_ANCHOR_GRID_KM})",
    )
    routing.add_argument(
        "--anchor-candidate-k",
        type=int,
        default=DEFAULT_ANCHOR_CANDIDATE_K,
        help="how many straight-line-nearest anchors each address routes against "
        f"(default {DEFAULT_ANCHOR_CANDIDATE_K})",
    )

    gate = build.add_argument_group("accuracy gate (§5.3)")
    gate.add_argument(
        "--accuracy-sample-size",
        type=int,
        default=DEFAULT_ACCURACY_SAMPLE_SIZE,
        help=f"held-out address pairs to measure (default {DEFAULT_ACCURACY_SAMPLE_SIZE})",
    )
    gate.add_argument(
        "--accuracy-seed",
        default=DEFAULT_ACCURACY_SEED,
        help="seed for the held-out sample; recorded in the manifest and the report",
    )
    gate.add_argument(
        "--accuracy-min-evaluated-fraction",
        type=float,
        default=DEFAULT_ACCURACY_MIN_EVALUATED_FRACTION,
        help="share of the drawn sample that must actually produce a measurement "
        "before the gate may pass; a gate that skipped every pair would otherwise "
        f"pass on an empty distribution (default {DEFAULT_ACCURACY_MIN_EVALUATED_FRACTION})",
    )

    snapshot = build.add_argument_group("pinned source snapshot (§2, manifest §7)")
    snapshot.add_argument("--source-url", default=None, help="resolved Geofabrik download URL")
    snapshot.add_argument(
        "--source-published-at", default=None, help="server-reported publication date"
    )
    snapshot.add_argument("--source-bytes", type=int, default=None, help="downloaded byte size")
    snapshot.add_argument("--source-sha256", default=None, help="SHA-256 of the downloaded file")

    build.set_defaults(func=_build)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


__all__ = ["build_parser", "main"]
