from __future__ import annotations

import sqlite3
from pathlib import Path

from . import views
from .errors import EngineError, ErrorCode

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

_ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        "count", "sum", "avg", "min", "max", "total", "group_concat",
        "abs", "round", "sign", "ceil", "ceiling", "floor", "power", "sqrt",
        "exp", "ln", "log", "log2", "log10", "mod", "pi",
        "length", "lower", "upper", "substr", "substring", "trim", "ltrim",
        "rtrim", "replace", "instr", "printf", "format", "hex", "quote",
        "char", "unicode", "like", "glob",
        "date", "time", "datetime", "julianday", "strftime", "unixepoch",
        "timediff",
        "coalesce", "ifnull", "nullif", "iif", "typeof",
    }
)

_VIEW_INTERNAL_FUNCTIONS: frozenset[str] = frozenset({"json_extract"})

_SPECIAL_CASE_VIEW_NAMES: frozenset[str] = frozenset({"technician_locations", "company_state"})


def _standard_view_reads(spec: views.ViewSpec) -> frozenset[tuple[str, str]]:
    pairs = {
        (column.source.table, column.source.column)
        for column in spec.columns
        if column.source is not None
    }
    clock_column = "service_date" if "k.service_date" in spec.sql else "current_time_ms"
    pairs.add(("_clock", clock_column))
    return frozenset(pairs)


_VIEW_INTERNAL_PRIVATE_READS: dict[str, frozenset[tuple[str, str]]] = {
    spec.name: _standard_view_reads(spec)
    for spec in views.VIEWS
    if spec.name not in _SPECIAL_CASE_VIEW_NAMES
}

_VIEW_INTERNAL_PRIVATE_READS["technician_locations"] = frozenset(
    {
        ("_clock", "current_time_ms"),
        ("_config", "canonical_json"),
        ("_technician_locations", "technician_id"),
        ("_technician_locations", "location_state"),
        ("_technician_locations", "latitude"),
        ("_technician_locations", "longitude"),
        ("_technician_locations", "coordinates_updated_at"),
        ("_technician_locations", "trip_origin_latitude"),
        ("_technician_locations", "trip_origin_longitude"),
        ("_technician_locations", "trip_destination_latitude"),
        ("_technician_locations", "trip_destination_longitude"),
        ("_technician_locations", "trip_departed_at"),
        ("_technician_locations", "trip_travel_minutes"),
    }
)

_VIEW_INTERNAL_PRIVATE_READS["company_state"] = frozenset(
    {
        ("_clock", "current_time_ms"),
        ("_clock", "service_date"),
        ("_ledger_entries", "amount_cents"),
        ("_ledger_entries", "occurred_at"),
        ("_run", "status"),
    }
)

_PROTECTED_VIEW_NAMES: frozenset[str] = frozenset(
    name.lower() for name in views.VIEW_WHITELIST
)


def _authorize_read(
    table: str | None, column: str | None, trigger_name: str | None
) -> int:
    if not table or not column:
        return sqlite3.SQLITE_DENY
    if table.startswith("_"):
        if trigger_name in views.VIEW_WHITELIST:
            allowed = _VIEW_INTERNAL_PRIVATE_READS.get(trigger_name)
            if allowed is not None and (table, column) in allowed:
                return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if column in _VIEW_COLUMNS.get(table, ()):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _authorize_function(name: str | None, trigger_name: str | None) -> int:
    if not name:
        return sqlite3.SQLITE_DENY
    lowered = name.lower()
    if lowered in _ALLOWED_FUNCTIONS:
        return sqlite3.SQLITE_OK
    if trigger_name in views.VIEW_WHITELIST and lowered in _VIEW_INTERNAL_FUNCTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None, trigger_name: str | None) -> int:
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ:
        return _authorize_read(arg1, arg2, trigger_name)
    if action == sqlite3.SQLITE_FUNCTION:
        return _authorize_function(arg2, trigger_name)
    return sqlite3.SQLITE_OK


class _AgentConnection(sqlite3.Connection):

    def execute(self, sql: str, parameters=()) -> sqlite3.Cursor:
        _reject_cte_name_collisions(sql)
        return super().execute(sql, parameters)


def open_agent_session(db_path: str | Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(
        uri,
        uri=True,
        isolation_level=None,
        check_same_thread=False,
        factory=_AgentConnection,
    )
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
