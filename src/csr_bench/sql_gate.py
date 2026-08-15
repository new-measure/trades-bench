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

_VIEW_INTERNAL_PRIVATE_READS: dict[str, frozenset[tuple[str, str]]] = {
    "technician_locations": frozenset(
        {
            ("_technician_locations", "trip_origin_latitude"),
            ("_technician_locations", "trip_origin_longitude"),
            ("_technician_locations", "trip_destination_latitude"),
            ("_technician_locations", "trip_destination_longitude"),
            ("_technician_locations", "trip_departed_at"),
            ("_technician_locations", "trip_travel_minutes"),
            ("_config", "canonical_json"),
        }
    ),
}

_PROTECTED_VIEW_NAMES: frozenset[str] = frozenset(
    name.lower() for name in _VIEW_INTERNAL_PRIVATE_READS
)


def _authorize_read(
    table: str | None, column: str | None, trigger_name: str | None
) -> int:
    if not table or not column:
        return sqlite3.SQLITE_DENY
    if table.startswith("_"):
        try:
            if classify((table, column)) in VisibilityClass.public():
                return sqlite3.SQLITE_OK
        except Exception:
            pass
        allowed = _VIEW_INTERNAL_PRIVATE_READS.get(trigger_name or "")
        if allowed is not None and (table, column) in allowed:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if column in _VIEW_COLUMNS.get(table, ()):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None, trigger_name: str | None) -> int:
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ:
        return _authorize_read(arg1, arg2, trigger_name)
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


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _skip_ws_comments(sql: str, i: int) -> int:
    n = len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            i = _skip_until(sql, i, "\n")
        elif sql.startswith("/*", i):
            i = _skip_until(sql, i, "*/")
        else:
            break
    return i


def _read_ident(sql: str, i: int) -> tuple[str, int]:
    n = len(sql)
    if i < n and sql[i] in "\"'`":
        quote = sql[i]
        end = _skip_quoted(sql, i, quote)
        return sql[i + 1:end - 1], end
    if i < n and sql[i] == "[":
        end = _skip_until(sql, i, "]")
        return sql[i + 1:end - 1], end
    j = i
    while j < n and _is_ident_char(sql[j]):
        j += 1
    return sql[i:j], j


def _skip_balanced_parens(sql: str, i: int) -> int:
    n = len(sql)
    depth = 0
    while i < n:
        c = sql[i]
        if c in "'\"`":
            i = _skip_quoted(sql, i, c)
            continue
        if c == "[":
            i = _skip_until(sql, i, "]")
            continue
        if sql.startswith("--", i):
            i = _skip_until(sql, i, "\n")
            continue
        if sql.startswith("/*", i):
            i = _skip_until(sql, i, "*/")
            continue
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            depth -= 1
            i += 1
            if depth == 0:
                return i
            continue
        i += 1
    return i


def _reject_cte_clause_names(sql: str, i: int) -> None:
    n = len(sql)
    i = _skip_ws_comments(sql, i)
    ident2, j2 = _read_ident(sql, i)
    if ident2.upper() == "RECURSIVE":
        i = _skip_ws_comments(sql, j2)
    while True:
        name, i = _read_ident(sql, i)
        if not name:
            return
        if name.lower() in _PROTECTED_VIEW_NAMES:
            raise EngineError.unauthorized()
        i = _skip_ws_comments(sql, i)
        if i < n and sql[i] == "(":
            i = _skip_balanced_parens(sql, i)
            i = _skip_ws_comments(sql, i)
        as_ident, i2 = _read_ident(sql, i)
        if as_ident.upper() != "AS":
            return
        i = _skip_ws_comments(sql, i2)
        modifier_ident, i3 = _read_ident(sql, i)
        if modifier_ident.upper() in ("MATERIALIZED", "NOT"):
            i = _skip_ws_comments(sql, i3)
            if modifier_ident.upper() == "NOT":
                _not_materialized, i4 = _read_ident(sql, i)
                i = _skip_ws_comments(sql, i4)
        if i >= n or sql[i] != "(":
            return
        i = _skip_balanced_parens(sql, i)
        i = _skip_ws_comments(sql, i)
        if i < n and sql[i] == ",":
            i = _skip_ws_comments(sql, i + 1)
            continue
        return


def _reject_cte_name_collisions(sql: str) -> None:
    n = len(sql)
    i = 0
    while i < n:
        c = sql[i]
        if c in "'\"`":
            i = _skip_quoted(sql, i, c)
            continue
        if c == "[":
            i = _skip_until(sql, i, "]")
            continue
        if sql.startswith("--", i):
            i = _skip_until(sql, i, "\n")
            continue
        if sql.startswith("/*", i):
            i = _skip_until(sql, i, "*/")
            continue
        if _is_ident_char(c):
            ident, j = _read_ident(sql, i)
            if ident.upper() == "WITH":
                _reject_cte_clause_names(sql, j)
            i = j
            continue
        i += 1


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
    _reject_cte_name_collisions(sql)


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
