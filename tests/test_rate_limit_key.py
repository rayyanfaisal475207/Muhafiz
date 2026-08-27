"""
Unit tests for src/auth/routes.py::_rate_limit_key() (audit finding F-09).

slowapi's default get_remote_address keys on the TCP peer address, which
collapses every client behind a reverse proxy into one shared rate-limit
bucket. _rate_limit_key() only trusts X-Forwarded-For when both
config.TRUST_PROXY_HEADERS is on AND the immediate peer is a listed trusted
proxy -- otherwise an untrusted direct client could forge the header and
evade rate limiting entirely.
"""
import pytest

from src import config
from src.auth.routes import _rate_limit_key


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, peer_ip, headers=None):
        self.client = _FakeClient(peer_ip)
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch):
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", False)
    monkeypatch.setattr(config, "TRUSTED_PROXY_IPS", [])


def test_default_uses_peer_address_ignoring_forwarded_header():
    """Flag off (the default): behaves exactly like get_remote_address did."""
    request = _FakeRequest("10.0.0.5", headers={"X-Forwarded-For": "1.2.3.4"})
    assert _rate_limit_key(request) == "10.0.0.5"


def test_trusted_peer_with_flag_on_uses_forwarded_client_ip(monkeypatch):
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)
    monkeypatch.setattr(config, "TRUSTED_PROXY_IPS", ["10.0.0.5"])
    request = _FakeRequest("10.0.0.5", headers={"X-Forwarded-For": "1.2.3.4"})
    assert _rate_limit_key(request) == "1.2.3.4"


def test_trusted_peer_forwarded_chain_uses_first_hop(monkeypatch):
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)
    monkeypatch.setattr(config, "TRUSTED_PROXY_IPS", ["10.0.0.5"])
    request = _FakeRequest("10.0.0.5", headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.9, 10.0.0.5"})
    assert _rate_limit_key(request) == "1.2.3.4"


def test_untrusted_peer_with_flag_on_is_ignored(monkeypatch):
    """
    The core F-09 anti-regression: turning the flag on must not let an
    arbitrary direct client (not in the allowlist) forge X-Forwarded-For
    and evade the limit -- only a listed trusted proxy's forwarded header
    is honored.
    """
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)
    monkeypatch.setattr(config, "TRUSTED_PROXY_IPS", ["10.0.0.5"])
    request = _FakeRequest("203.0.113.9", headers={"X-Forwarded-For": "1.2.3.4"})
    assert _rate_limit_key(request) == "203.0.113.9"


def test_trusted_peer_with_no_forwarded_header_falls_back_to_peer(monkeypatch):
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)
    monkeypatch.setattr(config, "TRUSTED_PROXY_IPS", ["10.0.0.5"])
    request = _FakeRequest("10.0.0.5", headers={})
    assert _rate_limit_key(request) == "10.0.0.5"
