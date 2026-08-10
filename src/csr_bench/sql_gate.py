"""In-process SQL authorization gate for agent sessions (spec 09 VIS-01..06;
CSR-002 decision 13).

The agent gets a *separate* connection (:func:`open_agent_session`) that is
read-only twice over: ``PRAGMA query_only`` as defence in depth, and SQLite's
authorizer callback as the real gate. :func:`execute_gated` is the wrapper the
agent-facing surface calls; it converts every rejection into the one uniform
:data:`ErrorCode.UNAUTHORIZED` error and refuses multi-statement and EXPLAIN
payloads wholesale.

The whitelist is exactly the 31 views of :data:`csr_bench.views.VIEW_WHITELIST`
(spec 02 §3.2). SQLite's own parser resolves quoting, case, schema
qualification, and nesting tricks before the callback fires, so the gate
decides on *resolved* names — no table-name text matching anywhere (the one
exception, EXPLAIN, is explained below; SQLite has no authorizer action for
it).

Authorizer rule table, checked in order:

1. Action not in ``{READ, SELECT, RECURSIVE, FUNCTION}`` → deny.
   PRAGMA, INSERT/UPDATE/DELETE, every CREATE_*/DROP_*/ALTER, ATTACH/DETACH,
   ANALYZE, SAVEPOINT, TRANSACTION (BEGIN/COMMIT/ROLLBACK — a held-open read
   transaction on the agent connection would block engine writes with
   SQLITE_BUSY; final-branch audit F5), and any action code not seen before →
   deny.
2. READ:
   - table or column missing/empty → deny (defensive; unknown shapes).
   - table starts with ``_`` → allow iff the column classifies as public in
     :mod:`csr_bench.visibility` (the enforceable form of the underscore
     rule — see the deviation note below).
   - otherwise → allow iff the table is a whitelisted view and the column is
     one of that view's declared columns (VIS-03 non-view columns; the
     column set comes from ``views.ViewSpec.columns``, which includes the
     computed ``*_iso`` siblings).
3. SELECT / RECURSIVE / FUNCTION → allow.

Two SQLite-forced corrections to decision 13's sketch (verified on SQLite
3.50.4; precedent: CSR-002 decision 14 documents a similar mechanism
correction):

- **FUNCTION is allowed.** SQLite fires ``SQLITE_FUNCTION`` for *built-in*
  functions (``COUNT``, ``SUM``, ``COALESCE``, ``strftime``, ...) — the
  whitelisted view bodies and legitimate agent queries use them, so denying
  the action would break ``SELECT COUNT(*) FROM customers`` and every
  ``company_state`` read. The engine registers no functions of its own
  (views.py renders the ISO siblings with ``strftime`` precisely so nothing
  custom is callable by agent SQL), so a function trick has no hidden-data
  primitive to call: any read it would perform is denied at the READ level.
- **The underscore rule is column-level, not table-level.** SQLite fires
  ``SQLITE_READ`` for a view's *internal* reads of its underscore base tables
  with exactly the same arguments as a direct query of that table. A flat
  deny on ``_``-prefixed names would reject every legitimate view query, and
  the callback cannot tell the two apart. The enforceable form: ``_``-table
  reads are allowed only for columns classified public. Every hidden table
  of the VIS corpus (``_technician_true_skill``, ``_customer_repeat_state``,
  ``_skills``, ``_rng_state``, and peers) is private in every column, so
  direct access is denied uniformly whether or not the table exists. Known
  remaining gap, documented for CSR-012: a direct query of explicit public
  columns of a ``_`` table (e.g. ``SELECT id, name FROM _customers``)
  succeeds, because its reads are indistinguishable from view internals.
  ``SELECT *`` still trips on the private ``origin`` column, and a lone
  rowid-alias read (``SELECT id FROM _customers``) trips SQLite's
  empty-column rowid probe, so the gap is narrower than the rule table
  suggests. Closing it fully needs a statement-level check the callback
  cannot express.

EXPLAIN. SQLite has no authorizer action for EXPLAIN (the inner statement's
actions do fire, so EXPLAIN of a hidden read is denied anyway, but EXPLAIN of
a public query succeeds). ``execute_gated`` therefore refuses any payload
whose first token is ``EXPLAIN`` — the one deliberate text check, justified
because no callback hook exists; it raises the same uniform error as every
other denial, so the refusal itself leaks nothing. The token is read as an
*identifier* after skipping whitespace and comments, so comment-adjacent
spellings (``EXPLAIN--c``, ``EXPLAIN/*c*/`` — both lexed as the keyword by
SQLite) cannot slip past a whitespace-based split.

Multi-statement payloads (VIS-04). ``sqlite3`` executes only the first
statement and silently discards the rest — later statements would go
completely unexamined. ``execute_gated`` scans the payload lexically
(skipping string literals, quoted and bracket identifiers, and comments) and
refuses the whole payload when a top-level ``;`` is followed by anything
other than whitespace, comments, or further terminators. Trailing
separators alone are insignificant, so ``SELECT 1; ;`` passes the scan —
Python ≥3.12's sqlite3 then refuses the payload itself with
``ProgrammingError("You can only execute one statement at a time")``, which
:func:`_uniform_error` maps to the stable ``invalid query`` (it is a
payload-shape failure, not an authorization event; final-branch audit F7).
Real authorizer denials still surface as ``unauthorized``.

Error surface (VIS-05). Every authorizer denial raises :class:`EngineError`
with :data:`ErrorCode.UNAUTHORIZED` and the constant message ``"unauthorized"``
— byte-identical for every shape. The criterion for the code-1
(``SQLITE_ERROR``) failures of a gated connection: *parse failures* (SQLite's
``near ...: syntax error``, ``unrecognized token``, ``incomplete input``) are
ordinary query failures and become the stable ``INVALID_ARGUMENT`` /
``"invalid query"``; every other code-1 failure is a refusal shape and is
folded into the uniform denial, because surfacing it as-is would let a probe
tell hidden from nonexistent:

- ``no such table: ...`` / ``no such column: ...`` — the resolver reports a
  name that does not exist; must be indistinguishable from a denied hidden
  table (spec 01 §Visibility) or a non-view column (VIS-03).
- ``no such index: ...`` / ``no such trigger: ...`` — same rule for
  schema objects.
- ``cannot modify ... because it is a view`` — SQLite rejects DML on a view
  *before* consulting the authorizer (verified on 3.50.4; ``INSERT`` on a
  view does fire and is denied normally).
- the DDL-on-view refusals (``Cannot add a column to a view``, ``views may
  not be indexed``, ``view ... may not be altered``, trigger creation on a
  view) and any other code-1 message — a refused operation is a refusal.

All conversions raise ``from None``: the underlying SQLite message names
tables and columns, and no part of it may ride on the engine error.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import views
from .enums import VisibilityClass
from .errors import EngineError, ErrorCode
from .visibility import classify

#: Actions the gate allows. SQLITE_FUNCTION is here per the deviation note
#: above; everything else SQLite can fire is denied by absence from the set
#: (including actions this module has never seen — deny is the safe default).
#: SQLITE_TRANSACTION is deliberately absent: no legitimate single-statement
#: autocommit read fires it, and allowing it would let the agent open a read
#: transaction that blocks engine writes with SQLITE_BUSY (final-branch audit
#: F5).
_ALLOWED_ACTIONS: frozenset[int] = frozenset(
    {
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_RECURSIVE,
        sqlite3.SQLITE_FUNCTION,
    }
)

#: The whitelisted views' declared columns, by view name. Built from the
#: registry so it can never drift from ``views.py`` (spec 02 §3.1: the
#: registry is the single source for public column names). Includes the
#: computed ``*_iso`` siblings — the view bodies and the agent both read them.
_VIEW_COLUMNS: dict[str, frozenset[str]] = {
    spec.name: frozenset(column.name for column in spec.columns)
    for spec in views.VIEWS
}


def _authorize_read(table: str | None, column: str | None) -> int:
    """Decide one ``SQLITE_READ``: rule 2 of the table above.

    Must never raise — a raised callback aborts the whole statement. Every
    lookup failure (unknown field, missing column, malformed args) denies.
    """
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
    """The SQLite authorizer callback for agent sessions (rule table above)."""
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ:
        return _authorize_read(arg1, arg2)
    return sqlite3.SQLITE_OK


def open_agent_session(db_path: str | Path) -> sqlite3.Connection:
    """Open a separate, gated connection to the run database for the agent.

    A dedicated connection, never the engine's (the authorizer is
    per-connection — test 7 in ``tests/test_sql_gate.py`` pins that the
    engine side is unaffected). ``PRAGMA query_only`` is set *before* the
    authorizer is installed — the PRAGMA itself would be denied. The
    authorizer is the real gate; ``query_only`` is defence in depth.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorizer)
    return conn


