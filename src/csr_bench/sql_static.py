from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import views
from .sql_gate import (
    _ALLOWED_FUNCTIONS,
    _VIEW_COLUMNS,
    _VIEW_INTERNAL_FUNCTIONS,
    _VIEW_INTERNAL_PRIVATE_READS,
)
from .wire import WireCode

__all__ = [
    "SqlRejected",
    "check_static",
    "open_shadow_connection",
    "validate_relations",
]


class SqlRejected(Exception):

    __slots__ = ("wire_code",)

    def __init__(self, wire_code: WireCode) -> None:
        self.wire_code = wire_code
        super().__init__(wire_code.value)


@dataclass(frozen=True)
class Token:

    kind: str
    text: str


def _is_ident_start(c: str) -> bool:
    return c.isalpha() or c == "_"


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _read_number(sql: str, i: int) -> int:
    n = len(sql)
    if sql[i] == "0" and i + 1 < n and sql[i + 1] in "xX":
        j = i + 2
        while j < n and sql[j] in "0123456789abcdefABCDEF":
            j += 1
        return j
    j = i
    while j < n and sql[j].isdigit():
        j += 1
    if j < n and sql[j] == ".":
        j += 1
        while j < n and sql[j].isdigit():
            j += 1
    if j < n and sql[j] in "eE":
        k = j + 1
        if k < n and sql[k] in "+-":
            k += 1
        if k < n and sql[k].isdigit():
            j = k
            while j < n and sql[j].isdigit():
                j += 1
    return j


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
            i = j + 2
            continue
        if c == "'":
            j = i + 1
            closed = False
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    closed = True
                    break
                j += 1
            if not closed:
                raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
            tokens.append(Token("string", sql[i:j]))
            i = j
            continue
        if c in ("x", "X") and i + 1 < n and sql[i + 1] == "'":
            j = sql.find("'", i + 2)
            if j < 0:
                raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
            tokens.append(Token("blob", sql[i:j + 1]))
            i = j + 1
            continue
        if c in ('"', "`"):
            quote = c
            j = i + 1
            closed = False
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                        continue
                    j += 1
                    closed = True
                    break
                j += 1
            if not closed:
                raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
            tokens.append(Token("ident", sql[i + 1:j - 1]))
            i = j
            continue
        if c == "[":
            j = sql.find("]", i + 1)
            if j < 0:
                raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
            tokens.append(Token("ident", sql[i + 1:j]))
            i = j + 1
            continue
        if _is_ident_start(c):
            j = i + 1
            while j < n and _is_ident_char(sql[j]):
                j += 1
            tokens.append(Token("bareword", sql[i:j]))
            i = j
            continue
        if c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = _read_number(sql, i)
            tokens.append(Token("number", sql[i:j]))
            i = j
            continue
        tokens.append(Token("punct", c))
        i += 1
    return tokens


def _split_statements(tokens: list[Token]) -> list[list[Token]]:
    groups: list[list[Token]] = [[]]
    for tok in tokens:
        if tok.kind == "punct" and tok.text == ";":
            groups.append([])
        else:
            groups[-1].append(tok)
    return groups


def _check_single_statement(tokens: list[Token]) -> list[Token]:
    groups = _split_statements(tokens)
    if not groups[0]:
        raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
    if any(groups[1:]):
        raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
    return groups[0]


def _at(tokens: list[Token], i: int) -> Token | None:
    return tokens[i] if 0 <= i < len(tokens) else None


def _keyword(tok: Token | None, word: str) -> bool:
    return tok is not None and tok.kind == "bareword" and tok.text.upper() == word


def _skip_parens(tokens: list[Token], i: int) -> int:
    n = len(tokens)
    if i >= n or tokens[i].kind != "punct" or tokens[i].text != "(":
        raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
    depth = 0
    while i < n:
        tok = tokens[i]
        if tok.kind == "punct" and tok.text == "(":
            depth += 1
            i += 1
            continue
        if tok.kind == "punct" and tok.text == ")":
            depth -= 1
            i += 1
            if depth == 0:
                return i
            continue
        i += 1
    raise SqlRejected(WireCode.SQL_NOT_PERMITTED)


