from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

ALLOW_THIRD_PARTY_ENV = "NOVEL_CRAWLER_ALLOW_THIRD_PARTY"


@dataclass(frozen=True)
class ComplianceDecision:
    allowed: bool
    code: str
    reason: str


def third_party_allowed() -> bool:
    return os.getenv(ALLOW_THIRD_PARTY_ENV, "").strip().casefold() in {"1", "true", "yes", "allow"}


def is_local_or_test_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    host = parts.hostname.rstrip(".").casefold()
    if host in {"localhost", "example.com", "example.org", "example.net"} or host.endswith(".localhost"):
        return True
    if host.endswith((".test", ".example", ".invalid")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def decide_third_party_access(url: str) -> ComplianceDecision:
    if is_local_or_test_url(url):
        return ComplianceDecision(True, "local_or_test_url", "local/test targets are allowed")
    if third_party_allowed():
        return ComplianceDecision(True, "third_party_explicitly_enabled", "operator explicitly enabled third-party access")
    return ComplianceDecision(
        False,
        "third_party_crawl_disabled",
        f"third-party crawling is disabled by default; set {ALLOW_THIRD_PARTY_ENV}=1 or pass the CLI flag only for authorized targets",
    )


DISCLAIMER = (
    "Use only for content you are legally allowed to access, copy, store, and transform. "
    "Respect robots.txt, site terms, copyright, rate limits, and access controls. "
    "Do not bypass CAPTCHAs, login walls, paywalls, DRM, or anti-abuse systems."
)
