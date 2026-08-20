from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .migrations import MIGRATIONS_PACKAGE_DIR
from .registry import OPERATIONS, ArgType, OperationSpec
from .views import VIEWS, ViewSpec
from .wire import SPEC_VERSION, WIRE_MESSAGE_TEMPLATES, WIRE_STATUS, WireCode

DOCS_FILENAME = "openapi.json"
API_TITLE = "Sunridge Services Operations API"
VIEW_SCHEMA_PREFIX = "view."
VIEW_EXAMPLE_PREFIX = "view."
WORKED_EXAMPLE_PREFIX = "worked."
REQUEST_EXAMPLE_NAME = "request"

EXAMPLE_ADDRESS = "1 E Geneva Dr, Tempe, AZ 85282"
EXAMPLE_DESTINATION_ADDRESS = "100 E Geneva Dr, Tempe, AZ 85282"
EXAMPLE_SERVICE_DATE = "2026-10-01"
EXAMPLE_HORIZON_DATE = "2026-10-07"

_ISO_SUFFIX = "_iso"

_SQLITE_JSON_TYPE = {
    "INTEGER": "integer",
    "REAL": "number",
    "TEXT": "string",
    "BLOB": "string",
    "NUMERIC": "number",
}

_ARG_JSON_TYPE: dict[ArgType, str] = {
    "int": "integer",
    "str": "string",
    "date": "string",
    "list": "array",
    "object": "object",
    "bool": "boolean",
}

_CREATE_TABLE_NAME_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE
)
_ALTER_ADD_RE = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)\s+(\w+)", re.IGNORECASE)
_ALTER_DROP_RE = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+DROP\s+COLUMN\s+(\w+)", re.IGNORECASE)
_COLUMN_RE = re.compile(r"^(\w+)\s+(\w+)")

_NUMBER_FIELDS = frozenset(
    {"distance_km", "distance_miles", "latitude", "longitude", "precipitation_probability"}
)
_ARRAY_FIELDS = frozenset({"blocks", "columns", "contacts", "released_blocks", "rows"})
_BOOLEAN_FIELDS = frozenset({"storm", "truncated"})
_INTEGER_FIELDS = frozenset(
    {"high_f", "horizon_days", "limit", "low_f", "max_rows", "row_count", "trip_index"}
)
_INTEGER_SUFFIXES = ("_id", "_cents", "_ms", "_at", "_minutes", "_days", "_basis_points")

_EXAMPLE_ARGUMENTS: dict[str, object] = {
    "call_id": 1,
    "customer_id": 1,
    "location_id": 1,
    "job_id": 1,
    "job_type_id": 1,
    "appointment_id": 1,
    "technician_id": 1,
    "invoice_id": 1,
    "zone_id": 1,
    "limit": 5,
    "max_rows": 100,
    "name": "Example Customer",
    "segment": "residential",
    "contacts": [{"contact_type": "phone", "value": "(602) 555-0142"}],
    "address": EXAMPLE_ADDRESS,
    "service_date": EXAMPLE_SERVICE_DATE,
    "block_start": "09:00",
    "date": EXAMPLE_SERVICE_DATE,
    "date_from": EXAMPLE_SERVICE_DATE,
    "date_to": EXAMPLE_HORIZON_DATE,
    "origin_address": EXAMPLE_ADDRESS,
    "destination_address": EXAMPLE_DESTINATION_ADDRESS,
}

_EXAMPLE_TYPE_FALLBACK: dict[ArgType, object] = {
    "int": 1,
    "str": "example",
    "date": EXAMPLE_SERVICE_DATE,
    "list": [],
    "object": {},
    "bool": True,
}