def _skip_cte_clause(tokens: list[Token], i: int) -> tuple[int, list[str]]:
    names: list[str] = []
    if _keyword(_at(tokens, i), "RECURSIVE"):
        i += 1
    while True:
        head = _at(tokens, i)
        if head is None or head.kind not in ("bareword", "ident"):
            raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
        names.append(head.text.lower())
        i += 1
        if _at(tokens, i) is not None and tokens[i].kind == "punct" and tokens[i].text == "(":
            i = _skip_parens(tokens, i)
        if not _keyword(_at(tokens, i), "AS"):
            raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
        i += 1
        if _keyword(_at(tokens, i), "NOT"):
            i += 1
            if not _keyword(_at(tokens, i), "MATERIALIZED"):
                raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
            i += 1
        elif _keyword(_at(tokens, i), "MATERIALIZED"):
            i += 1
        i = _skip_parens(tokens, i)
        nxt = _at(tokens, i)
        if nxt is not None and nxt.kind == "punct" and nxt.text == ",":
            i += 1
            continue
        return i, names


def _check_top_level_select(tokens: list[Token]) -> None:
    head = _at(tokens, 0)
    if head is None or head.kind != "bareword":
        raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
    keyword = head.text.upper()
    if keyword == "SELECT":
        return
    if keyword != "WITH":
        raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
    i, _ = _skip_cte_clause(tokens, 1)
    if not _keyword(_at(tokens, i), "SELECT"):
        raise SqlRejected(WireCode.SQL_NOT_PERMITTED)


def _decode_identifier_text(tok: Token) -> str:
    if tok.kind == "string":
        return tok.text[1:-1].replace("''", "'")
    if tok.kind == "blob":
        payload = tok.text[2:-1]
        try:
            return bytes.fromhex(payload).decode("latin-1")
        except ValueError:
            return ""
    return tok.text


_RELATION_INTRODUCERS: frozenset[str] = frozenset({"from", "join", "in"})


def _in_relation_position(prev: Token | None) -> bool:
    if prev is None:
        return False
    if prev.kind == "punct":
        return prev.text == "."
    return prev.kind == "bareword" and prev.text.lower() in _RELATION_INTRODUCERS


def _check_identifiers(tokens: list[Token]) -> None:
    prev: Token | None = None
    for tok in tokens:
        if tok.kind in ("punct", "number"):
            prev = tok
            continue
        if tok.kind in ("string", "blob") and not _in_relation_position(prev):
            prev = tok
            continue
        text = _decode_identifier_text(tok)
        if text.startswith("_"):
            raise SqlRejected(WireCode.UNKNOWN_RELATION)
        lowered = text.lower()
        if lowered.startswith("sqlite_") or lowered.startswith("pragma_"):
            raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
        prev = tok


_WALL_CLOCK_FUNCTIONS: frozenset[str] = frozenset(
    {"date", "time", "datetime", "julianday", "strftime", "unixepoch", "timediff"}
)

_WALL_CLOCK_MODIFIERS: frozenset[str] = frozenset({"now", "localtime", "utc"})

_WALL_CLOCK_MIN_ARGS: dict[str, int] = {
    "date": 1,
    "time": 1,
    "datetime": 1,
    "julianday": 1,
    "unixepoch": 1,
    "strftime": 2,
    "timediff": 2,
}


def _count_top_level_args(tokens: list[Token], start: int, end: int) -> int:
    if start >= end:
        return 0
    depth = 0
    count = 1
    for tok in tokens[start:end]:
        if tok.kind == "punct" and tok.text == "(":
            depth += 1
        elif tok.kind == "punct" and tok.text == ")":
            depth -= 1
        elif tok.kind == "punct" and tok.text == "," and depth == 0:
            count += 1
    return count


def _contains_concatenation(tokens: list[Token], start: int, end: int) -> bool:
    for j in range(start, end - 1):
        if (
            tokens[j].kind == "punct" and tokens[j].text == "|"
            and tokens[j + 1].kind == "punct" and tokens[j + 1].text == "|"
        ):
            return True
    return False


def _check_no_wall_clock_reads(tokens: list[Token]) -> None:
    n = len(tokens)
    i = 0
    while i < n:
        tok = tokens[i]
        nxt = _at(tokens, i + 1)
        if (
            tok.kind == "bareword"
            and tok.text.lower() in _WALL_CLOCK_FUNCTIONS
            and nxt is not None
            and nxt.kind == "punct"
            and nxt.text == "("
        ):
            close = _skip_parens(tokens, i + 1)
            name = tok.text.lower()
            arg_start, arg_end = i + 2, close - 1
            if _count_top_level_args(tokens, arg_start, arg_end) < _WALL_CLOCK_MIN_ARGS[name]:
                raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
            if _contains_concatenation(tokens, arg_start, arg_end):
                raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
            for arg in tokens[arg_start:arg_end]:
                if arg.kind != "string":
                    continue
                if _decode_identifier_text(arg).lower() in _WALL_CLOCK_MODIFIERS:
                    raise SqlRejected(WireCode.SQL_NOT_PERMITTED)
            i = close
            continue
        i += 1


