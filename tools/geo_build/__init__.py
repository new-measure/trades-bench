"""Build-time OSM geography pipeline (CSR-003b, Linear RES-60).

This package is **build tooling**, not a runtime dependency: it produces the
frozen geography artifact that `03-geography-and-scheduling.md` and
`research/osm-data.md` describe, but nothing under ``src/csr_bench`` imports
it, and it never ships inside the wheel (`pyproject.toml`'s
``[tool.hatch.build.targets.wheel]`` lists only ``src/csr_bench``). It imports
``csr_bench`` for the shared pieces build-time and run-time normalization must
never drift from: :func:`csr_bench.address.normalize`,
:data:`csr_bench.constants.ZONE_NAMES`, and the ``geography.*`` config
defaults (CSR-003 decision 13).

Dependencies (``osmium`` for reading ``.osm``/``.pbf`` and ``shapely`` for
polygon geometry) live in the uv dependency group ``geo``
(``uv sync --group geo``) — never in ``src/``'s zero-runtime-dependency
surface. Every module here does its own ``pytest.importorskip("osmium")`` /
``importorskip("shapely")`` at the top of its test file so ``uv run pytest``
without the group installed still collects (and passes) the whole suite
(CSR-003 decision 8's spirit extended to this sub-issue: geo tooling is
opt-in, never load-bearing for the default gate).

Pipeline stages (research/osm-data.md §4.1-§4.7; §4.8 routability is CSR-003c):

1. :mod:`tools.geo_build.zones` — administrative-relation zone polygons, the
   Central/North Phoenix split, disjointness.
2. :mod:`tools.geo_build.crop` — buffer the union of zones, filter to it.
3. :mod:`tools.geo_build.extract` — hard-tag address candidates + derivation.
4. :mod:`tools.geo_build.normalize_stage` — the shared ``normalize()`` call.
5. :mod:`tools.geo_build.dedupe` — ``duplicate_key`` / ``duplicate_coordinate``.
6. :mod:`tools.geo_build.eligibility` — the §4.5 tag -> eligibility table.
7. :mod:`tools.geo_build.zone_assign` — ``zone_id`` / ``effective_zone_id``.
8. :mod:`tools.geo_build.strip` — project to the 03 §3.1 column list.
9. :mod:`tools.geo_build.serialize` — deterministic sorted CSV/JSON/GeoJSON.
10. :mod:`tools.geo_build.funnel` — candidates -> accepted counts.
11. :mod:`tools.geo_build.pipeline` / :mod:`tools.geo_build.cli` — wiring.
"""

from __future__ import annotations

__all__: list[str] = []
