"""tools.geo_build.crop: build-area computation and element filtering
(research/osm-data.md §4.1; CSR-003b, RES-60).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from geo_build import crop, osm_reader, zones  # noqa: E402

FIXTURE = str(Path(__file__).parent / "fixtures" / "geo" / "pipeline_fixture.osm")


def _zone_polygons():
    return zones.extract_zones(FIXTURE).zone_polygons_in_order()


def test_build_area_contains_every_zone_polygon():
    polys = _zone_polygons()
    area = crop.build_area(polys, Decimal("15.0"))
    for poly in polys:
        assert area.covers(poly)


def test_build_area_grows_with_buffer_size():
    polys = _zone_polygons()
    small = crop.build_area(polys, Decimal("1.0"))
    large = crop.build_area(polys, Decimal("50.0"))
    assert large.area > small.area
    assert large.covers(small)


def test_filter_to_build_area_keeps_elements_inside_and_drops_far_outside():
    polys = _zone_polygons()
    area = crop.build_area(polys, Decimal("15.0"))
    elements = osm_reader.read_elements(FIXTURE)

    # Node 1009 sits >100km west of the crop; every address node inside a
    # zone should survive.
    ids_before = {n.osm_id for n in elements.nodes}
    assert 1009 in ids_before
    assert 1001 in ids_before

    cropped = crop.filter_to_build_area(elements, area)
    ids_after = {n.osm_id for n in cropped.nodes}
    assert 1009 not in ids_after
    assert 1001 in ids_after
    assert 1008 in ids_after  # the outlying-but-in-buffer gap point survives


def test_filter_to_build_area_is_stricter_with_a_tiny_buffer():
    polys = _zone_polygons()
    tiny_area = crop.build_area(polys, Decimal("0.01"))
    elements = osm_reader.read_elements(FIXTURE)
    cropped = crop.filter_to_build_area(elements, tiny_area)
    ids_after = {n.osm_id for n in cropped.nodes}
    # Node 1008 lives in the ~4km gap between Tempe and Scottsdale zones —
    # outside the service core, and now outside a near-zero buffer too.
    assert 1008 not in ids_after


def test_filter_to_build_area_preserves_ways_and_relations_inside():
    polys = _zone_polygons()
    area = crop.build_area(polys, Decimal("15.0"))
    elements = osm_reader.read_elements(FIXTURE)
    cropped = crop.filter_to_build_area(elements, area)
    # The apartment building way (2100) and every zone-boundary way/relation
    # sit well inside the crop.
    assert any(w.osm_id == 2100 for w in cropped.ways)
    assert len(cropped.relations) == len(elements.relations)
