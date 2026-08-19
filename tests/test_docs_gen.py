from __future__ import annotations

import json
from pathlib import Path

import pytest

from _docs_engine import docs_engine, fire_example
from _helpers import register_conn
from trades_bench.db import in_memory
from trades_bench.docs_gen import (
    DOCS_FILENAME,
    VIEW_EXAMPLE_PREFIX,
    VIEW_SCHEMA_PREFIX,
    WORKED_EXAMPLE_PREFIX,
    WORKED_QUERIES,
    render_bytes,
    render_docs,
)
from trades_bench.migrations import migrate
from trades_bench.registry import OPERATIONS, OPERATIONS_BY_NAME
from trades_bench.views import VIEW_WHITELIST, VIEWS
from trades_bench.wire import WIRE_STATUS, WireCode

_FORBIDDEN_STRINGS = ("trades_bench", "trades-bench", "csr_bench", "bench")

_OPS_PATH_PREFIX = "/v1/ops/"

_SQLITE_JSON_TYPE = {
    "INTEGER": "integer",
    "REAL": "number",
    "TEXT": "string",
    "BLOB": "string",
    "NUMERIC": "number",
}

_EXPECTED_OUTCOMES: dict[str, str] = {
    "crm.get_customer": "not_found",
    "crm.list_customers": "success",
    "crm.create_customer": "success",
    "crm.get_location": "not_found",
    "crm.list_locations": "success",
    "crm.create_location": "success",
    "calls.get": "success",
    "calls.list": "success",
    "calls.reject": "success",
    "jobs.get": "not_found",
    "jobs.list": "success",
    "jobs.book": "conflict:call_not_active",
    "appointments.get": "not_found",
    "appointments.list": "success",
    "appointments.list_events": "success",
    "appointments.acknowledge_cancellation": "conflict:call_not_active",
    "dispatch.assign": "not_found",
    "dispatch.list_assignments": "success",
    "dispatch.get_technician_location": "success",
    "finance.get_cash_position": "success",
    "finance.list_invoices": "success",
    "finance.list_payments": "success",
    "finance.list_ledger": "success",
    "weather.get": "success",
    "maps.distance": "success",
}


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict:
    path = render_docs(tmp_path_factory.mktemp("docs"))
    return json.loads(path.read_bytes())


@pytest.fixture(scope="module")
def table_columns() -> dict[str, dict[str, tuple[str, bool]]]:
    conn = in_memory()
    register_conn(conn)
    migrate(conn)
    tables: dict[str, dict[str, tuple[str, bool]]] = {}
    names = [
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    ]
    for name in names:
        columns: dict[str, tuple[str, bool]] = {}
        for row in conn.execute(f"PRAGMA table_info({name})"):
            declared = str(row["type"]).upper()
            if declared not in _SQLITE_JSON_TYPE:
                continue
            nullable = not (row["notnull"] or row["pk"])
            columns[str(row["name"])] = (_SQLITE_JSON_TYPE[declared], nullable)
        tables[name] = columns
    return tables


def _operation_names(document: dict) -> list[str]:
    names = []
    for path in document["paths"]:
        assert path.startswith(_OPS_PATH_PREFIX), path
        names.append(path[len(_OPS_PATH_PREFIX) :])
    return names


def _documented_view_schemas(document: dict) -> list[str]:
    return [
        name[len(VIEW_SCHEMA_PREFIX) :]
        for name in document["components"]["schemas"]
        if name.startswith(VIEW_SCHEMA_PREFIX)
    ]


def _query_examples(document: dict) -> dict:
    post = document["paths"][f"{_OPS_PATH_PREFIX}query.sql"]["post"]
    return post["requestBody"]["content"]["application/json"]["examples"]


def _documented_view_examples(document: dict) -> list[str]:
    return [
        name[len(VIEW_EXAMPLE_PREFIX) :]
        for name in _query_examples(document)
        if name.startswith(VIEW_EXAMPLE_PREFIX)
    ]


def _type_members(schema: dict) -> set[str]:
    declared = schema["type"]
    if isinstance(declared, list):
        return set(declared)
    return {declared}


def test_render_docs_writes_one_parseable_openapi_file(tmp_path):
    path = render_docs(tmp_path)
    assert path.name == DOCS_FILENAME
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [DOCS_FILENAME]
    document = json.loads(path.read_bytes())
    assert document["openapi"] == "3.1.0"
    assert path.read_bytes().endswith(b"\n")


def test_every_registry_operation_is_documented_exactly_once(rendered):
    names = _operation_names(rendered)
    assert len(names) == len(set(names))
    assert set(names) == {spec.name for spec in OPERATIONS}


def test_every_documented_operation_exists_in_the_registry(rendered):
    for name in _operation_names(rendered):
        assert name in OPERATIONS_BY_NAME
        post = rendered["paths"][f"{_OPS_PATH_PREFIX}{name}"]["post"]
        assert post["operationId"] == name
        schema = post["requestBody"]["content"]["application/json"]["schema"]
        documented_args = set(schema["properties"])
        assert documented_args == {arg.name for arg in OPERATIONS_BY_NAME[name].args}


def test_every_whitelisted_view_is_a_component_schema_exactly_once(rendered):
    documented = _documented_view_schemas(rendered)
    assert len(documented) == len(set(documented))
    assert set(documented) == set(VIEW_WHITELIST)


