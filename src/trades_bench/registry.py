from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from .constants import PHOENIX_SQLITE_UTC_OFFSET
from .enums import VisibilityClass
from .views import VIEW_WHITELIST, VIEWS

ArgType = Literal["int", "str", "date", "list", "object", "bool"]
OpKind = Literal["read", "mutation"]
ReadKind = Literal["view_get", "view_list", "view_singleton", "sql", "weather", "maps"]


class FilterOp(StrEnum):
    EQ = "eq"
    DATE_GTE = "date_gte"
    DATE_LTE = "date_lte"
    LIKE = "like"


@dataclass(frozen=True)
class ArgSpec:
    name: str
    type: ArgType
    required: bool = True


@dataclass(frozen=True)
class FilterSpec:
    arg_name: str
    column: str
    type: ArgType
    op: FilterOp = FilterOp.EQ
    required: bool = False


@dataclass(frozen=True)
class ReadSpec:
    kind: ReadKind
    view: str | None = None
    pk_column: str | None = None
    get_id_arg: str | None = None
    filters: tuple[FilterSpec, ...] = ()
    singleton_fields: tuple[str, ...] = ()
    unpaginated: bool = False


@dataclass(frozen=True)
class OperationSpec:
    name: str
    namespace: str
    operation: str
    kind: OpKind
    args: tuple[ArgSpec, ...]
    response_fields: tuple[str, ...]
    visibility_class: VisibilityClass
    doc: str
    read: ReadSpec | None = None
    terminal: bool = False
    sql_positional_arg: str | None = None
    mutation: bool = False


def _arg(name: str, type: ArgType, *, required: bool = True) -> ArgSpec:
    return ArgSpec(name=name, type=type, required=required)


def _filter(
    arg_name: str,
    column: str,
    type: ArgType,
    *,
    op: FilterOp = FilterOp.EQ,
    required: bool = False,
) -> FilterSpec:
    return FilterSpec(arg_name=arg_name, column=column, type=type, op=op, required=required)


def _read(
    *,
    kind: ReadKind,
    view: str | None = None,
    pk_column: str | None = None,
    get_id_arg: str | None = None,
    filters: tuple[FilterSpec, ...] = (),
    singleton_fields: tuple[str, ...] = (),
    unpaginated: bool = False,
) -> ReadSpec:
    return ReadSpec(
        kind=kind,
        view=view,
        pk_column=pk_column,
        get_id_arg=get_id_arg,
        filters=filters,
        singleton_fields=singleton_fields,
        unpaginated=unpaginated,
    )


def _mutation(
    name: str,
    namespace: str,
    operation: str,
    args: tuple[ArgSpec, ...],
    response_fields: tuple[str, ...],
    doc: str,
    *,
    terminal: bool = False,
) -> OperationSpec:
    return OperationSpec(
        name=name,
        namespace=namespace,
        operation=operation,
        kind="mutation",
        args=args,
        response_fields=response_fields,
        visibility_class=VisibilityClass.PUBLIC_SDK,
        doc=doc,
        mutation=True,
        terminal=terminal,
    )


def _view_get(
    name: str,
    namespace: str,
    operation: str,
    view: str,
    id_arg: str,
    response_fields: tuple[str, ...],
    doc: str,
) -> OperationSpec:
    return OperationSpec(
        name=name,
        namespace=namespace,
        operation=operation,
        kind="read",
        args=(_arg(id_arg, "int"),),
        response_fields=response_fields,
        visibility_class=VisibilityClass.PUBLIC_SDK,
        doc=doc,
        read=_read(kind="view_get", view=view, pk_column="id", get_id_arg=id_arg),
    )


def _view_list(
    name: str,
    namespace: str,
    operation: str,
    view: str,
    pk_column: str,
    response_fields: tuple[str, ...],
    doc: str,
    *,
    filters: tuple[FilterSpec, ...] = (),
    extra_args: tuple[ArgSpec, ...] = (),
) -> OperationSpec:
    filter_args = tuple(
        _arg(flt.arg_name, flt.type, required=flt.required) for flt in filters
    )
    list_args = extra_args + filter_args + (
        _arg("cursor", "str", required=False),
        _arg("limit", "int", required=False),
    )
    return OperationSpec(
        name=name,
        namespace=namespace,
        operation=operation,
        kind="read",
        args=list_args,
        response_fields=response_fields,
        visibility_class=VisibilityClass.PUBLIC_SDK,
        doc=doc,
        read=_read(
            kind="view_list",
            view=view,
            pk_column=pk_column,
            filters=filters,
        ),
    )


