from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from . import visibility
from .enums import VisibilityClass
from .errors import EngineError, ErrorCode
from .visibility import Field


@dataclass(frozen=True)
class ViewColumn:

    name: str
    source: Field | None = None
    expr: str | None = None
    visibility_class: VisibilityClass | None = None

    def __post_init__(self) -> None:
        if (self.source is None) == (self.expr is None):
            raise ValueError(
                f"ViewColumn {self.name!r}: exactly one of source/expr must be set"
            )
        if self.source is not None:
            if self.visibility_class is not None:
                raise ValueError(
                    f"ViewColumn {self.name!r}: a passthrough resolves its class "
                    f"via visibility.classify and must not declare one"
                )
        elif self.visibility_class is None:
            raise ValueError(
                f"ViewColumn {self.name!r}: a computed column must declare "
                f"its visibility class"
            )


@dataclass(frozen=True)
class ViewSpec:

    name: str
    sql: str
    columns: tuple[ViewColumn, ...]


def _iso(alias: str, column: str) -> ViewColumn:
    return ViewColumn(
        name=f"{column}_iso",
        expr=(
            f"strftime('%Y-%m-%dT%H:%M:%S', {alias}.{column}/1000, "
            f"'unixepoch', '-7 hours') || '-07:00'"
        ),
        visibility_class=VisibilityClass.PUBLIC_SQL,
    )


def _project(table: str, alias: str, *columns: str | tuple[str, bool]) -> tuple[ViewColumn, ...]:
    out: list[ViewColumn] = []
    for entry in columns:
        name, is_epoch = entry if isinstance(entry, tuple) else (entry, False)
        out.append(ViewColumn(name=name, source=Field(table, name)))
        if is_epoch:
            out.append(_iso(alias, name))
    return tuple(out)


def _build_sql(
    name: str,
    table: str,
    alias: str,
    filter_sql: str,
    columns: tuple[ViewColumn, ...],
) -> str:
    select = ",\n       ".join(
        f"{alias}.{c.name}" if c.source is not None else f"{c.expr} AS {c.name}"
        for c in columns
    )
    return (
        f"CREATE VIEW {name} AS\n"
        f"  SELECT {select}\n"
        f"  FROM {table} {alias}, _clock k\n"
        f"  WHERE {filter_sql}"
    )


def _view(
    name: str,
    table: str,
    alias: str,
    *columns: str | tuple[str, bool],
    filter_sql: str | None = None,
) -> ViewSpec:
    if filter_sql is None:
        filter_sql = f"{alias}.created_at <= k.current_time_ms"
    cols = _project(table, alias, *columns)
    return ViewSpec(
        name=name,
        sql=_build_sql(name, table, alias, filter_sql, cols),
        columns=cols,
    )