WORKED_QUERIES: tuple[tuple[str, str, str], ...] = (
    (
        "commercial-hvac-invoicing",
        "What commercial HVAC installations have historically invoiced.",
        "SELECT c.segment, jt.name, COUNT(*) AS jobs,\n"
        "       AVG(i.total_cents) AS avg_total_cents,\n"
        "       MIN(i.total_cents) AS min_total_cents,\n"
        "       MAX(i.total_cents) AS max_total_cents\n"
        "FROM invoices i\n"
        "JOIN jobs j       ON j.id = i.job_id\n"
        "JOIN job_types jt ON jt.id = j.job_type_id\n"
        "JOIN customers c  ON c.id = i.customer_id\n"
        "WHERE i.no_charge = 0\n"
        "GROUP BY c.segment, jt.name\n"
        "ORDER BY avg_total_cents DESC;",
    ),
    (
        "tomorrow-board-one-technician",
        "Tomorrow's board for one technician. Appointments carry no service_date column, "
        "so the local day is derived from the epoch-millisecond scheduled_start; "
        "America/Phoenix is UTC-7 all year (no DST).",
        "SELECT a.id, a.scheduled_start, a.scheduled_end, a.status, l.street, l.city, l.zone_id\n"
        "FROM appointments a\n"
        "JOIN appointment_assignments aa ON aa.appointment_id = a.id "
        "AND aa.unassigned_at IS NULL\n"
        "JOIN jobs j      ON j.id = a.job_id\n"
        "JOIN locations l ON l.id = j.location_id\n"
        "WHERE aa.technician_id = 3\n"
        "  AND date(a.scheduled_start / 1000, 'unixepoch', '-7 hours') = '2026-10-02'\n"
        "ORDER BY a.scheduled_start;",
    ),
    (
        "notifications-newest-first",
        "Notifications, newest first (there is no read/unread state).",
        "SELECT created_at, notification_type, entity_type, entity_id, message\n"
        "FROM notifications\n"
        "ORDER BY created_at DESC;",
    ),
    (
        "cash-movement-by-category",
        "Yesterday's cash movement by category.",
        "SELECT entry_type, SUM(amount_cents) AS total_cents\n"
        "FROM ledger_entries\n"
        "WHERE service_date = '2026-10-01'\n"
        "GROUP BY entry_type;",
    ),
)

_SQL_RULES = (
    "Read-only SQL over the public views listed in this document's component schemas.",
    "Rules: exactly one statement, top level SELECT or WITH ... SELECT, parsed before execution.",
    "Every relation must be a public view or a CTE/derived table defined in the same "
    "statement; every column must resolve to one of those.",
    "SELECT * is allowed and expands to view columns only.",
    "No identifier may start with an underscore or match sqlite_% or pragma_%.",
    "Only arithmetic, string, date/time, CAST, COALESCE, CASE and the standard aggregates "
    "are callable; PRAGMA, EXPLAIN, ATTACH, transaction control, DDL and DML are rejected.",
    "Rows are capped by the engine; max_rows may only lower that cap, and truncation is "
    "always reported as truncated: true. There is no parameter binding: send one finished "
    "statement.",
)


@dataclass(frozen=True)
class ColumnType:
    json_type: str
    nullable: bool


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    buffer: list[str] = []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    parts.append("".join(buffer))
    return parts


def _declared_column_type(definition: str) -> ColumnType | None:
    match = _COLUMN_RE.match(definition.strip())
    if match is None:
        return None
    declared = match.group(2).upper()
    if declared not in _SQLITE_JSON_TYPE:
        return None
    upper = definition.upper()
    not_null = "NOT NULL" in upper or "PRIMARY KEY" in upper
    return ColumnType(json_type=_SQLITE_JSON_TYPE[declared], nullable=not not_null)


def schema_column_types() -> dict[str, dict[str, ColumnType]]:
    tables: dict[str, dict[str, ColumnType]] = {}
    for path in sorted(MIGRATIONS_PACKAGE_DIR.glob("*.sql")):
        source = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("--")
        )
        for raw_statement in source.split(";"):
            statement = raw_statement.strip()
            if not statement:
                continue
            upper = statement.upper()
            if upper.startswith("CREATE TABLE"):
                name_match = _CREATE_TABLE_NAME_RE.match(statement)
                if name_match is None or "(" not in statement:
                    continue
                body = statement[statement.index("(") + 1 : statement.rindex(")")]
                columns: dict[str, ColumnType] = {}
                for part in _split_top_level(body):
                    match = _COLUMN_RE.match(part.strip())
                    declared = _declared_column_type(part)
                    if match is None or declared is None:
                        continue
                    columns[match.group(1)] = declared
                tables[name_match.group(1)] = columns
                continue
            add_match = _ALTER_ADD_RE.match(statement)
            if add_match is not None:
                declared = _declared_column_type(statement[add_match.start(2) :])
                if declared is not None:
                    tables.setdefault(add_match.group(1), {})[add_match.group(2)] = declared
                continue
            drop_match = _ALTER_DROP_RE.match(statement)
            if drop_match is None:
                continue
            tables.get(drop_match.group(1), {}).pop(drop_match.group(2), None)
    return tables