def _skip_quoted(sql: str, i: int, quote: str) -> int:
    """Skip a ``'...'`` string or ``"..."``/backtick identifier from ``i``.

    Doubled quotes (``'it''s'``) are escaped, not terminators. An unterminated
    literal consumes to the end and is left for SQLite to reject.
    """
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
    """Skip to just past the first ``marker`` at or after ``i`` (or EOF)."""
    j = sql.find(marker, i)
    return len(sql) if j < 0 else j + len(marker)


def _skip_insignificant(sql: str, i: int) -> int:
    """Skip whitespace, comments, and stray semicolons from ``i``.

    Used after a statement terminator: only these may follow it without
    starting a second statement.
    """
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
    """True when a top-level ``;`` is followed by more SQL (VIS-04).

    Semicolons inside string literals, quoted/bracket identifiers, and
    comments do not count — only a terminator at nesting depth zero.
    """
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
    """Refuse EXPLAIN and multi-statement payloads before any execution.

    Both raise the uniform denial so the refusal itself never reveals that a
    separate code path exists. EXPLAIN is the one deliberate text check in
    the module: SQLite has no authorizer action for it, so no callback can
    decide it. The first token is read as an *identifier* after skipping
    whitespace and comments — a whitespace split would let comment-adjacent
    spellings through (``EXPLAIN--c`` and ``EXPLAIN/*c*/`` are lexed by
    SQLite as the EXPLAIN keyword followed by a comment, no whitespace
    needed), while the identifier read stops at the first non-identifier
    character and can only match the keyword itself.
    """
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
    """Fold a SQLite failure into the gate's stable error surface (VIS-05).

    ``SQLITE_AUTH`` covers every authorizer denial (``not authorized`` and
    SQLite's ``access to <table>.<column> is prohibited`` variant for denied
    reads). Among the code-1 failures only *parse failures* are ordinary
    query failures (stable ``invalid query``); every other code-1 shape on a
    gated connection is a refusal — nonexistent objects, non-view columns,
    DML/DDL against views — and must be indistinguishable from a denial
    (spec 01 §Visibility; VIS-05).
    """
    if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_AUTH:
        return EngineError.unauthorized()
    message = str(exc)
    if (
        "syntax error" in message
        or "unrecognized token" in message
        or "incomplete input" in message
        # Python ≥3.12's sqlite3 refuses any payload with a trailing
        # statement — including ones whose only trailing content is
        # insignificant separators, which the lexical scan passes (VIS-04).
        # A payload-shape failure is a query failure, not an authorization
        # event (final-branch audit F7).
        or "You can only execute one statement at a time" in message
    ):
        return EngineError(ErrorCode.INVALID_ARGUMENT, "invalid query")
    return EngineError.unauthorized()


def execute_gated(conn: sqlite3.Connection, sql: str) -> sqlite3.Cursor:
    """Execute one statement through the gated connection.

    The payload is refused wholesale when it is EXPLAIN or multi-statement;
    then every SQLite failure is converted to the gate's stable errors, never
    echoing the SQL text. ``from None`` keeps the underlying SQLite message —
    which names tables and columns — off the engine error entirely.
    """
    _reject_payload(sql)
    try:
        return conn.execute(sql)
    except sqlite3.Error as exc:
        raise _uniform_error(exc) from None


__all__ = ["execute_gated", "open_agent_session"]