VIEWS: tuple[ViewSpec, ...] = (
    _view(
        "business_units", "_business_units", "b",
        "id", "code", "name", "active", ("created_at", True),
    ),
    _view(
        "job_types", "_job_types", "jt",
        "id", "code", "name", "business_unit_id", "scheduled_blocks",
        "required_skill_code", "active", ("created_at", True),
    ),
    _view(
        "zones", "_zones", "z",
        "id", "code", "name", "cities_json", "postal_codes_json", "active",
        ("created_at", True),
    ),
    _view(
        "customers", "_customers", "cu",
        "id", "name", "segment", "status", ("created_at", True),
        "created_from_call_id",
    ),
    _view(
        "customer_contacts", "_customer_contacts", "cc",
        "id", "customer_id", "contact_type", "value", "do_not_text", "memo",
        ("created_at", True),
    ),
    _view(
        "locations", "_locations", "lo",
        "id", "customer_id", "name", "street", "unit", "city", "state",
        "postal_code", "latitude", "longitude", "zone_id", "active",
        ("created_at", True), ("modified_at", True),
    ),
    _view(
        "location_contacts", "_location_contacts", "lc",
        "id", "location_id", "contact_type", "value", "do_not_text", "memo",
        ("created_at", True),
    ),
    _view(
        "calls", "_calls", "c",
        "id", ("arrived_at", True), "service_date", "call_kind", "status",
        "caller_name", "caller_phone", "segment", "customer_id", "location_id",
        "service_street", "service_unit", "service_city", "service_state",
        "service_postal_code", "requested_service_name", "urgent",
        "related_job_id", "target_appointment_id", "target_job_id",
        "created_job_id", ("terminal_at", True), "terminal_reason",
        ("created_at", True),
        filter_sql="c.arrived_at <= k.current_time_ms",
    ),
    _view(
        "call_attempts", "_call_attempts", "ca",
        "id", "call_id", "attempt_number", ("prompted_at", True), "outcome",
        ("created_at", True),
    ),
    _view(
        "jobs", "_jobs", "j",
        "id", "job_number", "call_id", "customer_id", "location_id",
        "job_type_id", "business_unit_id", "requested_service_name", "status",
        "outcome", "priority", "no_charge", "recall_for_job_id", "summary",
        ("created_at", True), ("completed_at", True), ("modified_at", True),
    ),
    _view(
        "appointments", "_appointments", "a",
        "id", "appointment_number", "job_id", "status",
        ("scheduled_start", True), ("scheduled_end", True),
        ("arrival_window_start", True), ("arrival_window_end", True),
        ("actual_start", True), ("actual_end", True), "lateness_minutes",
        ("cancelled_at", True),
        ("created_at", True), ("modified_at", True),
    ),
    _view(
        "appointment_blocks", "_appointment_blocks", "ab",
        "id", "appointment_id", "ordinal", "block_code",
        ("block_start", True), ("block_end", True), ("created_at", True),
    ),
    _view(
        "appointment_assignments", "_appointment_assignments", "aa",
        "id", "appointment_id", "job_id", "technician_id", "status",
        ("assigned_at", True), ("unassigned_at", True), "assigned_by",
        ("created_at", True),
    ),
    _view(
        "appointment_events", "_appointment_events", "ae",
        "id", "appointment_id", "technician_id", "event_type",
        ("occurred_at", True), "service_date", "detail", ("created_at", True),
    ),
    _view(
        "technicians", "_technicians", "tech",
        "id", "name", "street", "unit", "city", "state", "postal_code",
        "home_latitude", "home_longitude", "home_zone_id", "joined_on",
        "hourly_wage_cents", "employment_status", "availability_status",
        "available_from", "final_working_date", ("created_at", True),
        ("modified_at", True),
    ),
    _view(
        "technician_skills", "_technician_skills", "tsk",
        "id", "technician_id", "skill_code", "active", "granted_on",
        ("created_at", True),
    ),
    _view(
        "technician_shifts", "_technician_shifts", "tsh",
        "id", "technician_id", "shift_type", ("start_at", True),
        ("end_at", True), "service_date", "active", "on_call_rotation_id",
        ("created_at", True),
    ),
    _view(
        "technician_locations", "_technician_locations", "tl",
        "technician_id", "latitude", "longitude",
        ("coordinates_updated_at", True), "location_state",
        filter_sql="tl.coordinates_updated_at <= k.current_time_ms",
    ),
    _view(
        "technician_absences", "_technician_absences", "ta",
        "id", "technician_id", "absence_kind", "start_date",
        "expected_return_date", "actual_return_date", "status",
        ("created_at", True),
    ),
    _view(
        "non_job_appointments", "_non_job_appointments", "nja",
        "id", "technician_id", "kind", "name", ("start_at", True),
        ("end_at", True), "block_code", "status", "training_reservation_id",
        "technician_absence_id", ("created_at", True),
    ),
    _view(
        "on_call_rotations", "_on_call_rotations", "ocr",
        "id", "business_unit_id", "technician_id", "effective_start_date",
        "effective_end_date", "status", ("vacated_at", True),
        ("created_at", True), ("modified_at", True),
    ),
    _view(
        "hiring_requests", "_hiring_requests", "hr",
        "id", ("requested_at", True), "requested_skill_codes_json",
        "minimum_experience_years", "preferred_home_zone_id", "status",
        "recruiting_cost_cents", "committed_hourly_wage_cents",
        "committed_start_date", "technician_id", ("created_at", True),
        ("modified_at", True),
    ),
    _view(
        "training_rates", "_training_rates", "t",
        "id", "skill_code", "rate_date", "hourly_rate_cents",
        ("created_at", True),
        filter_sql="t.rate_date <= k.service_date",
    ),
    _view(
        "training_reservations", "_training_reservations", "tres",
        "id", "technician_id", "skill_code", ("scheduled_at", True),
        "total_hours", "locked_hourly_rate_cents", "prepaid_cost_cents",
        "status", "completed_hours", ("created_at", True),
        ("modified_at", True),
    ),
    _view(
        "invoices", "_invoices", "i",
        "id", "invoice_number", "job_id", "customer_id", "location_id",
        "business_unit_id", "invoice_date", ("issued_at", True), "status",
        "no_charge", "subtotal_cents", "total_cents", "material_cost_cents",
        "material_percent_basis_points", ("created_at", True),
    ),
    _view(
        "invoice_items", "_invoice_items", "ii",
        "id", "invoice_id", "item_type", "description", "quantity",
        "amount_cents", ("created_at", True),
    ),
    _view(
        "payments", "_payments", "p",
        "id", "payment_number", "invoice_id", "customer_id",
        ("paid_at", True), "amount_cents", "payment_type", "status",
        ("created_at", True),
    ),
    _view(
        "ledger_entries", "_ledger_entries", "le",
        "id", ("occurred_at", True), "service_date", "entry_type",
        "amount_cents", "balance_after_cents", "job_id", "invoice_id",
        "payment_id", "technician_id", "hiring_request_id",
        "training_reservation_id", "description", ("created_at", True),
    ),
    _view(
        "reviews", "_reviews", "r",
        "id", "job_id", "appointment_id", "customer_id", "technician_id",
        ("published_at", True), "service_date", "stars", "source",
        ("created_at", True),
    ),
    _view(
        "notifications", "_notifications", "n",
        "id", ("created_at", True), "service_date", "notification_type",
        "entity_type", "entity_id", "message",
    ),
    ViewSpec(
        name="company_state",
        sql=(
            "CREATE VIEW company_state AS\n"
            "  SELECT k.service_date,\n"
            "         k.current_time_ms                                    AS as_of_ms,\n"
            "         (SELECT COALESCE(SUM(amount_cents), 0) FROM _ledger_entries\n"
            "           WHERE occurred_at <= k.current_time_ms)            AS cash_balance_cents,\n"
            "         (SELECT status FROM _run)                            AS run_status\n"
            "  FROM _clock k"
        ),
        columns=(
            ViewColumn(name="service_date", source=Field("_clock", "service_date")),
            ViewColumn(name="as_of_ms", source=Field("_clock", "current_time_ms")),
            ViewColumn(
                name="cash_balance_cents",
                expr=(
                    "(SELECT COALESCE(SUM(amount_cents), 0) FROM _ledger_entries "
                    "WHERE occurred_at <= k.current_time_ms)"
                ),
                visibility_class=VisibilityClass.PUBLIC_SQL,
            ),
            ViewColumn(
                name="run_status",
                expr="(SELECT status FROM _run)",
                visibility_class=VisibilityClass.PUBLIC_SQL,
            ),
        ),
    ),
)