_CUSTOMER_FIELDS = (
    "id", "name", "segment", "status", "created_at", "created_at_iso", "created_from_call_id",
)
_LOCATION_FIELDS = (
    "id", "customer_id", "name", "street", "unit", "city", "state", "postal_code",
    "latitude", "longitude", "zone_id", "active", "created_at", "created_at_iso",
    "modified_at", "modified_at_iso",
)
_CALL_FIELDS = (
    "id", "arrived_at", "arrived_at_iso", "service_date", "call_kind", "status",
    "caller_name", "caller_phone", "segment", "customer_id", "location_id",
    "service_street", "service_unit", "service_city", "service_state",
    "service_postal_code", "requested_service_name", "urgent", "related_job_id",
    "target_appointment_id", "target_job_id", "created_job_id", "terminal_at",
    "terminal_at_iso", "terminal_reason", "created_at", "created_at_iso",
)
_JOB_FIELDS = (
    "id", "job_number", "call_id", "customer_id", "location_id", "job_type_id",
    "business_unit_id", "requested_service_name", "status", "outcome", "priority",
    "no_charge", "recall_for_job_id", "summary", "created_at", "created_at_iso",
    "completed_at", "completed_at_iso", "modified_at", "modified_at_iso",
)
_APPOINTMENT_FIELDS = (
    "id", "appointment_number", "job_id", "status", "scheduled_start", "scheduled_start_iso",
    "scheduled_end", "scheduled_end_iso", "arrival_window_start", "arrival_window_start_iso",
    "arrival_window_end", "arrival_window_end_iso", "actual_start", "actual_start_iso",
    "actual_end", "actual_end_iso", "lateness_minutes", "cancelled_at", "cancelled_at_iso",
    "created_at", "created_at_iso", "modified_at", "modified_at_iso",
)
_APPOINTMENT_EVENT_FIELDS = (
    "id", "appointment_id", "technician_id", "event_type", "occurred_at", "occurred_at_iso",
    "service_date", "detail", "created_at", "created_at_iso",
)
_ASSIGNMENT_FIELDS = (
    "id", "appointment_id", "job_id", "technician_id", "status", "assigned_at",
    "assigned_at_iso", "unassigned_at", "unassigned_at_iso", "assigned_by", "created_at",
    "created_at_iso",
)
_TECHNICIAN_LOCATION_FIELDS = (
    "technician_id", "latitude", "longitude", "coordinates_updated_at", "location_state",
)
_INVOICE_FIELDS = (
    "id", "invoice_number", "job_id", "customer_id", "location_id", "business_unit_id",
    "invoice_date", "issued_at", "issued_at_iso", "status", "no_charge", "subtotal_cents",
    "total_cents", "material_cost_cents", "material_percent_basis_points", "created_at",
    "created_at_iso",
)
_PAYMENT_FIELDS = (
    "id", "payment_number", "invoice_id", "customer_id", "paid_at", "paid_at_iso",
    "amount_cents", "payment_type", "status", "created_at", "created_at_iso",
)
_LEDGER_FIELDS = (
    "id", "occurred_at", "occurred_at_iso", "service_date", "entry_type", "amount_cents",
    "balance_after_cents", "job_id", "invoice_id", "payment_id", "technician_id",
    "hiring_request_id", "training_reservation_id", "description", "created_at",
    "created_at_iso",
)
_CASH_POSITION_FIELDS = ("service_date", "as_of_ms", "cash_balance_cents", "run_status")