def test_every_whitelisted_view_has_exactly_one_named_example_query(rendered):
    documented = _documented_view_examples(rendered)
    assert len(documented) == len(set(documented))
    assert set(documented) == set(VIEW_WHITELIST)


def test_view_schemas_carry_typed_columns_for_every_view_column(rendered, table_columns):
    for view in VIEWS:
        properties = rendered["components"]["schemas"][f"{VIEW_SCHEMA_PREFIX}{view.name}"][
            "properties"
        ]
        assert list(properties) == [column.name for column in view.columns]
        for column in view.columns:
            members = _type_members(properties[column.name])
            assert members <= {"boolean", "string", "integer", "number", "null"}
            if column.source is None:
                continue
            expected_type, expected_nullable = table_columns[column.source.table][
                column.source.column
            ]
            assert members - {"null"} == {expected_type}
            assert ("null" in members) == expected_nullable


def test_iso_twin_columns_mirror_their_epoch_column_nullability(rendered, table_columns):
    for view in VIEWS:
        properties = rendered["components"]["schemas"][f"{VIEW_SCHEMA_PREFIX}{view.name}"][
            "properties"
        ]
        for name, schema in properties.items():
            twin = name.removesuffix("_iso")
            if twin == name or twin not in properties:
                continue
            assert _type_members(schema) - {"null"} == {"string"}
            assert ("null" in _type_members(schema)) == ("null" in _type_members(properties[twin]))


def test_worked_queries_are_documented_as_named_examples(rendered):
    examples = _query_examples(rendered)
    for key, _summary, statement in WORKED_QUERIES:
        assert examples[f"{WORKED_EXAMPLE_PREFIX}{key}"]["value"]["statement"] == statement


def test_query_sql_documents_statement_and_max_rows_only(rendered):
    post = rendered["paths"][f"{_OPS_PATH_PREFIX}query.sql"]["post"]
    schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]) == {"statement", "max_rows"}
    assert schema["required"] == ["statement"]
    description = post["description"]
    assert "exactly one statement" in description
    assert "no parameter binding" in description.lower()


def test_error_catalog_covers_every_wire_code_grouped_by_status(rendered):
    for name in _operation_names(rendered):
        responses = rendered["paths"][f"{_OPS_PATH_PREFIX}{name}"]["post"]["responses"]
        statuses = {int(status) for status in responses} - {200}
        assert statuses == set(WIRE_STATUS.values())
        documented = " ".join(
            entry["description"] for status, entry in responses.items() if status != "200"
        )
        for code in WireCode:
            assert code.value in documented
        for status, entry in responses.items():
            if status == "200":
                continue
            schema = entry["content"]["application/json"]["schema"]
            assert schema["$ref"] == "#/components/schemas/FailureEnvelope"


def test_info_description_carries_the_wire_protocol_rules(rendered):
    description = rendered["info"]["description"]
    for fragment in (
        "SUNRIDGE_API_PORT",
        "SUNRIDGE_API_TOKEN",
        "request_id",
        "next_cursor",
        "X-CSR-Idempotent-Replay",
    ):
        assert fragment in description


def test_rendered_bytes_contain_no_benchmark_identifier(tmp_path):
    lowered = render_docs(tmp_path).read_bytes().lower()
    for forbidden in _FORBIDDEN_STRINGS:
        assert forbidden.encode("ascii") not in lowered


def test_two_renders_are_byte_identical(tmp_path):
    first = render_docs(tmp_path / "one").read_bytes()
    second = render_docs(tmp_path / "two").read_bytes()
    assert first == second
    assert first == render_bytes()


def test_rendered_document_carries_no_float(tmp_path):
    document = json.loads(render_docs(tmp_path).read_bytes())

    def walk(node) -> None:
        if isinstance(node, float):
            raise AssertionError(f"float in rendered document: {node!r}")
        if isinstance(node, dict):
            for key, value in node.items():
                walk(key)
                walk(value)
            return
        if isinstance(node, list):
            for value in node:
                walk(value)

    walk(document)


def test_pinned_outcomes_cover_every_non_sql_operation():
    assert set(_EXPECTED_OUTCOMES) == {spec.name for spec in OPERATIONS if spec.name != "query.sql"}


def test_every_documented_example_executes_with_its_pinned_outcome(tmp_path):
    document = json.loads(render_docs(tmp_path).read_bytes())
    with docs_engine(tmp_path / "engine") as engine:
        for path, item in document["paths"].items():
            examples = item["post"]["requestBody"]["content"]["application/json"]["examples"]
            for example in examples.values():
                status, body = fire_example(engine, path, example["value"])
                name = path[len(_OPS_PATH_PREFIX) :]
                expected = "success" if name == "query.sql" else _EXPECTED_OUTCOMES[name]
                if expected == "success":
                    assert status == 200, (path, body)
                    assert set(body.keys()) == {"data", "meta"}
                    continue
                code, _, rule = expected.partition(":")
                assert "data" not in body, (path, body)
                assert body["error"]["code"] == code, (path, body)
                assert status == WIRE_STATUS[WireCode(code)], (path, body)
                assert (body["error"].get("details") or {}).get("rule") == (rule or None), (
                    path,
                    body,
                )


def test_docs_directory_is_created_when_missing(tmp_path):
    target = tmp_path / "workspace" / "docs"
    assert not target.exists()
    path = render_docs(target)
    assert path == target / DOCS_FILENAME
    assert Path(path).is_file()