def _json_type_for_name(name: str) -> str:
    if name.endswith(_ISO_SUFFIX):
        return "string"
    if name in _NUMBER_FIELDS:
        return "number"
    if name in _ARRAY_FIELDS:
        return "array"
    if name in _BOOLEAN_FIELDS:
        return "boolean"
    if name in _INTEGER_FIELDS:
        return "integer"
    if name.endswith(_INTEGER_SUFFIXES):
        return "integer"
    return "string"


def view_column_types() -> dict[str, dict[str, ColumnType]]:
    schema = schema_column_types()
    catalog: dict[str, dict[str, ColumnType]] = {}
    for view in VIEWS:
        passthrough: dict[str, ColumnType] = {}
        for column in view.columns:
            if column.source is None:
                continue
            declared = schema.get(column.source.table, {}).get(column.source.column)
            passthrough[column.name] = declared or ColumnType(
                json_type=_json_type_for_name(column.name), nullable=True
            )
        columns: dict[str, ColumnType] = {}
        for column in view.columns:
            if column.name in passthrough:
                columns[column.name] = passthrough[column.name]
                continue
            twin = passthrough.get(column.name.removesuffix(_ISO_SUFFIX))
            if column.name.endswith(_ISO_SUFFIX) and twin is not None:
                columns[column.name] = ColumnType(json_type="string", nullable=twin.nullable)
                continue
            columns[column.name] = ColumnType(
                json_type=_json_type_for_name(column.name), nullable=True
            )
        catalog[view.name] = columns
    return catalog


VIEW_COLUMN_TYPES: dict[str, dict[str, ColumnType]] = view_column_types()


def _type_schema(column: ColumnType) -> dict[str, object]:
    if column.nullable:
        return {"type": [column.json_type, "null"]}
    return {"type": column.json_type}


def _view_schema(view: ViewSpec) -> dict[str, object]:
    types = VIEW_COLUMN_TYPES[view.name]
    return {
        "type": "object",
        "title": view.name,
        "description": f"One row of the public {view.name} view.",
        "properties": {column.name: _type_schema(types[column.name]) for column in view.columns},
    }


def _response_field_schema(spec: OperationSpec) -> dict[str, object]:
    read = spec.read
    if read is not None and read.view and read.kind in frozenset({"view_get", "view_list"}):
        return {"$ref": f"#/components/schemas/{VIEW_SCHEMA_PREFIX}{read.view}"}
    view_types: dict[str, ColumnType] = {}
    if read is not None and read.view:
        view_types = VIEW_COLUMN_TYPES[read.view]
    return {
        "type": "object",
        "properties": {
            field: _type_schema(
                view_types.get(field)
                or ColumnType(json_type=_json_type_for_name(field), nullable=False)
            )
            for field in spec.response_fields
        },
    }


def _meta_schema(spec: OperationSpec) -> dict[str, object]:
    paginated = (
        spec.read is not None and spec.read.kind == "view_list" and not spec.read.unpaginated
    )
    if paginated:
        return {"$ref": "#/components/schemas/PaginatedMeta"}
    return {"$ref": "#/components/schemas/Meta"}


def _success_schema(spec: OperationSpec) -> dict[str, object]:
    data_schema = _response_field_schema(spec)
    if spec.read is not None and spec.read.kind == "view_list":
        data_schema = {"type": "array", "items": data_schema}
    return {
        "type": "object",
        "properties": {"data": data_schema, "meta": _meta_schema(spec)},
        "required": ["data", "meta"],
    }


