"""View registry: the 31 public views, their SQL, and the golden hash
(spec 02 §3.1/§3.2; CSR-002 decisions 6–7).

The registry is the single machine-readable source of the agent-visible
surface: for every view it declares the name, the full ``CREATE VIEW`` text,
and the ordered column list, and each column is classified through
:mod:`csr_bench.visibility` (passthroughs resolve by base-table field;
computed columns — the ISO siblings of decision 7 and the two
``company_state`` projections — declare a class explicitly). Any other
specification naming a public view column names it as the registry spells it
(spec 02 §3.1).

At every database open the engine Must recreate all views from this registry
and Must verify that the resulting ``sqlite_schema`` view definitions hash to
the recorded golden constant :data:`VIEW_REGISTRY_HASH`; a mismatch is a
startup failure, not a warning (spec 02 §3.1, §12). This is the tripwire that
makes silent view drift — the most likely way a hidden column reaches the
agent — impossible to ship (CSR-002 decision 6).

The SQL is *generated* from the column declarations (:func:`_build_sql`) so
the SELECT list and the registry's column list can never disagree — a
divergence between the two would itself be a defect (spec 02 §3.1). SQLite
normalizes the stored text, so the golden hash covers what SQLite actually
stored, comparing like with like.
"""

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
    """One column of a public view: either a passthrough of a base-table
    field or a computed expression.

    Exactly one of ``source`` / ``expr`` is set:

    - ``source`` — the view column is the base-table field itself; its
      classification resolves through :func:`visibility.classify` at
      validation time, never stored here (spec 02 §3.1: the classification
      source is ``visibility.py``).
    - ``expr`` — the column is computed in SQL (an ISO sibling of decision 7,
      or a ``company_state`` projection) and Must declare its
      ``visibility_class`` explicitly.
    """

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
    """One whitelisted view: name, full ``CREATE VIEW`` text, ordered columns.

    ``columns`` is ordered exactly as the SELECT list outputs them (verified
    against ``PRAGMA table_info`` by the SCH-02 test).
    """

    name: str
    sql: str
    columns: tuple[ViewColumn, ...]


def _iso(alias: str, column: str) -> ViewColumn:
    """The ISO-8601 sibling of an epoch-millisecond column (decision 7).

    Rendered with SQLite's own date math and the fixed −07:00 Phoenix offset
    — no registered functions (a registered function would be callable by
    agent SQL), no floats (``<col>/1000`` is integer division), no wall clock
    (spec 02 §1.3 timestamps, §4.9 note). ``America/Phoenix`` has no DST, so
    the offset is constant (clock.py).
    """
    return ViewColumn(
        name=f"{column}_iso",
        expr=(
            f"strftime('%Y-%m-%dT%H:%M:%S', {alias}.{column}/1000, "
            f"'unixepoch', '-7 hours') || '-07:00'"
        ),
        visibility_class=VisibilityClass.PUBLIC_SQL,
    )


def _project(table: str, alias: str, *columns: str | tuple[str, bool]) -> tuple[ViewColumn, ...]:
    """Expand the ordered base-table column list into the view's column tuple.

    A bare name is a plain passthrough; a ``(name, True)`` pair marks an
    epoch-millisecond column, which gains the immediately-following
    ``<name>_iso`` sibling (decision 7). Order is the base table's own column
    order minus the private/``origin`` columns (spec 02 §4 preamble:02:220).
    """
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
    """Assemble the full ``CREATE VIEW`` text from the column tuple.

    Generating the SELECT from the registry's column list is what guarantees
    the two can never disagree (spec 02 §3.1). Every view reads only its own
    ``_``-table plus ``_clock``; ``company_state`` is the one exception and is
    declared verbatim instead (spec 02 §5:02:519).
    """
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
    """Build one data view: passthroughs plus ISO siblings, clock-filtered.

    The default filter is creation time ``<alias>.created_at <=
    k.current_time_ms`` — the spec 02 §1.5 rule "filter on creation/arrival
    time, never on scheduled time" applied to every view except the three
    that pass an explicit filter (``calls``: arrival; ``training_rates``:
    date; ``appointments`` is default creation-time and stated explicitly).
    The filter is harmless for seed rows (``created_at = 0``) and keeps
    future-materialized shift rows visible (their ``created_at`` is the
    initialization instant) — spec 02 §8.8.
    """
    if filter_sql is None:
        filter_sql = f"{alias}.created_at <= k.current_time_ms"
    cols = _project(table, alias, *columns)
    return ViewSpec(
        name=name,
        sql=_build_sql(name, table, alias, filter_sql, cols),
        columns=cols,
    )


