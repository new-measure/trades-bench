"""Forward-only, checksummed SQL migration runner (spec 02 §9; CSR-002 decisions 1, 16).

The run database's schema is applied by numbered ``NNNN_name.sql`` files from
the package ``schema/`` directory (spec 02 §9: "Runtime code Must not issue DDL
outside the migration runner" — the one exception, view creation from the
registry, lives in the views module). Every successful application is recorded
in ``_schema_migrations(version, name, checksum, applied_at)``; every open
first verifies each recorded row against the current file on disk, so a missing
file or a content-checksum mismatch is a startup failure (the tamper guard) and
re-applying an already-applied version is a no-op.

``applied_at`` is the constant 0 — "before world time began" — because the
runner never reads a clock or wall time: identical builds must produce
byte-identical databases (WS-05 / dump determinism).

The runner must be called with no open transaction on ``conn`` — enforced,
because ``executescript`` implicitly commits any pending transaction before
running its script, which would commit a caller's partial work mid-migration.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import re
import sqlite3
from pathlib import Path

from .db import rollback
from .errors import EngineError, ErrorCode

#: The default migration source: the package's ``schema/`` directory, resolved
#: through importlib.resources so it works however the package is installed
#: (it is package data, not an importable module). ``str()`` keeps the
#: expression a plain ``Path`` (files() is typed ``Traversable``).
MIGRATIONS_PACKAGE_DIR = Path(str(importlib.resources.files("csr_bench") / "schema"))

#: Migration filenames are exactly ``NNNN_name.sql`` with a 4-digit numeric
#: prefix; anything else in the directory is a packaging error, not a file to
#: silently skip.
_MIGRATION_FILE = re.compile(r"^(\d{4})_(.+)\.sql$")


def _checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _bootstrap(conn: sqlite3.Connection) -> None:
    # Identical DDL to the copy migration 0001 creates, with IF NOT EXISTS so
    # the runner can bootstrap before or after the file applies. The two
    # copies must stay in lockstep.
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
    """Return ``(version, name, content_bytes)`` for every migration file.

    Sorted by version. A nonexistent path, a malformed filename, or two files
    with the same version is a packaging error and aborts.
    """
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
    """Ordered ``(version, name)`` pairs recorded in ``_schema_migrations``."""
    return [
        (int(row[0]), str(row[1]))
        for row in conn.execute(
            "SELECT version, name FROM _schema_migrations ORDER BY version"
        )
    ]


def _apply(conn: sqlite3.Connection, version: int, name: str, content: bytes) -> None:
    """Apply one migration file and record it, atomically.

    The transaction is opened inside the script text rather than via
    ``conn.execute("BEGIN")`` first: Python 3.13's ``executescript`` still
    implicitly commits any pending transaction in legacy ``isolation_level=None``
    mode, which would silently end an explicit BEGIN and leave the script's
    statements running in autocommit — partial application on failure. With
    BEGIN/COMMIT inside the script, SQLite itself commits the DDL and the
    ``_schema_migrations`` row in one transaction, and the record row lives in
    the same script so it can never exist without the DDL it describes.
    Migration files must therefore never issue their own BEGIN/COMMIT.
    """
    checksum = _checksum(content)
    escaped_name = name.replace("'", "''")
    try:
        # applied_at = 0: no clock exists before the world begins; the constant
        # keeps identical builds byte-identical (determinism note above). The
        # decode sits inside the protected path so a corrupt file surfaces as
        # MIGRATION_FAILED like every other migration-file failure.
        script = (
            "BEGIN;\n"
            + content.decode("utf-8")
            + "\nINSERT INTO _schema_migrations(version, name, checksum, applied_at) "
            f"VALUES({version}, '{escaped_name}', '{checksum}', 0);\n"
            "COMMIT;\n"
        )
        conn.executescript(script)
    except Exception as exc:
        # A statement error leaves the open transaction untouched, so the
        # partial DDL must be rolled back by hand before re-raising.
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
    """Apply all pending migrations; return the ``"NNNN_name"`` labels applied.

    On a database that has been opened before (the record table exists) every
    recorded migration is verified against the current files on disk — the spec
    02 §9 tamper guard, checked before anything new is applied — then pending
    files are applied in version order. On a fresh database there is nothing
    recorded to verify and the record table does not exist yet: migration 0001
    creates it, so the runner must not pre-create it there (the file's plain
    ``CREATE TABLE`` would then fail). Re-applying an already-applied set
    returns ``[]``.

    The default source is :data:`MIGRATIONS_PACKAGE_DIR`; tests pass a custom
    path to feed corrupted copies (decision 16). Must be called with no
    transaction open on ``conn`` — enforced at the top.
    """
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
        # Re-open path: the record table already exists (migration 0001 or an
        # earlier open created it). Bootstrap tolerates that; verify that every
        # recorded row still matches a file on disk.
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