def _request_schema(spec: OperationSpec) -> dict[str, object]:
    properties: dict[str, object] = {}
    required: list[str] = []
    for arg in spec.args:
        entry: dict[str, object] = {"type": _ARG_JSON_TYPE[arg.type]}
        if arg.type == "date":
            entry["format"] = "date"
        if arg.name == "request_id":
            entry["description"] = (
                "Caller-minted unique id for this mutation; reuse it verbatim on a retry."
            )
        properties[arg.name] = entry
        if arg.required:
            required.append(arg.name)
    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _example_value(spec: OperationSpec, name: str, arg_type: ArgType) -> object:
    if name == "request_id":
        return f"example-{spec.name}-1"
    if name in _EXAMPLE_ARGUMENTS:
        return _EXAMPLE_ARGUMENTS[name]
    return _EXAMPLE_TYPE_FALLBACK[arg_type]


def _example_body(spec: OperationSpec) -> dict[str, object]:
    body: dict[str, object] = {}
    for arg in spec.args:
        if not arg.required:
            continue
        body[arg.name] = _example_value(spec, arg.name, arg.type)
    for arg in spec.args:
        if arg.name == "limit" and not arg.required:
            body[arg.name] = _EXAMPLE_ARGUMENTS["limit"]
    return body


def _query_examples() -> dict[str, object]:
    examples: dict[str, object] = {}
    for key, summary, statement in WORKED_QUERIES:
        examples[f"{WORKED_EXAMPLE_PREFIX}{key}"] = {
            "summary": summary,
            "value": {"statement": statement},
        }
    for view in VIEWS:
        examples[f"{VIEW_EXAMPLE_PREFIX}{view.name}"] = {
            "summary": f"Sample rows from the {view.name} view.",
            "value": {"statement": f"SELECT * FROM {view.name} LIMIT 5", "max_rows": 5},
        }
    return examples


def _request_examples(spec: OperationSpec) -> dict[str, object]:
    if spec.name == "query.sql":
        return _query_examples()
    return {
        REQUEST_EXAMPLE_NAME: {
            "summary": f"Call {spec.name}.",
            "value": _example_body(spec),
        }
    }


def _operation_description(spec: OperationSpec) -> str:
    lines = [spec.doc]
    if spec.name == "query.sql":
        lines.extend(_SQL_RULES)
        return "\n".join(lines)
    if spec.kind == "mutation":
        lines.append(
            "Mutation: supply a request_id minted before the first send and reused on "
            "every retry of the same call."
        )
    if spec.terminal:
        lines.append(
            "Terminal call action: legal only against the currently active call, and only once."
        )
    if spec.read is not None and spec.read.kind == "view_list" and not spec.read.unpaginated:
        lines.append(
            "Paginated: pass cursor from meta.next_cursor to continue; rows are ordered "
            "by id ascending."
        )
    return "\n".join(lines)


def _error_responses() -> dict[str, object]:
    by_status: dict[int, list[WireCode]] = {}
    for code in WireCode:
        by_status.setdefault(WIRE_STATUS[code], []).append(code)
    responses: dict[str, object] = {}
    for status in sorted(by_status):
        codes = sorted(by_status[status], key=lambda item: item.value)
        description = "; ".join(f"{code.value}: {WIRE_MESSAGE_TEMPLATES[code]}" for code in codes)
        responses[str(status)] = {
            "description": description,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/FailureEnvelope"}}
            },
        }
    return responses


def _path_item(spec: OperationSpec) -> dict[str, object]:
    responses: dict[str, object] = {
        "200": {
            "description": "Success envelope.",
            "content": {"application/json": {"schema": _success_schema(spec)}},
        }
    }
    responses.update(_error_responses())
    return {
        "post": {
            "operationId": spec.name,
            "summary": spec.doc,
            "description": _operation_description(spec),
            "tags": [spec.namespace],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _request_schema(spec),
                        "examples": _request_examples(spec),
                    }
                },
            },
            "responses": responses,
        }
    }