OPERATIONS: tuple[OperationSpec, ...] = (
    _view_get(
        "crm.get_customer", "crm", "get_customer", "customers", "customer_id",
        _CUSTOMER_FIELDS, "Return one customer by id.",
    ),
    _view_list(
        "crm.list_customers", "crm", "list_customers", "customers", "id",
        _CUSTOMER_FIELDS, "List customers with optional name and segment filters.",
        filters=(
            _filter("name", "name", "str", op=FilterOp.LIKE),
            _filter("segment", "segment", "str"),
        ),
    ),
    _mutation(
        "crm.create_customer", "crm", "create_customer",
        (
            _arg("call_id", "int"),
            _arg("name", "str"),
            _arg("segment", "str"),
            _arg("contacts", "list"),
            _arg("request_id", "str"),
        ),
        ("customer_id",),
        "Register a new customer on the active call.",
    ),
    _view_get(
        "crm.get_location", "crm", "get_location", "locations", "location_id",
        _LOCATION_FIELDS, "Return one service location by id.",
    ),
    _view_list(
        "crm.list_locations", "crm", "list_locations", "locations", "id",
        _LOCATION_FIELDS, "List locations with optional customer filter.",
        filters=(_filter("customer_id", "customer_id", "int"),),
    ),
    _mutation(
        "crm.create_location", "crm", "create_location",
        (
            _arg("call_id", "int"),
            _arg("customer_id", "int"),
            _arg("address", "str"),
            _arg("contacts", "list", required=False),
            _arg("request_id", "str"),
        ),
        ("location_id",),
        "Register a service location on the active call.",
    ),
    _view_get(
        "calls.get", "calls", "get", "calls", "call_id",
        _CALL_FIELDS, "Return one call by id.",
    ),
    _view_list(
        "calls.list", "calls", "list", "calls", "id",
        _CALL_FIELDS, "List calls with optional date, kind, and status filters.",
        filters=(
            _filter("date", "service_date", "date"),
            _filter("kind", "call_kind", "str"),
            _filter("status", "status", "str"),
        ),
    ),
    _mutation(
        "calls.reject", "calls", "reject",
        (_arg("call_id", "int"), _arg("request_id", "str")),
        ("call_id", "outcome", "message"),
        "Reject the active call.",
        terminal=True,
    ),
    _view_get(
        "jobs.get", "jobs", "get", "jobs", "job_id",
        _JOB_FIELDS, "Return one job by id.",
    ),
    _view_list(
        "jobs.list", "jobs", "list", "jobs", "id",
        _JOB_FIELDS, "List jobs with optional customer, status, and date filters.",
        filters=(
            _filter("customer_id", "customer_id", "int"),
            _filter("status", "status", "str"),
            _filter("date_from", "created_at", "date", op=FilterOp.DATE_GTE),
            _filter("date_to", "created_at", "date", op=FilterOp.DATE_LTE),
        ),
    ),
    _mutation(
        "jobs.book", "jobs", "book",
        (
            _arg("call_id", "int"),
            _arg("customer_id", "int"),
            _arg("location_id", "int"),
            _arg("job_type_id", "int"),
            _arg("service_date", "date"),
            _arg("block_start", "str"),
            _arg("technician_id", "int"),
            _arg("request_id", "str"),
        ),
        (
            "call_id", "outcome", "job_id", "appointment_id", "assignment_id", "blocks",
            "message",
        ),
        "Book a job on the active call.",
        terminal=True,
    ),
    _view_get(
        "appointments.get", "appointments", "get", "appointments", "appointment_id",
        _APPOINTMENT_FIELDS, "Return one appointment by id.",
    ),
    _view_list(
        "appointments.list", "appointments", "list", "appointments", "id",
        _APPOINTMENT_FIELDS,
        "List appointments with optional technician, date range, and status filters.",
        filters=(
            FilterSpec(
                "technician_id",
                "id",
                "int",
                op=FilterOp.EQ,
                required=False,
            ),
            _filter("date_from", "scheduled_start", "date", op=FilterOp.DATE_GTE),
            _filter("date_to", "scheduled_start", "date", op=FilterOp.DATE_LTE),
            _filter("status", "status", "str"),
        ),
    ),
    OperationSpec(
        name="appointments.list_events",
        namespace="appointments",
        operation="list_events",
        kind="read",
        args=(_arg("appointment_id", "int"),),
        response_fields=_APPOINTMENT_EVENT_FIELDS,
        visibility_class=VisibilityClass.PUBLIC_SDK,
        doc="List timeline events for one appointment.",
        read=_read(
            kind="view_list",
            view="appointment_events",
            pk_column="id",
            filters=(_filter("appointment_id", "appointment_id", "int", required=True),),
            unpaginated=True,
        ),
    ),
    _mutation(
        "appointments.acknowledge_cancellation", "appointments", "acknowledge_cancellation",
        (_arg("call_id", "int"), _arg("request_id", "str")),
        ("call_id", "outcome", "appointment_id", "released_blocks", "message"),
        "Acknowledge a cancellation call.",
        terminal=True,
    ),
    _mutation(
        "dispatch.assign", "dispatch", "assign",
        (
            _arg("appointment_id", "int"),
            _arg("technician_id", "int"),
            _arg("request_id", "str"),
        ),
        ("appointment_id", "technician_id", "previous_technician_id", "trip_index"),
        "Assign or reassign a technician to an appointment.",
    ),
    _view_list(
        "dispatch.list_assignments", "dispatch", "list_assignments",
        "appointment_assignments", "id",
        _ASSIGNMENT_FIELDS,
        "List appointment assignments with optional technician filter and a date filter "
        "on assigned_at (the day the assignment was made, not the appointment's service "
        "day); filter by service day through appointments.list instead.",
        filters=(
            _filter("technician_id", "technician_id", "int"),
            _filter("date", "assigned_at", "date"),
        ),
    ),
    OperationSpec(
        name="dispatch.get_technician_location",
        namespace="dispatch",
        operation="get_technician_location",
        kind="read",
        args=(_arg("technician_id", "int"),),
        response_fields=_TECHNICIAN_LOCATION_FIELDS,
        visibility_class=VisibilityClass.PUBLIC_SDK,
        doc="Return the current map location for one technician.",
        read=_read(
            kind="view_get",
            view="technician_locations",
            pk_column="technician_id",
            get_id_arg="technician_id",
        ),
    ),
    OperationSpec(
        name="finance.get_cash_position",
        namespace="finance",
        operation="get_cash_position",
        kind="read",
        args=(),
        response_fields=_CASH_POSITION_FIELDS,
        visibility_class=VisibilityClass.PUBLIC_SDK,
        doc="Return the company cash snapshot.",
        read=_read(
            kind="view_singleton",
            view="company_state",
            singleton_fields=_CASH_POSITION_FIELDS,
        ),
    ),
    _view_list(
        "finance.list_invoices", "finance", "list_invoices", "invoices", "id",
        _INVOICE_FIELDS,
        "List invoices with optional customer and date filters.",
        filters=(
            _filter("customer_id", "customer_id", "int"),
            _filter("date_from", "invoice_date", "date", op=FilterOp.DATE_GTE),
            _filter("date_to", "invoice_date", "date", op=FilterOp.DATE_LTE),
        ),
    ),
    _view_list(
        "finance.list_payments", "finance", "list_payments", "payments", "id",
        _PAYMENT_FIELDS,
        "List payments with optional invoice and date filters.",
        filters=(
            _filter("invoice_id", "invoice_id", "int"),
            _filter("date_from", "paid_at", "date", op=FilterOp.DATE_GTE),
            _filter("date_to", "paid_at", "date", op=FilterOp.DATE_LTE),
        ),
    ),
    _view_list(
        "finance.list_ledger", "finance", "list_ledger", "ledger_entries", "id",
        _LEDGER_FIELDS,
        "List ledger entries in a date range.",
        filters=(
            _filter("date_from", "service_date", "date", op=FilterOp.DATE_GTE, required=True),
            _filter("date_to", "service_date", "date", op=FilterOp.DATE_LTE, required=True),
            _filter("entry_type", "entry_type", "str"),
        ),
    ),
    OperationSpec(
        name="query.sql",
        namespace="query",
        operation="sql",
        kind="read",
        args=(
            _arg("statement", "str"),
            _arg("max_rows", "int", required=False),
        ),
        response_fields=("columns", "rows", "row_count", "truncated"),
        visibility_class=VisibilityClass.PUBLIC_SQL,
        doc="Run one read-only SQL statement against public views.",
        read=_read(kind="sql"),
        sql_positional_arg="statement",
    ),
    OperationSpec(
        name="weather.get",
        namespace="weather",
        operation="get",
        kind="read",
        args=(_arg("zone_id", "int"), _arg("date", "date")),
        response_fields=(
            "zone_id", "date", "kind", "horizon_days", "high_f", "low_f",
            "precipitation_probability", "storm", "storm_severity",
        ),
        visibility_class=VisibilityClass.PUBLIC_SDK,
        doc=(
            "Return observed or forecast weather for one zone and date. "
            "storm_severity is present only when storm is true."
        ),
        read=_read(kind="weather"),
    ),
    OperationSpec(
        name="maps.distance",
        namespace="maps",
        operation="distance",
        kind="read",
        args=(_arg("origin_address", "str"), _arg("destination_address", "str")),
        response_fields=(
            "origin_address", "destination_address", "distance_km", "distance_miles",
            "dataset_version",
        ),
        visibility_class=VisibilityClass.PUBLIC_SDK,
        doc="Return frozen route distance between two addresses.",
        read=_read(kind="maps"),
    ),
)

