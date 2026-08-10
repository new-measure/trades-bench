"""tools.geo_build.funnel: candidates -> accepted counting and the
tag-provenance breakdown (research/osm-data.md §8 items 2-3; CSR-003b,
RES-60).
"""

from __future__ import annotations

import pytest

pytest.importorskip("shapely")

from geo_build.funnel import FunnelCounts  # noqa: E402
from geo_build.models import Candidate, RejectedCandidate  # noqa: E402


def _candidate(city_source="tag", state_source="crop"):
    return Candidate(
        source_type="node",
        source_id=1,
        longitude=0,
        latitude=0,
        house_number_raw="1",
        street_raw="s",
        postal_code_raw="85251",
        city="x",
        city_source=city_source,
        state="AZ",
        state_source=state_source,
    )


def test_record_extraction_counts_candidates_and_rejections():
    funnel = FunnelCounts()
    candidates = [_candidate(), _candidate(city_source="admin_relation")]
    rejected = [RejectedCandidate("node", 2, "incomplete_address")]
    funnel.record_extraction(candidates, rejected)
    assert funnel.candidates_total == 3
    assert funnel.rejected_by_reason == {"incomplete_address": 1}
    assert funnel.city_source_counts == {"tag": 1, "admin_relation": 1}
    assert funnel.state_source_counts == {"crop": 2}


def test_record_rejections_only_tallies_reason_not_candidates_total():
    funnel = FunnelCounts()
    funnel.record_extraction([_candidate()], [])
    assert funnel.candidates_total == 1
    funnel.record_rejections([RejectedCandidate("node", 1, "duplicate_key")])
    assert funnel.candidates_total == 1  # unchanged
    assert funnel.rejected_by_reason == {"duplicate_key": 1}


def test_is_balanced_holds_across_the_whole_funnel():
    funnel = FunnelCounts()
    candidates = [_candidate(), _candidate(), _candidate()]
    rejected_extract = [RejectedCandidate("node", 9, "incomplete_address")]
    funnel.record_extraction(candidates, rejected_extract)
    # one of the three survivors gets rejected at a later stage.
    funnel.record_rejections([RejectedCandidate("node", 1, "duplicate_key")])
    funnel.record_final_accepted(2)
    assert funnel.is_balanced()
    assert funnel.candidates_total == 4
    assert funnel.total_rejected() == 2


def test_is_balanced_fails_if_final_accepted_is_wrong():
    funnel = FunnelCounts()
    funnel.record_extraction([_candidate()], [])
    funnel.record_final_accepted(0)  # should have been 1
    assert not funnel.is_balanced()


def test_to_dict_is_sorted_and_json_ready():
    funnel = FunnelCounts()
    funnel.record_extraction(
        [_candidate(city_source="zone"), _candidate(city_source="admin_relation")],
        [RejectedCandidate("node", 9, "state_mismatch"), RejectedCandidate("way", 8, "duplicate_key")],
    )
    funnel.record_final_accepted(2)
    d = funnel.to_dict()
    assert d["candidates_total"] == 4
    assert d["accepted"] == 2
    assert list(d["rejected_by_reason"].keys()) == sorted(d["rejected_by_reason"])
    assert list(d["city_source_counts"].keys()) == sorted(d["city_source_counts"])
    import json

    json.dumps(d)  # must not raise