def _info_description() -> str:
    lines = [
        "The Sunridge Services dispatch office runs entirely through this API.",
        "",
        "Base URL: http://127.0.0.1:$SUNRIDGE_API_PORT",
        "Auth: send Authorization: Bearer $SUNRIDGE_API_TOKEN on every request. "
        "Both values arrive in the environment; nothing else is a valid source for them.",
        "Every operation is POST /v1/ops/{name} with a JSON object body of arguments.",
        "",
        'Success envelope: {"data": ..., "meta": {"as_of", "invocation_id", "request_id", '
        '"spec_version"}}.',
        'Failure envelope: {"error": {"code", "message", "details"}, "meta": {"as_of"}}. '
        "A response carries data or error, never both.",
        "meta.as_of is the frozen office time of the open invocation, never the wall clock.",
        "",
        "request_id: the server never creates one for you. Before sending a mutation, "
        "invent a unique string yourself, record it, then send it as the request_id "
        'argument. Example: to reject call 17, send {"call_id": 17, "request_id": '
        '"reject-call-17"} - any unique string works, a UUID or a descriptive one. '
        "If the response is lost or times out, resend the exact same body with the exact "
        "same request_id: a replay returns the stored response body byte for byte with "
        "the header X-CSR-Idempotent-Replay: 1 and writes nothing new. A new action "
        "needs a new id; reusing an id with different arguments fails with "
        "request_id_conflict. Reads take no request_id.",
        "",
        "Pagination: list operations accept cursor and limit. Ordering is by id ascending, "
        "limit has an engine cap, and meta.next_cursor is an opaque token that is absent "
        "once the last page is returned. Paging is stable while an invocation is open.",
        "",
        "Money is always integer cents. Timestamps ending in _at are epoch milliseconds; "
        "their _iso twin is the same instant in local office time.",
    ]
    return "\n".join(lines)


def _components() -> dict[str, object]:
    schemas: dict[str, object] = {
        "Meta": {
            "type": "object",
            "properties": {
                "as_of": {"type": "string"},
                "invocation_id": {"type": "integer"},
                "request_id": {"type": "string"},
                "spec_version": {"type": "string"},
            },
        },
        "PaginatedMeta": {
            "type": "object",
            "properties": {
                "as_of": {"type": "string"},
                "invocation_id": {"type": "integer"},
                "request_id": {"type": "string"},
                "spec_version": {"type": "string"},
                "next_cursor": {"type": "string"},
            },
        },
        "Error": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "enum": [code.value for code in WireCode]},
                "message": {"type": "string"},
                "details": {"type": "object"},
            },
            "required": ["code", "message"],
        },
        "FailureEnvelope": {
            "type": "object",
            "properties": {
                "error": {"$ref": "#/components/schemas/Error"},
                "meta": {"type": "object", "properties": {"as_of": {"type": "string"}}},
            },
            "required": ["error", "meta"],
        },
    }
    for view in VIEWS:
        schemas[f"{VIEW_SCHEMA_PREFIX}{view.name}"] = _view_schema(view)
    return {
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "Per-run agent token from SUNRIDGE_API_TOKEN.",
            }
        },
        "schemas": schemas,
    }


def build_document() -> dict[str, object]:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": API_TITLE,
            "version": SPEC_VERSION,
            "description": _info_description(),
        },
        "servers": [
            {
                "url": "http://127.0.0.1:{port}",
                "description": "Local Sunridge Services engine.",
                "variables": {
                    "port": {
                        "default": "0",
                        "description": ("Value of the SUNRIDGE_API_PORT environment variable."),
                    }
                },
            }
        ],
        "security": [{"bearerAuth": []}],
        "paths": {f"/v1/ops/{spec.name}": _path_item(spec) for spec in OPERATIONS},
        "components": _components(),
    }


def render_bytes() -> bytes:
    document = build_document()
    return (json.dumps(document, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def render_docs(target_dir: str | Path) -> Path:
    directory = Path(target_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / DOCS_FILENAME
    path.write_bytes(render_bytes())
    return path


__all__ = [
    "API_TITLE",
    "DOCS_FILENAME",
    "VIEW_COLUMN_TYPES",
    "VIEW_EXAMPLE_PREFIX",
    "VIEW_SCHEMA_PREFIX",
    "WORKED_EXAMPLE_PREFIX",
    "WORKED_QUERIES",
    "ColumnType",
    "build_document",
    "render_bytes",
    "render_docs",
    "schema_column_types",
    "view_column_types",
]