OPERATIONS_BY_NAME: dict[str, OperationSpec] = {spec.name: spec for spec in OPERATIONS}

_VIEW_COLUMNS: dict[str, tuple[str, ...]] = {
    spec.name: tuple(col.name for col in spec.columns) for spec in VIEWS
}

_APPOINTMENTS_TECHNICIAN_FILTER = (
    "id IN (SELECT appointment_id FROM appointment_assignments "
    "WHERE technician_id = ? AND unassigned_at IS NULL)"
)

_DISPATCH_DATE_FILTER = (
    f"date(assigned_at / 1000, 'unixepoch', '{PHOENIX_SQLITE_UTC_OFFSET}') = ?"
)


def _timestamp_date_predicate(column: str, op: str) -> str:
    return f"date({column} / 1000, 'unixepoch', '{PHOENIX_SQLITE_UTC_OFFSET}') {op} ?"


def _filter_predicate(spec: OperationSpec, flt: FilterSpec) -> str:
    if spec.name == "appointments.list" and flt.arg_name == "technician_id":
        return _APPOINTMENTS_TECHNICIAN_FILTER
    if spec.name == "dispatch.list_assignments" and flt.arg_name == "date":
        return _DISPATCH_DATE_FILTER
    column = flt.column
    if flt.op is FilterOp.EQ:
        return f"{column} = ?"
    if flt.op is FilterOp.LIKE:
        return f"{column} LIKE '%' || ? || '%'"
    if flt.op is FilterOp.DATE_GTE:
        if column in {"service_date", "invoice_date"}:
            return f"{column} >= ?"
        return _timestamp_date_predicate(column, ">=")
    if flt.op is FilterOp.DATE_LTE:
        if column in {"service_date", "invoice_date"}:
            return f"{column} <= ?"
        return _timestamp_date_predicate(column, "<=")
    raise AssertionError(f"unsupported filter op {flt.op!r}")


