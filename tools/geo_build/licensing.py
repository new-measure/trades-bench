from __future__ import annotations

from pathlib import Path

LICENSE_ID = "ODbL-1.0"

ATTRIBUTION_LINE = (
    "Map data © OpenStreetMap contributors, available under the Open Database "
    "License (ODbL 1.0). Extract via Geofabrik GmbH."
)

LICENSE_TEXT_PATH = Path(__file__).resolve().parent / "data" / "odbl-1.0.txt"


def license_text() -> str:
    return LICENSE_TEXT_PATH.read_text(encoding="utf-8")


def attribution_markdown() -> str:
    return (
        "# Attribution\n"
        "\n"
        f"{ATTRIBUTION_LINE}\n"
        "\n"
        "This directory is a **derived database** of OpenStreetMap data: the\n"
        "address catalog, the zone assignment, the routing anchor set, and the\n"
        "anchor distance matrix are all produced from a pinned Geofabrik extract\n"
        "of Arizona (identified in `manifest.json` under `source`).\n"
        "\n"
        "Under the Open Database License 1.0 a derived database must retain the\n"
        "same license, so this artifact — and any bundle that ships it — is\n"
        "licensed under ODbL 1.0. The full text is in `LICENSE`.\n"
        "\n"
        "No OpenStreetMap contributor metadata (user names, user IDs, changeset\n"
        "ids) is present: the build strips every tag except the address columns\n"
        "published in `addresses/catalog.csv`. No real person is represented\n"
        "anywhere in this artifact.\n"
    )


def write_license_files(output_dir: Path) -> None:
    (output_dir / "LICENSE").write_text(license_text(), encoding="utf-8")
    (output_dir / "ATTRIBUTION.md").write_text(attribution_markdown(), encoding="utf-8")


__all__ = [
    "ATTRIBUTION_LINE",
    "LICENSE_ID",
    "LICENSE_TEXT_PATH",
    "attribution_markdown",
    "license_text",
    "write_license_files",
]