_PROTECTED_RELATION_NAMES: frozenset[str] = frozenset(
    name.lower() for name in views.VIEW_WHITELIST
)


def _reject_shadowed_relations(sql: str) -> None:
    try:
        tokens = tokenize(sql)
    except SqlRejected:
        return
    for index, tok in enumerate(tokens):
        if not _keyword(tok, "WITH"):
            continue
        try:
            _, names = _skip_cte_clause(tokens, index + 1)
        except SqlRejected:
            continue
        if any(name in _PROTECTED_RELATION_NAMES for name in names):
            raise SqlRejected(WireCode.SQL_NOT_PERMITTED)


def check_static(sql: str) -> None:
    tokens = tokenize(sql)
    statement = _check_single_statement(tokens)
    _check_top_level_select(statement)
    _check_identifiers(statement)
    _check_no_wall_clock_reads(statement)
    _reject_shadowed_relations(sql)


class _ShadowAuthorizer:

    __slots__ = ("denial",)

    def __init__(self) -> None:
        self.denial: WireCode | None = None

    def __call__(
        self,
        action: int,
        arg1: str | None,
        arg2: str | None,
        db_name: str | None,
        trigger_name: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ:
            return self._read(arg1, arg2, trigger_name)
        if action == sqlite3.SQLITE_FUNCTION:
            return self._function(arg2, trigger_name)
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        return self._deny(WireCode.SQL_NOT_PERMITTED)

    def _deny(self, wire_code: WireCode) -> int:
        self.denial = wire_code
        return sqlite3.SQLITE_DENY

    def _read(self, table: str | None, column: str | None, trigger_name: str | None) -> int:
        if not table:
            return self._deny(WireCode.SQL_NOT_PERMITTED)
        if table.lower().startswith("sqlite_"):
            return self._deny(WireCode.SQL_NOT_PERMITTED)
        if table.startswith("_"):
            if column and trigger_name in views.VIEW_WHITELIST:
                allowed = _VIEW_INTERNAL_PRIVATE_READS.get(trigger_name)
                if allowed is not None and (table, column) in allowed:
                    return sqlite3.SQLITE_OK
            return self._deny(WireCode.UNKNOWN_RELATION)
        if table not in views.VIEW_WHITELIST:
            return self._deny(WireCode.UNKNOWN_RELATION)
        if not column:
            return sqlite3.SQLITE_OK
        if column not in _VIEW_COLUMNS.get(table, frozenset()):
            return self._deny(WireCode.UNKNOWN_COLUMN)
        return sqlite3.SQLITE_OK

    def _function(self, name: str | None, trigger_name: str | None) -> int:
        if not name:
            return self._deny(WireCode.SQL_NOT_PERMITTED)
        lowered = name.lower()
        if lowered in _ALLOWED_FUNCTIONS:
            return sqlite3.SQLITE_OK
        if trigger_name in views.VIEW_WHITELIST and lowered in _VIEW_INTERNAL_FUNCTIONS:
            return sqlite3.SQLITE_OK
        return self._deny(WireCode.SQL_NOT_PERMITTED)


class _ShadowConnection(sqlite3.Connection):

    def execute(self, sql: str, parameters=()) -> sqlite3.Cursor:
        _reject_shadowed_relations(sql)
        return super().execute(sql, parameters)


def open_shadow_connection(db_path: str | Path) -> sqlite3.Connection:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(
        uri,
        uri=True,
        isolation_level=None,
        check_same_thread=False,
        factory=_ShadowConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _classify_compile_error(exc: sqlite3.Error) -> WireCode:
    message = str(exc).lower()
    if "no such table" in message or "no such view" in message:
        return WireCode.UNKNOWN_RELATION
    if "no such column" in message:
        return WireCode.UNKNOWN_COLUMN
    return WireCode.SQL_NOT_PERMITTED


def validate_relations(conn: sqlite3.Connection, sql: str) -> None:
    _reject_shadowed_relations(sql)
    tracker = _ShadowAuthorizer()
    conn.set_authorizer(tracker)
    try:
        conn.execute("EXPLAIN " + sql)
    except sqlite3.Error as exc:
        if tracker.denial is not None:
            raise SqlRejected(tracker.denial) from None
        raise SqlRejected(_classify_compile_error(exc)) from None
    finally:
        conn.set_authorizer(None)
