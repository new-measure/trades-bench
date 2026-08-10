"""``python -m geo_build`` — the CLI skeleton (research/osm-data.md §7:
"a single scripted, non-interactive pipeline"). CSR-003c/003g extend this
with §4.8 routability and the full manifest/report/license bundle; this
sub-issue wires §4.1-§4.7 behind one ``build`` subcommand so later work
extends one entry point instead of inventing a second.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .pipeline import run_pipeline, write_artifact


def _build(args: argparse.Namespace) -> int:
    scenario_json: str | None = None
    if args.scenario:
        scenario_json = Path(args.scenario).read_text(encoding="utf-8")

    result = run_pipeline(
        args.osm_input,
        dataset_version=args.dataset_version,
        scenario_json=scenario_json,
    )
    output_dir = Path(args.output_dir)
    write_artifact(result, output_dir)

    funnel = result.funnel
    print(
        f"geo_build: {funnel.accepted} accepted / {funnel.candidates_total} candidates "
        f"({funnel.total_rejected()} rejected) -> {output_dir}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m geo_build",
        description="Build-time OSM geography pipeline (CSR-003, §4.1-§4.7).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build", help="run the full crop -> extract -> ... -> serialize pipeline"
    )
    build.add_argument("osm_input", help="path to a .osm or .osm.pbf source file")
    build.add_argument(
        "output_dir", help="directory to write the zones/addresses/reports artifact tree into"
    )
    build.add_argument(
        "--dataset-version",
        required=True,
        help="the dataset_version stamped onto every catalog row",
    )
    build.add_argument(
        "--scenario",
        default=None,
        help="optional scenario JSON overriding geography.* config defaults",
    )
    build.set_defaults(func=_build)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


__all__ = ["build_parser", "main"]
