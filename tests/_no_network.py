from __future__ import annotations

import contextlib
import socket


class NetworkAccessBlocked(RuntimeError):
    pass


def _blocked_socket(*args, **kwargs):
    raise NetworkAccessBlocked("network access attempted inside a no-network guarded block")


@contextlib.contextmanager
def block_network():
    original = socket.socket
    socket.socket = _blocked_socket
    try:
        yield
    finally:
        socket.socket = original