_WHITELIST_NAMES: frozenset[str] = frozenset(
    {
        "business_units", "job_types", "zones",
        "customers", "customer_contacts", "locations", "location_contacts",
        "calls", "call_attempts",
        "jobs", "appointments", "appointment_blocks",
        "appointment_assignments", "appointment_events",
        "technicians", "technician_skills", "technician_shifts",
        "technician_locations", "technician_absences", "non_job_appointments",
        "on_call_rotations",
        "hiring_requests", "training_rates", "training_reservations",
        "invoices", "invoice_items", "payments", "ledger_entries",
        "reviews", "notifications",
        "company_state",
    }
)

VIEW_WHITELIST: frozenset[str] = frozenset(spec.name for spec in VIEWS)

VIEW_REGISTRY_HASH: str = (
    "05d3b73e639fefa739afd49d460d5bb376245d62b2931a8e177aa5e2b7701404"
)


def create_views(conn: sqlite3.Connection) -> None:
    for spec in VIEWS:
        conn.execute(f"DROP VIEW IF EXISTS {spec.name}")
    for spec in VIEWS:
        conn.execute(spec.sql)


def canonical_view_bytes(conn: sqlite3.Connection) -> bytes:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type='view'"
    ).fetchall()
    stored = {
        str(row["name"]): str(row["sql"])
        for row in rows
        if str(row["name"]) in VIEW_WHITELIST
    }
    return json.dumps(
        {"views": {name: stored[name] for name in sorted(stored)}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_view_hash(conn: sqlite3.Connection) -> str:
    return hashlib.sha256(canonical_view_bytes(conn)).hexdigest()


def verify_views(conn: sqlite3.Connection) -> None:
    stored = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='view'")
    }
    if stored != VIEW_WHITELIST:
        raise EngineError(
            ErrorCode.VIEW_REGISTRY_MISMATCH,
            "stored view set differs from the whitelist: "
            f"orphans={sorted(stored - VIEW_WHITELIST)} "
            f"missing={sorted(VIEW_WHITELIST - stored)}",
        )
    if compute_view_hash(conn) != VIEW_REGISTRY_HASH:
        raise EngineError(
            ErrorCode.VIEW_REGISTRY_MISMATCH,
            "stored view definitions do not hash to VIEW_REGISTRY_HASH",
        )


def validate_registry() -> None:
    if len(VIEWS) != 31 or {spec.name for spec in VIEWS} != _WHITELIST_NAMES:
        raise AssertionError(
            "the whitelist must be exactly the 31 declared views: the registry and "
            "the _WHITELIST_NAMES literal disagree — adding or removing a view "
            "requires a deliberate whitelist decision"
        )
    for spec in VIEWS:
        seen: set[str] = set()
        for col in spec.columns:
            if col.name in seen:
                raise AssertionError(
                    f"view {spec.name!r}: duplicate column {col.name!r}"
                )
            seen.add(col.name)
            if col.source is not None:
                resolved = visibility.classify(col.source)
                if resolved not in VisibilityClass.public():
                    raise AssertionError(
                        f"view {spec.name!r}: passthrough {col.source} resolves "
                        f"to non-public {resolved}"
                    )
            elif col.visibility_class not in VisibilityClass.public():
                raise AssertionError(
                    f"view {spec.name!r}: computed column {col.name!r} declares "
                    f"non-public {col.visibility_class}"
                )


__all__ = [
    "VIEWS",
    "VIEW_REGISTRY_HASH",
    "VIEW_WHITELIST",
    "ViewColumn",
    "ViewSpec",
    "canonical_view_bytes",
    "compute_view_hash",
    "create_views",
    "validate_registry",
    "verify_views",
]
