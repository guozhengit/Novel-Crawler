from __future__ import annotations

from pathlib import Path

import pytest

from novel_crawler.acquisition.security import UrlSafetyPolicy
from novel_crawler.adaptation.config_schema import SiteConfig
from novel_crawler.browser.sessions import BrowserSessionStore
from novel_crawler.core.domains import canonical_domain


def _config(domain: str) -> SiteConfig:
    return SiteConfig.from_dict({
        "schema_version": 1,
        "config_id": "cfg_0123456789abcdef",
        "site": "example",
        "domain": domain,
        "url_patterns": ["/book/{int}"],
        "selectors": {"clean": [], "book": {"title": "h1", "chapter_list": "a"}, "chapter": {"title": "h1", "content": "article"}},
        "request_policy": {"timeout_seconds": 1.0, "max_retries": 0, "rate_limit_seconds": 0.0},
        "generated_at": "2026-07-11T00:00:00Z",
        "last_validated": "2026-07-11T00:00:00Z",
        "field_scores": {},
        "validation_samples": [],
        "fingerprint_salt": "11" * 32,
    })


def test_nontransitional_domain_identity_is_shared_across_layers(tmp_path: Path) -> None:
    expected = "xn--fa-hia.de"
    assert canonical_domain("faß.de") == expected
    assert _config("faß.de").domain == expected
    target = UrlSafetyPolicy(resolver=lambda host, port: ("93.184.216.34",)).validate("https://faß.de/")
    assert target.host == expected
    with BrowserSessionStore(tmp_path / "sessions").acquire("faß.de") as lease:
        assert lease.info.domain == expected


def test_joiner_is_rejected_consistently_across_layers(tmp_path: Path) -> None:
    invalid = "a\u200db.example"
    with pytest.raises(ValueError):
        canonical_domain(invalid)
    with pytest.raises(ValueError):
        _config(invalid)
    with pytest.raises(ValueError):
        BrowserSessionStore(tmp_path / "sessions").acquire(invalid)
    with pytest.raises(ValueError):
        UrlSafetyPolicy(resolver=lambda host, port: ()).validate(f"https://{invalid}/")
