"""Run initialization sequence (spec 02 §9, §12; CSR-002 decision 12).

The controller-facing startup path, in order: open the run database (migrate,
recreate the whitelisted views, verify the golden hash — spec 02 §9/§3.1),
create the run record and ``_clock`` singleton seeded at the opening instant
2026-10-01T06:45:00-07:00, persist the resolved config artifact and the
visibility registry copy (decision 5), run the warm start (spec 02 §8), verify
the clean boundary of spec 02 §8.5, and flip the run to ``active``.

Any failure — migration, hash mismatch, warm start, or boundary violation —
aborts initialization (spec 02 §12): the run is reported ``failed`` (the
``initializing -> failed`` transition, legal since CSR-002 decision 12) and a
partially built database is never reused — :func:`initialize_run` refuses to
build onto a path that already contains a run record, so a clean re-init
requires a fresh file.

CSR-005's controller calls exactly these functions; nothing here is test-only
(decision 12). WS-01's boundary assertions are written as tests against this
real path and reuse :func:`boundary_check`, which is also the production
verifier for CSR-011's real generator.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from . import SPEC_VERSION, clock, constants, db, migrations, views, warm_start
from .config import RunConfig
from .enums import RunStatus
from .errors import EngineError, ErrorCode
from .ledger import Ledger


def open_run_db(path: str | Path) -> sqlite3.Connection:
    """Open a run database at ``path``: migrate (no-op on re-open), recreate
    the whitelisted views, and verify the golden hash (spec 02 §9/§3.1,
    decision 6). A failure closes the connection before re-raising.

    ``migrate`` already maps its failures to :data:`ErrorCode.MIGRATION_FAILED`;
    the view-DDL and hash steps sit under the same wrapper, so a raw
    ``sqlite3`` failure there (a view body that no longer parses, ...) surfaces
    the same stable code instead of the raw exception (final-branch audit F3).
    """
    conn = db.connect(path)
    try:
        migrations.migrate(conn)
        views.create_views(conn)
        views.verify_views(conn)
    except EngineError:
        conn.close()
        raise
    except Exception as exc:
        conn.close()
        raise EngineError(
            ErrorCode.MIGRATION_FAILED,
            "failed to open run database",
        ) from exc
    return conn


def _opening_instant() -> int:
    """The run's opening instant: ``constants.run_start`` at the 06:45
    opening (spec 02 §8.1: the world is a going concern on 2026-10-01T06:45)."""
    d = date.fromisoformat(constants.RUN_START)
    return clock.phoenix_instant(d.year, d.month, d.day, constants.OPENING_HOUR, constants.OPENING_MINUTE)


def _abort_failed(conn: sqlite3.Connection | None, run_id: str) -> None:
    """The botched-startup exit (spec 02 §12 / decision 12): report the run
    ``failed`` if a run record exists, roll back any open transaction, close.

    Every step is best-effort — the original exception is what propagates, so
    a cleanup failure can never mask it. ``initializing -> failed`` is a legal
    transition (db.py); once the run row is committed, this is a plain
    autocommit UPDATE.
    """
    if conn is None:
        return
    try:
        if conn.in_transaction:
            db.rollback(conn)
        if db.get_run(conn, run_id) is not None:
            db.set_run_status(conn, run_id, RunStatus.FAILED)
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def boundary_check(conn: sqlite3.Connection, config: RunConfig) -> None:
    """The WS-01 clean-boundary verifier (spec 02 §8.5; decision 10).

    Each rule is a zero-row assertion against the live schema, so the same
    function is the production verifier for CSR-011's real generator; a
    violation raises :data:`ErrorCode.INVARIANT_VIOLATED` and the init
    sequence reports the run ``failed`` (spec 02 §12). Rules checked:

    - §8.5 rule 1 — no appointment is open (``scheduled``/``dispatched``/
      ``in_progress``) and none is scheduled at or after the boundary *day
      start* — the comparison boundary is 2026-10-01T00:00 Phoenix (derived
      from the run's own start date), not the 06:45 opening instant: an
      appointment in the pre-opening hours would still be an inherited
      liability (n1);
    - §8.5 rule 2 — every invoice has a matching payment (a ``NOT EXISTS``
      probe on ``_payments``);
    - §8.5 rule 3 — every callback reached a terminal state: NOT representable
      in today's schema (callback semantics arrive with CSR-008), so this rule
      is deferred to that ticket and the check is silent on it;
    - §8.5 rule 4 — no call is ``active``;
    - §8.5 rule 5 — no open hiring request, no scheduled/in-progress training
      reservation, no scheduled non-job appointment;
    - §8.5 rule 6 — no technician is pending/notice (``hired_pending_start``,
      ``pending_termination``, ``resigning_notice``) or ``sick``, and no
      absence row is ``active``;
    - §8.5 rule 7 — no block hold at or after the boundary day start;
    - §8.5 rule 8 — every on-call rotation is ``active``: the probe is
      ``status != 'active'``, not ``= 'vacant'`` — an ``ended`` rotation is
      just as much a boundary violation as a vacant one (final-branch audit
      F2);
    - §8.7 — cash equals ``finance.starting_cash``.
    """
    run_row = conn.execute("SELECT start_at_ms FROM _run LIMIT 1").fetchone()
    if run_row is None:
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, "clean-boundary check: no run record")
    start_local = clock.phoenix_local(int(run_row["start_at_ms"]))
    boundary_day_start_ms = clock.phoenix_instant(
        start_local.year, start_local.month, start_local.day, 0, 0
    )
    rules: list[tuple[str, bool]] = [
        (
            "no open appointments (§8.5 rule 1)",
            conn.execute(
                "SELECT 1 FROM _appointments WHERE status IN "
                "('scheduled','dispatched','in_progress') "
                "OR scheduled_start >= ? LIMIT 1",
                (boundary_day_start_ms,),
            ).fetchone() is None,
        ),
        (
            "every invoice has a matching payment (§8.5 rule 2)",
            conn.execute(
                "SELECT 1 FROM _invoices i WHERE NOT EXISTS "
                "(SELECT 1 FROM _payments p WHERE p.invoice_id = i.id) LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no active calls (§8.5 rule 4)",
            conn.execute(
                "SELECT 1 FROM _calls WHERE status = 'active' LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no open hiring requests (§8.5 rule 5)",
            conn.execute(
                "SELECT 1 FROM _hiring_requests WHERE status = 'open' LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no scheduled or in-progress training reservations (§8.5 rule 5)",
            conn.execute(
                "SELECT 1 FROM _training_reservations WHERE status IN "
                "('scheduled','in_progress') LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no scheduled non-job appointments (§8.5 rule 5)",
            conn.execute(
                "SELECT 1 FROM _non_job_appointments WHERE status = 'scheduled' LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no pending/notice or sick technicians (§8.5 rule 6)",
            conn.execute(
                "SELECT 1 FROM _technicians WHERE employment_status IN "
                "('hired_pending_start','pending_termination','resigning_notice') "
                "OR availability_status = 'sick' LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no active technician absences (§8.5 rule 6)",
            conn.execute(
                "SELECT 1 FROM _technician_absences WHERE status = 'active' LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no block holds at or after the boundary (§8.5 rule 7)",
            conn.execute(
                "SELECT 1 FROM _technician_block_holds WHERE block_start >= ? LIMIT 1",
                (boundary_day_start_ms,),
            ).fetchone() is None,
        ),
        (
            "no non-active on-call rotations (§8.5 rule 8)",
            conn.execute(
                "SELECT 1 FROM _on_call_rotations WHERE status != 'active' LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "cash equals finance.starting_cash (spec 02 §8.7)",
            Ledger(conn).cash_balance() == config.get_int("finance.starting_cash"),
        ),
    ]
    for label, holds in rules:
        if not holds:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"warm-start clean-boundary violation: {label}",
            )


def initialize_run(
    path: str | Path,
    run_id: str | None = None,
    *,
    master_seed: int,
    config: RunConfig,
    dataset_version: str = "",
) -> str:
    """Build a run database at ``path`` and leave it ``active``; return the
    run id (decision 12).

    Open -> run record + config + visibility registry (one transaction, the
    clock seeded at the opening instant) -> warm start -> clean-boundary check
    -> ``active``. Any exception aborts via :func:`_abort_failed` and is
    re-raised; a path that already contains a run record is refused with
    :data:`ErrorCode.STATE_ILLEGAL` — a partially built or completed database
    is never reused (spec 02 §12), so a clean re-init requires a fresh file.
    """
    run_id = run_id or db.new_run_id()
    conn: sqlite3.Connection | None = None
    try:
        conn = open_run_db(path)
        if conn.execute("SELECT 1 FROM _run LIMIT 1").fetchone() is not None:
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                "refusing to initialize onto a database that already contains a run "
                "record: a partially built or completed database is never reused "
                "(spec 02 §12) — use a fresh file",
            )
        start_at_ms = _opening_instant()
        db.begin(conn)
        try:
            db.insert_run(
                conn,
                run_id=run_id,
                spec_version=SPEC_VERSION,
                config_version=config.config_version,
                config_hash=config.config_hash,
                master_seed=int(master_seed),
                start_at_ms=start_at_ms,
                first_service_date=constants.RUN_START,
                last_service_date=constants.RUN_END,
                timezone=constants.PHOENIX_TIMEZONE,
                status=RunStatus.INITIALIZING,
            )
            db.write_config(
                conn, run_id, config.config_version, config.canonical_bytes.decode()
            )
            db.populate_visibility_registry(conn)
            db.commit(conn)
        except Exception:
            db.rollback(conn)
            raise
        warm_start.run(conn, run_id, master_seed, config, dataset_version)
        boundary_check(conn, config)
        db.set_run_status(conn, run_id, RunStatus.ACTIVE)
        conn.close()
        return run_id
    except Exception:
        _abort_failed(conn, run_id)
        raise


__all__ = ["boundary_check", "initialize_run", "open_run_db"]
