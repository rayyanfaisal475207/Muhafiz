"""
Unit tests for src/pipeline/url_safety.py (audit finding F-04).

`is_domain_allowed()` replaces the raw `domain in url` substring test that
let a hostile URL slip past the web-search domain allowlist as long as an
allowed domain string appeared *anywhere* in it.
"""
import pytest

from src.pipeline.url_safety import is_domain_allowed

ALLOWED = ["gov.pk", "dawn.com"]


@pytest.mark.parametrize("url", [
    "https://gov.pk/a",
    "https://www.gov.pk/a",
    "https://islamabadpolice.gov.pk/a",
    "https://dawn.com/article",
    "https://news.dawn.com/article",
])
def test_allows_exact_and_subdomain_matches(url):
    assert is_domain_allowed(url, ALLOWED) is True


@pytest.mark.parametrize("url", [
    "https://dawn.com.attacker.tld/a",
    "https://evil.example/?ref=gov.pk",
    "https://evil.example/gov.pk/path",
    "https://evil.example/#dawn.com",
    "https://evil.example/?to=dawn.com",
    "http://192.0.2.1/gov.pk",
    "not a url at all gov.pk",
    "",
    "evilgov.pk",
])
def test_rejects_substring_bypass(url):
    assert is_domain_allowed(url, ALLOWED) is False