def canonical_registry_bytes() -> bytes:
    payload = []
    for spec in OPERATIONS:
        entry: dict[str, object] = {
            "name": spec.name,
            "namespace": spec.namespace,
            "operation": spec.operation,
            "kind": spec.kind,
            "args": [
                {"name": arg.name, "type": arg.type, "required": arg.required}
                for arg in spec.args
            ],
            "response_fields": list(spec.response_fields),
            "visibility_class": spec.visibility_class.value,
            "doc": spec.doc,
            "terminal": spec.terminal,
        }
        if spec.sql_positional_arg is not None:
            entry["sql_positional_arg"] = spec.sql_positional_arg
        if spec.read is not None:
            read_entry: dict[str, object] = {"kind": spec.read.kind}
            if spec.read.view is not None:
                read_entry["view"] = spec.read.view
            if spec.read.pk_column is not None:
                read_entry["pk_column"] = spec.read.pk_column
            if spec.read.get_id_arg is not None:
                read_entry["get_id_arg"] = spec.read.get_id_arg
            if spec.read.singleton_fields:
                read_entry["singleton_fields"] = list(spec.read.singleton_fields)
            if spec.read.filters:
                read_entry["filters"] = [
                    {
                        "arg_name": flt.arg_name,
                        "column": flt.column,
                        "type": flt.type,
                        "op": flt.op.value,
                        "required": flt.required,
                    }
                    for flt in spec.read.filters
                ]
            if spec.read.unpaginated:
                read_entry["unpaginated"] = True
            entry["read"] = read_entry
        payload.append(entry)
    return json.dumps({"operations": payload}, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def compute_registry_hash() -> str:
    return hashlib.sha256(canonical_registry_bytes()).hexdigest()


REGISTRY_HASH: str = compute_registry_hash()


def validate_operation_identity(spec: OperationSpec) -> None:
    if spec.namespace == "workforce":
        raise AssertionError(
            f"{spec.name}: the workforce namespace is not part of the v1 public registry"
        )
    if spec.name != f"{spec.namespace}.{spec.operation}":
        raise AssertionError(
            f"{spec.name}: name must equal '{spec.namespace}.{spec.operation}'"
        )


def validate_registry() -> None:
    if len(OPERATIONS) != len(OPERATIONS_BY_NAME):
        raise AssertionError("duplicate operation names in OPERATIONS")
    expected_non_workforce = 26
    if len(OPERATIONS) != expected_non_workforce:
        raise AssertionError(
            f"OPERATIONS must declare exactly {expected_non_workforce} v1 public operations"
        )
    for spec in OPERATIONS:
        validate_operation_identity(spec)
        if spec.read is None and spec.kind == "read":
            raise AssertionError(f"{spec.name}: read operation missing read spec")
        if spec.read is not None and spec.read.view is not None:
            if spec.read.view not in VIEW_WHITELIST:
                raise AssertionError(
                    f"{spec.name}: view {spec.read.view!r} is not in VIEW_WHITELIST"
                )
            columns = set(_VIEW_COLUMNS[spec.read.view])
            if spec.read.kind in {"view_get", "view_list"}:
                if set(spec.response_fields) != columns:
                    raise AssertionError(
                        f"{spec.name}: response_fields do not exactly match view "
                        f"{spec.read.view!r} columns"
                    )
            if spec.read.pk_column is not None and spec.read.pk_column not in columns:
                raise AssertionError(
                    f"{spec.name}: pk column {spec.read.pk_column!r} not on view "
                    f"{spec.read.view!r}"
                )
            for flt in spec.read.filters:
                if flt.arg_name == "technician_id" and spec.name == "appointments.list":
                    continue
                if flt.arg_name == "date" and spec.name == "dispatch.list_assignments":
                    continue
                if flt.column not in columns:
                    raise AssertionError(
                        f"{spec.name}: filter column {flt.column!r} not on view "
                        f"{spec.read.view!r}"
                    )
            if spec.read.kind == "view_singleton":
                expected_singleton = set(spec.read.singleton_fields) or columns
                if set(spec.response_fields) != expected_singleton:
                    raise AssertionError(
                        f"{spec.name}: response_fields do not exactly match view "
                        f"{spec.read.view!r} singleton fields"
                    )
                for field in spec.read.singleton_fields:
                    if field not in columns:
                        raise AssertionError(
                            f"{spec.name}: singleton field {field!r} not on view "
                            f"{spec.read.view!r}"
                        )
        if spec.read is not None and spec.read.kind == "view_list" and spec.read.unpaginated:
            for arg in spec.args:
                if arg.name in {"cursor", "limit"}:
                    raise AssertionError(
                        f"{spec.name}: unpaginated list read must not declare {arg.name!r}"
                    )
        arg_names = [arg.name for arg in spec.args]
        if len(arg_names) != len(set(arg_names)):
            raise AssertionError(f"{spec.name}: duplicate argument names")


validate_registry()


__all__ = [
    "OPERATIONS",
    "OPERATIONS_BY_NAME",
    "REGISTRY_HASH",
    "ArgSpec",
    "ArgType",
    "FilterOp",
    "FilterSpec",
    "OpKind",
    "OperationSpec",
    "ReadKind",
    "ReadSpec",
    "canonical_registry_bytes",
    "compute_registry_hash",
    "validate_operation_identity",
    "validate_registry",
    "_filter_predicate",
]
