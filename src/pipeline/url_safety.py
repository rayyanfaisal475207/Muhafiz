"""
Hostname-safe domain-allowlist matching (audit finding F-04).

`orchestrator.py`'s `_filter_allowed_domains()` and its verbatim port in
`harness/tools/web.py` used to filter with a raw substring test
(`domain in url`), which a hostile URL can defeat trivially:
`evil.example/?ref=gov.pk`, `dawn.com.attacker.tld`, `#dawn.com`, and
`192.0.2.1/gov.pk` all "contain" an allowed domain string without the
request ever going to that domain. This only mattered for Gemini's
grounded-search fallback (Tavily is restricted via its native
`include_domains` API parameter, not this filter), and only if the search
provider itself returns a hostile URL — content-injection, not direct
attacker control — but it's the one real bypass of the web-search
sovereignty guardrail.

`is_domain_allowed()` parses the actual hostname and requires an exact
match or a dot-boundary suffix match, so only `gov.pk` and `*.gov.pk`
(never `evilgov.pk` or `gov.pk.attacker.tld`) pass for an allowlisted
`gov.pk`.
"""
from urllib.parse import urlparse


def is_domain_allowed(url: str, allowed_domains: list[str]) -> bool:
    """True if url's hostname is exactly an allowed domain or a subdomain of one."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in allowed_domains)