# ---------------------------------------------------------------------------
# The whitelist (spec 02 §3.2: exactly these 31 views, in the table's order)
# ---------------------------------------------------------------------------

VIEWS: tuple[ViewSpec, ...] = (
    # -- Reference (spec 02 §2) ---------------------------------------------
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
    # -- CRM (spec 02 §4.1) --------------------------------------------------
    # The customers public set is EXACTLY (id, name, segment, status,
    # created_at, created_from_call_id) (spec 02 §4.1:02:241, spec 05 §1.1) —
    # `modified_at` is deliberately not projected (final-branch audit F1).
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
    # _locations: `effective_zone_id` and `address_catalog_id` are private
    # (spec 02 §4.1 column list:02:234) and never projected.
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
    # -- Calls (spec 02 §4.2) ------------------------------------------------
    # `address_catalog_id` is private (02:257); `origin` is private (§1.3).
    # §1.5: visible only once the call has ARRIVED — the row itself is created
    # at arrival, and the clock filter is defence in depth (02:269).
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
    # -- Work (spec 02 §4.3–4.4) ---------------------------------------------
    _view(
        "jobs", "_jobs", "j",
        "id", "job_number", "call_id", "customer_id", "location_id",
        "job_type_id", "business_unit_id", "requested_service_name", "status",
        "outcome", "priority", "no_charge", "recall_for_job_id", "summary",
        ("created_at", True), ("completed_at", True), ("modified_at", True),
    ),
    # §1.5: a future appointment is visible because it was CREATED in the
    # past — never filtered on scheduled time (02:89; SCH-04).
    _view(
        "appointments", "_appointments", "a",
        "id", "appointment_number", "job_id", "status",
        ("scheduled_start", True), ("scheduled_end", True),
        ("arrival_window_start", True), ("arrival_window_end", True),
        ("actual_start", True), ("actual_end", True), ("cancelled_at", True),
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
    # -- Workforce (spec 02 §4.5) --------------------------------------------
    # The Q38 visible set: identity, home location, join date, wage,
    # employment/availability state (02:383). Skill and strain live in the
    # private tables of spec 04 §1.5, never here.
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
    # Passthrough of the stored row minus the six `trip_*` columns — the
    # read-time trip-interpolation projection is deferred to CSR-006, which
    # will flag its SQL change through the golden-hash tripwire
    # (CSR-002 decision 18). `technician_id` is the public PK and leads.
    # `_technician_locations` has no `created_at`; its only timestamp is
    # `coordinates_updated_at`, which is written at initialization and on
    # every ping at <= the clock, so the §1.5 creation-time filter is that
    # column — it can never hide a current row or leak a future one.
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
    # -- Labor market (spec 02 §4.6) -----------------------------------------
    _view(
        "hiring_requests", "_hiring_requests", "hr",
        "id", ("requested_at", True), "requested_skill_codes_json",
        "minimum_experience_years", "preferred_home_zone_id", "status",
        "recruiting_cost_cents", "committed_hourly_wage_cents",
        "committed_start_date", "technician_id", ("created_at", True),
        ("modified_at", True),
    ),
    # §1.5/§4.6: rates are drawn each morning and are not knowable in advance
    # — the view filters `rate_date <= k.service_date` (02:89, 02:412).
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
    # -- Money (spec 02 §4.7) ------------------------------------------------
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
    # -- Reputation and alerts (spec 02 §4.8) ---------------------------------
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
    # -- Company state (spec 02 §4.9) ----------------------------------------
    # SQL is spec 02 §4.9 VERBATIM (02:474-482): a one-row view, not a base
    # table; the one exception to the generated SQL — no ISO siblings, no
    # additions, and never operational aggregates (§3.3). It reads `_clock`
    # plus `_ledger_entries`/`_run` — the spec 02 §5:02:519 exception list.
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

#: The 31 agent-visible view names of spec 02 §3.2, as a literal. The registry
#: derives the whitelist from it (:data:`VIEW_WHITELIST`), and
#: :func:`validate_registry` compares the declared views against this literal —
#: a view added to the registry without a deliberate whitelist decision, or
#: dropped from it, fails at build time. The literal is the guard, so it cannot
#: be tautological (C8 nit: the earlier self-comparison could never fire).
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

#: The 31 agent-visible view names (spec 02 §3.2). Computed from the registry
#: so the whitelist can never drift from the declared views; the SCH-02 test
#: pins the count and the set, and :func:`validate_registry` pins the set
#: against the literal :data:`_WHITELIST_NAMES`.
VIEW_WHITELIST: frozenset[str] = frozenset(spec.name for spec in VIEWS)

#: Golden hash of the stored view definitions, captured by the SCH-02 test
#: into a scratch database (CSR-002 decision 6). Changing any view SQL
#: requires regenerating it — the test fails otherwise.
VIEW_REGISTRY_HASH: str = (
    "95f0c90aaf08120df5d1a315bfb7ed5e60e52dc00114cc38d913b2550adca5d8"
)


def create_views(conn: sqlite3.Connection) -> None:
    """Recreate every whitelisted view from the registry (spec 02 §3.1).

    Idempotent: any existing view whose name is in the whitelist is dropped
    first, then all 31 are recreated. This is the repo's one permitted DDL
    site outside the migration runner (spec 02 §9: "The one exception is view
    creation from the registry at open"); views outside the whitelist are
    never dropped here — :func:`verify_views` reports them instead.
    """
    for spec in VIEWS:
        conn.execute(f"DROP VIEW IF EXISTS {spec.name}")
    for spec in VIEWS:
        conn.execute(spec.sql)


def canonical_view_bytes(conn: sqlite3.Connection) -> bytes:
    """Canonical sorted-JSON of the stored definitions of the whitelisted views.

    The hash covers what SQLite actually stored in ``sqlite_schema`` — the
    normalized view text, not the registry's input strings — which is the
    point: comparing like with like across opens (CSR-002 decision 6). Same
    canonical style as :func:`csr_bench.config.canonical_config_bytes`:
    ``sort_keys=True``, compact separators, stable ordering by name.
    """
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
    """SHA-256 hex digest of :func:`canonical_view_bytes` — the comparison
    token for the golden-hash tripwire."""
    return hashlib.sha256(canonical_view_bytes(conn)).hexdigest()


def verify_views(conn: sqlite3.Connection) -> None:
    """The SCH-02 check: whitelist set-equality, then the golden hash.

    (a) the stored view-name set must equal the whitelist exactly — no orphan
    views, no missing views (spec 02 §3.2: "Any other name ... is rejected");
    (b) the stored definitions must hash to :data:`VIEW_REGISTRY_HASH`. Any
    failure is a startup failure with :data:`ErrorCode.VIEW_REGISTRY_MISMATCH`
    (spec 02 §3.1: "A mismatch is a startup failure, not a warning"; §12).
    The message is controller-facing.
    """
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
    """Build-time validation of the registry (used by the SCH-02 test).

    Every passthrough column Must resolve through :func:`visibility.classify`
    to a public class, every computed column Must declare a public class, and
    column names Must be unique within a view — the spec 02 §3.1 rule that
    "any other specification naming a public view column names it as the
    registry spells it" applied to the registry itself. A violation is a
    build failure (an unclassified field raises
    :data:`ErrorCode.UNCLASSIFIED_FIELD` from ``classify``), never a runtime
    maybe.
    """
    if len(VIEWS) != 31 or {spec.name for spec in VIEWS} != _WHITELIST_NAMES:
        raise AssertionError(
            "the whitelist must be exactly the 31 declared views (spec 02 §3.2): "
            "the registry and the _WHITELIST_NAMES literal disagree — adding or "
            "removing a view requires a deliberate whitelist decision"
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
