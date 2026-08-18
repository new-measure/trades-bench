from __future__ import annotations

import hashlib
import importlib.resources
import re
import sqlite3
from pathlib import Path

from .db import rollback
from .errors import EngineError, ErrorCode

MIGRATIONS_PACKAGE_DIR = Path(str(importlib.resources.files("trades_bench") / "schema"))

_MIGRATION_FILE = re.compile(r"^(\d{4})_(.+)\.sql$")


def _checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _bootstrap(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            checksum    TEXT NOT NULL,
            applied_at  INTEGER NOT NULL
        )
        """
    )


def _discover(migrations_path: Path) -> list[tuple[int, str, bytes]]:
    if not migrations_path.is_dir():
        raise EngineError(
            ErrorCode.MIGRATION_FAILED,
            f"migrations path {str(migrations_path)!r} does not exist "
            f"or is not a directory",
        )
    found: list[tuple[int, str, bytes]] = []
    for entry in sorted(migrations_path.iterdir()):
        if entry.is_dir():
            continue
        if entry.name == "__init__.py":
            continue
        match = _MIGRATION_FILE.match(entry.name)
        if match is None:
            raise EngineError(
                ErrorCode.MIGRATION_FAILED,
                f"malformed migration filename {entry.name!r} "
                f"(expected NNNN_name.sql)",
            )
        found.append((int(match.group(1)), match.group(2), entry.read_bytes()))
    found.sort(key=lambda item: item[0])
    versions = [item[0] for item in found]
    if len(set(versions)) != len(versions):
        raise EngineError(
            ErrorCode.MIGRATION_FAILED,
            f"duplicate migration version in {str(migrations_path)!r}: {versions}",
        )
    return found


def applied_versions(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    return [
        (int(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT version, name FROM _schema_migrations ORDER BY version"
        )
    ]


def _apply(conn: sqlite3.Connection, version: int, name: str, content: bytes) -> None:
    checksum = _checksum(content)
    escaped_name = name.replace("'", "''")
    try:
        script = (
            "BEGIN;\n"
            + content.decode("utf-8")
            + "\nINSERT INTO _schema_migrations(version, name, checksum, applied_at) "
            f"VALUES({version}, '{escaped_name}', '{checksum}', 0);\n"
            "COMMIT;\n"
        )
        conn.executescript(script)
    except Exception as exc:
        try:
            rollback(conn)
        except sqlite3.Error:
            pass
        raise EngineError(
            ErrorCode.MIGRATION_FAILED,
            f"migration {version:04d}_{name} failed to apply",
        ) from exc


def migrate(
    conn: sqlite3.Connection,
    *,
    migrations_path: Path | None = None,
) -> list[str]:
    if conn.in_transaction:
        raise EngineError(
            ErrorCode.MIGRATION_FAILED,
            "migrate() requires no open transaction on the connection",
        )
    path = migrations_path if migrations_path is not None else MIGRATIONS_PACKAGE_DIR
    discovered = _discover(path)
    by_version = {version: (name, content) for version, name, content in discovered}
    applied_rows: list[tuple] = []
    record_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_schema_migrations'"
    ).fetchone()
    if record_table is not None:
        _bootstrap(conn)
        applied_rows = conn.execute(
            "SELECT version, name, checksum FROM _schema_migrations ORDER BY version"
        ).fetchall()
    if not by_version and not applied_rows:
        raise EngineError(
            ErrorCode.MIGRATION_FAILED,
            f"no migration files found in {str(path)!r}",
        )
    for row in applied_rows:
        version, recorded_name, recorded_checksum = int(row[0]), str(row[1]), str(row[2])
        entry = by_version.get(version)
        if entry is None:
            raise EngineError(
                ErrorCode.MIGRATION_CHECKSUM_MISMATCH,
                f"migration {version:04d}_{recorded_name} is recorded as applied "
                f"but its file is missing from {str(path)!r}",
            )
        disk_name, disk_content = entry
        if disk_name != recorded_name or _checksum(disk_content) != recorded_checksum:
            raise EngineError(
                ErrorCode.MIGRATION_CHECKSUM_MISMATCH,
                f"migration {version:04d} on disk no longer matches the applied "
                f"record (name or content checksum changed)",
            )
    recorded_versions = {int(row[0]) for row in applied_rows}
    newly_applied: list[str] = []
    for version, name, content in discovered:
        if version in recorded_versions:
            continue
        _apply(conn, version, name, content)
        newly_applied.append(f"{version:04d}_{name}")
    return newly_applied


__all__ = ["MIGRATIONS_PACKAGE_DIR", "applied_versions", "migrate"]
