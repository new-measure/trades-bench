from __future__ import annotations

import sqlite3
from pathlib import Path

from . import views
from .enums import VisibilityClass
from .errors import EngineError, ErrorCode
from .visibility import classify

_ALLOWED_ACTIONS: frozenset[int] = frozenset(
    {
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_RECURSIVE,
        sqlite3.SQLITE_FUNCTION,
    }
)

_VIEW_COLUMNS: dict[str, frozenset[str]] = {
    spec.name: frozenset(column.name for column in spec.columns)
    for spec in views.VIEWS
}


def _authorize_read(table: str | None, column: str | None) -> int:
    if not table or not column:
        return sqlite3.SQLITE_DENY
    if table.startswith("_"):
        try:
            if classify((table, column)) in VisibilityClass.public():
                return sqlite3.SQLITE_OK
        except Exception:
            pass
        return sqlite3.SQLITE_DENY
    if column in _VIEW_COLUMNS.get(table, ()):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None, trigger_name: str | None) -> int:
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ:
        return _authorize_read(arg1, arg2)
    return sqlite3.SQLITE_OK


def open_agent_session(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorizer)
    return conn


def _skip_quoted(sql: str, i: int, quote: str) -> int:
    n = len(sql)
    i += 1
    while i < n:
        if sql[i] != quote:
            i += 1
            continue
        if i + 1 < n and sql[i + 1] == quote:
            i += 2
            continue
        return i + 1
    return n


def _skip_until(sql: str, i: int, marker: str) -> int:
    j = sql.find(marker, i)
    return len(sql) if j < 0 else j + len(marker)


def _skip_insignificant(sql: str, i: int) -> int:
    n = len(sql)
    while i < n:
        c = sql[i]
        if c.isspace() or c == ";":
            i += 1
        elif sql.startswith("--", i):
            i = _skip_until(sql, i, "\n")
        elif sql.startswith("/*", i):
            i = _skip_until(sql, i, "*/")
        else:
            return i
    return i


def _has_second_statement(sql: str) -> bool:
    n = len(sql)
    i = 0
    while i < n:
        c = sql[i]
        if c in "'\"`":
            i = _skip_quoted(sql, i, c)
        elif c == "[":
            i = _skip_until(sql, i, "]")
        elif sql.startswith("--", i):
            i = _skip_until(sql, i, "\n")
        elif sql.startswith("/*", i):
            i = _skip_until(sql, i, "*/")
        elif c == ";":
            j = _skip_insignificant(sql, i + 1)
            if j < n:
                return True
            i = j
        else:
            i += 1
    return False


def _reject_payload(sql: str) -> None:
    text = sql.lstrip()
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith("--", i):
            i = _skip_until(text, i, "\n")
        elif text.startswith("/*", i):
            i = _skip_until(text, i, "*/")
        else:
            break
    j = i
    while j < n and (text[j].isalnum() or text[j] == "_"):
        j += 1
    if text[i:j].upper() == "EXPLAIN":
        raise EngineError.unauthorized()
    if _has_second_statement(sql):
        raise EngineError.unauthorized()


def _uniform_error(exc: sqlite3.Error) -> EngineError:
    if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_AUTH:
        return EngineError.unauthorized()
    message = str(exc)
    if (
        "syntax error" in message
        or "unrecognized token" in message
        or "incomplete input" in message
        or "You can only execute one statement at a time" in message
    ):
        return EngineError(ErrorCode.INVALID_ARGUMENT, "invalid query")
    return EngineError.unauthorized()


def execute_gated(conn: sqlite3.Connection, sql: str) -> sqlite3.Cursor:
    _reject_payload(sql)
    try:
        return conn.execute(sql)
    except sqlite3.Error as exc:
        raise _uniform_error(exc) from None


__all__ = ["execute_gated", "open_agent_session"]
