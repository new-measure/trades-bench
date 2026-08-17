from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from . import (
    SPEC_VERSION,
    clock,
    constants,
    db,
    demand,
    geo_reader,
    migrations,
    views,
    warm_start,
    weather,
)
from .config import RunConfig
from .enums import EventKind, RunStatus
from .errors import EngineError, ErrorCode
from .events import Events
from .ledger import Ledger
from .rng import Rng, sqlite_audit_recorder


def _stored_config_keys(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT canonical_json FROM _config LIMIT 1").fetchone()
    if row is None:
        return None
    return json.loads(row["canonical_json"]).get("keys", {})


def _verify_stored_geography_artifact(
    conn: sqlite3.Connection, artifact_dir: str | Path | None
) -> None:
    keys = _stored_config_keys(conn)
    if keys is None:
        return
    expected_sha256 = str(keys.get("geography.artifact_sha256", ""))
    if not expected_sha256:
        return
    if artifact_dir is None:
        raise EngineError(
            ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
            "this database has a pinned geography.artifact_sha256 but open_run_db was "
            "not given an artifact_dir to re-verify it against — the hash Must be "
            "re-checked at every open, so the check cannot be skipped by omission",
            details={"expected_sha256": expected_sha256},
        )
    geo_reader.verify_artifact_sha256(artifact_dir, expected_sha256)


def open_run_db(
    path: str | Path, *, artifact_dir: str | Path | None = None
) -> sqlite3.Connection:
    conn = db.connect(path)
    try:
        migrations.migrate(conn)
        views.create_views(conn)
        views.verify_views(conn)
        db.begin(conn)
        try:
            db.populate_visibility_registry(conn)
            db.commit(conn)
        except Exception:
            db.rollback(conn)
            raise
        _verify_stored_geography_artifact(conn, artifact_dir)
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


def _opening_instant(config: RunConfig) -> int:
    d = date.fromisoformat(config.get_string("run.start_date"))
    return clock.phoenix_instant(d.year, d.month, d.day, constants.OPENING_HOUR, constants.OPENING_MINUTE)


def _abort_failed(conn: sqlite3.Connection | None, run_id: str) -> None:
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


def _schedule_first_rollover(
    conn: sqlite3.Connection, master_seed: int, start_at_ms: int
) -> None:
    rng = Rng(
        int(master_seed),
        SPEC_VERSION,
        audit_recorder=sqlite_audit_recorder(conn),
        conn=conn,
    )
    db.begin(conn)
    rng.enter_txn()
    try:
        Events(conn, rng).schedule(EventKind.DAY_ROLLOVER, start_at_ms)
    except Exception:
        rng.exit_txn()
        if conn.in_transaction:
            db.rollback(conn)
        raise
    else:
        rng.exit_txn()
        db.commit(conn)


def boundary_check(conn: sqlite3.Connection, config: RunConfig) -> None:
    run_row = conn.execute("SELECT start_at_ms FROM _run LIMIT 1").fetchone()
    if run_row is None:
        raise EngineError(ErrorCode.INVARIANT_VIOLATED, "clean-boundary check: no run record")
    start_local = clock.phoenix_local(int(run_row["start_at_ms"]))
    boundary_day_start_ms = clock.phoenix_instant(
        start_local.year, start_local.month, start_local.day, 0, 0
    )
    rules: list[tuple[str, bool]] = [
        (
            "no open appointments",
            conn.execute(
                "SELECT 1 FROM _appointments WHERE status IN "
                "('scheduled','dispatched','in_progress') "
                "OR scheduled_start >= ? LIMIT 1",
                (boundary_day_start_ms,),
            ).fetchone() is None,
        ),
        (
            "every invoice has a matching payment",
            conn.execute(
                "SELECT 1 FROM _invoices i WHERE NOT EXISTS "
                "(SELECT 1 FROM _payments p WHERE p.invoice_id = i.id) LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no active calls",
            conn.execute(
                "SELECT 1 FROM _calls WHERE status = 'active' LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no open hiring requests",
            conn.execute(
                "SELECT 1 FROM _hiring_requests WHERE status = 'open' LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no scheduled or in-progress training reservations",
            conn.execute(
                "SELECT 1 FROM _training_reservations WHERE status IN "
                "('scheduled','in_progress') LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no scheduled non-job appointments",
            conn.execute(
                "SELECT 1 FROM _non_job_appointments WHERE status = 'scheduled' LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no pending/notice or sick technicians",
            conn.execute(
                "SELECT 1 FROM _technicians WHERE employment_status IN "
                "('hired_pending_start','pending_termination','resigning_notice') "
                "OR availability_status = 'sick' LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no active technician absences",
            conn.execute(
                "SELECT 1 FROM _technician_absences WHERE status = 'active' LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "no block holds at or after the boundary",
            conn.execute(
                "SELECT 1 FROM _technician_block_holds WHERE block_start >= ? LIMIT 1",
                (boundary_day_start_ms,),
            ).fetchone() is None,
        ),
        (
            "no non-active on-call rotations",
            conn.execute(
                "SELECT 1 FROM _on_call_rotations WHERE status != 'active' LIMIT 1"
            ).fetchone() is None,
        ),
        (
            "cash equals finance.starting_cash",
            Ledger(conn).cash_balance() == config.get_int("finance.starting_cash"),
        ),
    ]
    for label, holds in rules:
        if not holds:
            raise EngineError(
                ErrorCode.INVARIANT_VIOLATED,
                f"warm-start clean-boundary violation: {label}",
            )


def _verify_and_load_geography_artifact(
    config: RunConfig, dataset_version: str, artifact_dir: str | Path | None
) -> tuple[str, geo_reader.GeoArtifact]:
    expected_sha256 = config.get_string("geography.artifact_sha256")
    computed_sha256 = geo_reader.verify_artifact_sha256(artifact_dir, expected_sha256)
    artifact = geo_reader.load_artifact(artifact_dir)
    manifest_artifact_id = str(artifact.manifest.get("artifact_id", ""))
    configured_artifact_id = config.get_string("geography.artifact_id")
    if configured_artifact_id and configured_artifact_id != manifest_artifact_id:
        raise EngineError(
            ErrorCode.GEOGRAPHY_ARTIFACT_MISMATCH,
            "geography.artifact_id does not match the loaded artifact's manifest "
            "artifact_id",
            details={
                "configured_artifact_id": configured_artifact_id,
                "manifest_artifact_id": manifest_artifact_id,
            },
        )
    if dataset_version and dataset_version != manifest_artifact_id:
        raise EngineError(
            ErrorCode.INVARIANT_VIOLATED,
            "dataset_version does not match the loaded geography artifact's "
            "artifact_id",
            details={
                "dataset_version": dataset_version,
                "manifest_artifact_id": manifest_artifact_id,
            },
        )
    resolved_dataset_version = dataset_version or configured_artifact_id or manifest_artifact_id
    geo_reader.verify_manifest_consistency(artifact, computed_sha256, resolved_dataset_version)
    return resolved_dataset_version, artifact


def initialize_run(
    path: str | Path,
    run_id: str | None = None,
    *,
    master_seed: int,
    config: RunConfig,
    dataset_version: str = "",
    artifact_dir: str | Path | None = None,
) -> str:
    run_id = run_id or db.new_run_id()
    conn: sqlite3.Connection | None = None
    try:
        conn = open_run_db(path, artifact_dir=artifact_dir)
        if conn.execute("SELECT 1 FROM _run LIMIT 1").fetchone() is not None:
            raise EngineError(
                ErrorCode.STATE_ILLEGAL,
                "refusing to initialize onto a database that already contains a run "
                "record: a partially built or completed database is never reused — "
                "use a fresh file",
            )
        start_at_ms = _opening_instant(config)
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
                first_service_date=config.get_string("run.start_date"),
                last_service_date=config.get_string("run.end_date"),
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
        resolved_dataset_version, artifact = _verify_and_load_geography_artifact(
            config, dataset_version, artifact_dir
        )
        warm_start.run(
            conn, run_id, master_seed, config, resolved_dataset_version, artifact=artifact
        )
        weather.precompute(
            conn,
            config,
            Rng(
                int(master_seed),
                SPEC_VERSION,
                audit_recorder=sqlite_audit_recorder(conn),
                conn=conn,
            ),
            created_at=start_at_ms,
        )
        demand.initialize_state(conn, config, created_at=start_at_ms)
        _schedule_first_rollover(conn, master_seed, start_at_ms)
        boundary_check(conn, config)
        db.set_run_status(conn, run_id, RunStatus.ACTIVE)
        conn.close()
        return run_id
    except Exception:
        _abort_failed(conn, run_id)
        raise


__all__ = ["boundary_check", "initialize_run", "open_run_db"]
