"""
Local fixtures for the compliance suite — kept independent of
tests/conftest.py (this directory is not under `testpaths` and is run as
its own suite; see this package's __init__.py). Same no-network posture as
the main suite: these are static-source and pure-behavioral checks, never
integration tests, so nothing here should ever need a real socket.
"""
import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_socket = socket.socket
    _LOOPBACK = {"127.0.0.1", "::1", "localhost"}

    def _is_loopback(address) -> bool:
        if isinstance(address, tuple) and address:
            return str(address[0]) in _LOOPBACK
        return False

    class _GuardedSocket(real_socket):
        def connect(self, address, *args, **kwargs):
            if not _is_loopback(address):
                raise RuntimeError(
                    "Network access is disabled in the compliance suite — "
                    f"attempted connection to {address!r}"
                )
            return super().connect(address, *args, **kwargs)

        def connect_ex(self, address, *args, **kwargs):
            if not _is_loopback(address):
                raise RuntimeError(
                    "Network access is disabled in the compliance suite — "
                    f"attempted connection to {address!r}"
                )
            return super().connect_ex(address, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", _GuardedSocket)
