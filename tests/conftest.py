from __future__ import annotations

import pytest

from _helpers import _OPEN_CONNS


def register_conn(conn) -> None:
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
