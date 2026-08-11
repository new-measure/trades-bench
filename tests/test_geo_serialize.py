from __future__ import annotations

import csv

import pytest

pytest.importorskip("osmium")
pytest.importorskip("shapely")

from geo_build import serialize  # noqa: E402
from geo_build.models import RejectedCandidate  # noqa: E402


def test_assign_address_ids_sorts_by_canonical_key_and_assigns_1_based_ids():
    rows = [
        {"canonical_key": "200 MAIN ST"},
        {"canonical_key": "100 MAIN ST"},
        {"canonical_key": "150 MAIN ST"},
    ]
    ordered = serialize.assign_address_ids(rows)
    assert [r["canonical_key"] for r in ordered] == [
        "100 MAIN ST",
        "150 MAIN ST",
        "200 MAIN ST",
    ]
    assert [r["address_id"] for r in ordered] == [1, 2, 3]


def test_assign_address_ids_is_independent_of_input_order():
    a = {"canonical_key": "B"}
    b = {"canonical_key": "A"}
    ordered_1 = serialize.assign_address_ids([a, b])
    ordered_2 = serialize.assign_address_ids([b, a])
    assert [r["address_id"] for r in ordered_1] == [r["address_id"] for r in ordered_2] == [1, 2]
    assert ordered_1[0]["canonical_key"] == ordered_2[0]["canonical_key"] == "A"


def test_sort_rejected_orders_by_node_then_way_then_relation_then_id():
    rows = [
        RejectedCandidate("relation", 1, "x"),
        RejectedCandidate("node", 5, "x"),
        RejectedCandidate("way", 3, "x"),
        RejectedCandidate("node", 1, "x"),
    ]
    ordered = serialize.sort_rejected(rows)
    assert [(r.source_type, r.source_id) for r in ordered] == [
        ("node", 1),
        ("node", 5),
        ("way", 3),
        ("relation", 1),
    ]


def test_write_csv_uses_lf_line_endings_and_a_header(tmp_path):
    path = tmp_path / "out.csv"
    serialize.write_csv(path, ("a", "b"), [{"a": 1, "b": None}, {"a": 2, "b": "x"}])
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.count(b"\n") == 3
    text = raw.decode("utf-8")
    lines = text.splitlines()
    assert lines[0] == "a,b"
    assert lines[1] == "1,"
    assert lines[2] == "2,x"


def test_write_csv_quotes_fields_containing_the_delimiter(tmp_path):
    path = tmp_path / "out.csv"
    serialize.write_csv(path, ("a",), [{"a": "residential,apartment"}])
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[1] == ["residential,apartment"]


def test_write_csv_is_byte_identical_across_reruns(tmp_path):
    rows = [{"a": 2, "b": "y"}, {"a": 1, "b": "x"}]
    path1 = tmp_path / "out1.csv"
    path2 = tmp_path / "out2.csv"
    serialize.write_csv(path1, ("a", "b"), rows)
    serialize.write_csv(path2, ("a", "b"), rows)
    assert path1.read_bytes() == path2.read_bytes()


def test_rejected_to_row_shape():
    r = RejectedCandidate("node", 7, "incomplete_address", "missing tags: addr:postcode")
    row = serialize.rejected_to_row(r)
    assert row == {
        "source_type": "node",
        "source_id": 7,
        "reason": "incomplete_address",
        "detail": "missing tags: addr:postcode",
    }
    assert set(row.keys()) == set(serialize.REJECTED_COLUMNS)
