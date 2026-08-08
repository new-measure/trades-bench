"""Shared test fixtures for the CSR-001 suite.

A small connection registry + autouse closer so the per-test helper functions that
open in-memory databases do not leak ``sqlite3.Connection`` objects (the suite
runs with ``filterwarnings = ["error"]``, which would otherwise turn unclosed
connection ResourceWarnings into failures).
"""

from __future__ import annotations

import pytest

from _helpers import _OPEN_CONNS


def register_conn(conn) -> None:  # sqlite3.Connection
    """Register a connection opened by a test helper for teardown-close."""
    _OPEN_CONNS.append(conn)


@pytest.fixture(autouse=True)
def _close_open_conns():
    yield
    while _OPEN_CONNS:
        conn = _OPEN_CONNS.pop()
        try:
            if conn.in_transaction:
                conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
