from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.acquisition.http import AcquisitionError
from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.adaptation.config_schema import SiteConfig
from novel_crawler.adaptation.fingerprint import fingerprint_html
from novel_crawler.adaptation.registry import ConfigConflictError, ConfigRegistry, ConfigStatus
from novel_crawler.adaptation.revalidation import ConfigRevalidator, RevalidationResult, RevalidationStatus

INDEX = "https://example.test/book"
C1 = "https://example.test/c1"
C2 = "https://example.test/c2"


def _index(*, wrapper: str = "", text: str = "Book A") -> str:
    links = '<nav class="chapters"><a href="/c1">Chapter 1</a><a href="/c2">Chapter 2</a></nav>'
    return f"<html><body>{wrapper}<h1>{text}</h1>{links}</body></html>"


def _chapter(number: int, *, wrapper: str = "", prose: str = "x" * 100) -> str:
    return (
        f"<html><body>{wrapper}<h1>Chapter {number}</h1><article><p>{prose}</p></article>"
        + (f'<a rel="next" href="/c{number + 1}">Next</a>' if number == 1 else "")
        + '<a class="index" href="/book">Contents</a></body></html>'
    )


def _snapshot(url: str, html: str, status: int = 200) -> PageSnapshot:
    body = html.encode()
    return PageSnapshot(url, url, status, {}, "utf-8", html, body, "GET", (), datetime.now(UTC))


class FakeAcquirer:
    def __init__(self, pages: dict[str, str], error: AcquisitionError | None = None) -> None:
        self.pages = pages
        self.error = error
        self.calls: list[tuple[str, int | None, str | None]] = []

    def fetch_page(self, url: str, *, max_body_bytes: int | None = None, locked_origin: str | None = None) -> AcquiredPage:
        self.calls.append((url, max_body_bytes, locked_origin))
        if self.error:
            raise self.error
        html = self.pages[url]
        if max_body_bytes is not None and len(html.encode()) > max_body_bytes:
            raise AcquisitionError("response_too_large", "https://example.test", False)
        return AcquiredPage(_snapshot(url, html), url)


class AlwaysHighScorer:
    def score_selector(self, field: str, selector: str, snapshot: PageSnapshot, page_kind: object) -> float:
        del field, selector, snapshot, page_kind
        return 0.95


class Noop:
    pass


class MemoryRegistry:
    def __init__(self, config: SiteConfig | None, *, conflict: str | None = None) -> None:
        self.config = config
        self.conflict = conflict
        self.transitions: list[tuple[str, tuple[str, ...]]] = []

    def load(self, entry: object) -> SiteConfig:
        del entry
        if self.config is None:
            raise KeyError("missing")
        return self.config

    def mark_validated(self, config_id: str, checked_at: str, *, expected_version: int) -> None:
        del config_id, checked_at, expected_version
        if self.conflict == "valid":
            raise ConfigConflictError("changed")
        self.transitions.append(("valid", ()))

    def mark_stale(self, config_id: str, *, expected_version: int) -> None:
        del config_id, expected_version
        if self.conflict == "stale":
            raise ConfigConflictError("changed")
        self.transitions.append(("stale", ()))

    def mark_invalid(self, config_id: str, reasons: tuple[str, ...], *, expected_version: int) -> None:
        del config_id, expected_version
        if self.conflict == "invalid":
            raise ConfigConflictError("changed")
        self.transitions.append(("invalid", reasons))


def _config(pages: dict[str, str], *, validated: str = "2026-07-11T08:00:00Z") -> SiteConfig:
    salt = b"s" * 32
    samples = [
        {"page_kind": "book", "fingerprint": fingerprint_html(pages[INDEX], "book", {"title": "h1", "chapter_list": ".chapters a"}, salt).digest},
        {"page_kind": "chapter", "fingerprint": fingerprint_html(pages[C1], "chapter", {"chapter_title": "h1", "content": "article", "next_link": "a[rel=next]", "index_link": "a.index"}, salt).digest},
    ]
    return SiteConfig.from_dict(
        {
            "schema_version": 1,
            "config_id": "cfg_abcdefghijklmnop",
            "site": "Example",
            "domain": "example.test",
            "url_patterns": ["/book", "/*"],
            "selectors": {
                "clean": [],
                "book": {"title": "h1", "chapter_list": ".chapters a"},
                "chapter": {"chapter_title": "h1", "content": "article", "next_link": "a[rel=next]", "index_link": "a.index"},
            },
            "request_policy": {"timeout_seconds": 5, "max_retries": 0, "rate_limit_seconds": 0},
            "generated_at": "2026-07-11T07:00:00Z",
            "last_validated": validated,
            "field_scores": {"title": 0.95, "chapter_list": 0.95, "chapter_title": 0.95, "content": 0.95},
            "validation_samples": samples,
            "fingerprint_salt": salt,
        }
    )


def _service(registry: object, acquirer: FakeAcquirer) -> ConfigRevalidator:
    return ConfigRevalidator(acquirer, Noop(), Noop(), AlwaysHighScorer(), Noop(), registry)


def _entry(config: SiteConfig, *, domain: str | None = None) -> object:
    from novel_crawler.adaptation.registry import RegistryEntry

    return RegistryEntry(config.config_id, domain or config.domain, ConfigStatus.ACTIVE, 1, config.generated_at, config.last_validated)


def _registered(tmp_path: Path, pages: dict[str, str]) -> tuple[ConfigRegistry, object]:
    registry = ConfigRegistry(tmp_path)
    return registry, registry.register(_config(pages))


def test_valid_revalidation_appends_revision_and_only_updates_last_validated(tmp_path: Path) -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    registry, entry = _registered(tmp_path, pages)
    result = _service(registry, FakeAcquirer(pages)).revalidate(entry, INDEX)
    latest = registry.list()[0]
    assert result.status is RevalidationStatus.VALID
    assert latest.version == 2 and latest.status is ConfigStatus.ACTIVE
    assert latest.validated == result.checked_at and latest.validated != entry.validated
    assert registry.load(entry, version=1).last_validated == entry.validated
    assert registry.load(latest).last_validated == result.checked_at


def test_content_only_change_keeps_fingerprints_stable(tmp_path: Path) -> None:
    original = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    current = {**original, C1: _chapter(1, prose="different private prose" * 10), C2: _chapter(2, prose="other text" * 20)}
    registry, entry = _registered(tmp_path, original)
    result = _service(registry, FakeAcquirer(current)).revalidate(entry, INDEX)
    assert result.status is RevalidationStatus.VALID
    assert all(result.fingerprint_matches.values())
    assert "different private prose" not in result.to_json()


def test_layout_drift_with_good_selectors_is_stale_and_never_valid(tmp_path: Path) -> None:
    original = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    current = {INDEX: _index(wrapper="<header></header>"), C1: _chapter(1), C2: _chapter(2)}
    registry, entry = _registered(tmp_path, original)
    result = _service(registry, FakeAcquirer(current)).revalidate(entry, INDEX)
    assert result.status is RevalidationStatus.STALE
    assert registry.list()[0].status is ConfigStatus.STALE
    assert "fingerprint_mismatch" in result.reason_ids


def test_broken_selector_is_invalid_and_marks_registry(tmp_path: Path) -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    payload = _config(pages).to_dict(include_sensitive=True)
    payload["selectors"]["chapter"]["content"] = "article.missing"  # type: ignore[index]
    registry = ConfigRegistry(tmp_path)
    entry = registry.register(SiteConfig.from_dict(payload))
    result = _service(registry, FakeAcquirer(pages)).revalidate(entry, INDEX)
    assert result.status is RevalidationStatus.INVALID
    assert registry.list()[0].status is ConfigStatus.INVALID
    assert "selector_match_invalid" in result.reason_ids


@pytest.mark.parametrize("code", ["timeout", "http_429", "http_500"])
def test_transient_errors_do_not_mutate_registry(tmp_path: Path, code: str) -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    registry, entry = _registered(tmp_path, pages)
    error = AcquisitionError(code, "https://example.test", True)
    result = _service(registry, FakeAcquirer(pages, error)).revalidate(entry, INDEX)
    assert result.status is RevalidationStatus.TRANSIENT_FAILURE
    assert registry.list()[0] == entry


def test_auth_is_stale_and_hard_404_is_invalid(tmp_path: Path) -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    registry, entry = _registered(tmp_path / "auth", pages)
    auth = {INDEX: "<title>Sign in</title><form><input type=password></form>"}
    auth_result = _service(registry, FakeAcquirer(auth)).revalidate(entry, INDEX)
    assert auth_result.status is RevalidationStatus.STALE and auth_result.reason_ids == ("auth_required",)
    assert registry.list()[0].status is ConfigStatus.STALE

    registry2, entry2 = _registered(tmp_path / "hard", pages)
    error = AcquisitionError("http_404", "https://example.test", False)
    hard_result = _service(registry2, FakeAcquirer(pages, error)).revalidate(entry2, INDEX)
    assert hard_result.status is RevalidationStatus.INVALID
    assert registry2.list()[0].status is ConfigStatus.INVALID


def test_budget_and_cross_origin_are_enforced_before_fetch(tmp_path: Path) -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    registry, entry = _registered(tmp_path, pages)
    acquirer = FakeAcquirer(pages)
    result = _service(registry, acquirer).revalidate(entry, "https://other.test/book")
    assert result.status is RevalidationStatus.INVALID
    assert acquirer.calls == []

    registry2, entry2 = _registered(tmp_path / "budget", pages)
    acquirer2 = FakeAcquirer(pages)
    result2 = ConfigRevalidator(acquirer2, Noop(), Noop(), AlwaysHighScorer(), Noop(), registry2, max_revalidation_bytes=1).revalidate(entry2, INDEX)
    assert result2.status is RevalidationStatus.INVALID
    assert len(acquirer2.calls) == 1 and acquirer2.calls[0][1] == 1


def test_result_is_immutable_and_safe() -> None:
    result = RevalidationResult(RevalidationStatus.STALE, ("fingerprint_mismatch",), {"content": 0.9}, {"chapter": False}, "2026-07-11T09:00:00Z")
    with pytest.raises((AttributeError, TypeError)):
        result.reason_ids = ()  # type: ignore[misc]
    serialized = json.loads(result.to_json())
    assert set(serialized) == {"status", "reason_ids", "field_scores", "fingerprint_matches", "checked_at"}
    assert "selector" not in result.to_json() and "https://" not in result.to_json() and "private prose" not in result.to_json()


def test_optimistic_conflict_reloads_and_does_not_downgrade_newer_revision(tmp_path: Path) -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    registry, entry = _registered(tmp_path, pages)
    newer = registry.mark_stale(entry.config_id, expected_version=entry.version)
    with pytest.raises(ConfigConflictError):
        registry.mark_invalid(entry.config_id, ["hard_error"], expected_version=entry.version)
    assert registry.list()[0] == newer


def test_chapter_input_reuses_start_and_fetches_only_index_and_neighbor() -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    config = _config(pages)
    registry = MemoryRegistry(config)
    acquirer = FakeAcquirer(pages)
    result = _service(registry, acquirer).revalidate(_entry(config), C1)  # type: ignore[arg-type]
    assert result.status is RevalidationStatus.VALID
    assert [call[0] for call in acquirer.calls] == [C1, INDEX, C2]


def test_low_scores_are_stale_and_valid_transition_conflict_is_not_retried() -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    config = _config(pages)

    class Low:
        def score_selector(self, field: str, selector: str, snapshot: PageSnapshot, page_kind: object) -> float:
            del field, selector, snapshot, page_kind
            return 0.5

    low_registry = MemoryRegistry(config)
    low = ConfigRevalidator(FakeAcquirer(pages), Noop(), Noop(), Low(), Noop(), low_registry).revalidate(_entry(config), INDEX)  # type: ignore[arg-type]
    assert low.status is RevalidationStatus.STALE and low.reason_ids == ("score_below_threshold",)

    conflict_registry = MemoryRegistry(config, conflict="valid")
    conflict = _service(conflict_registry, FakeAcquirer(pages)).revalidate(_entry(config), INDEX)  # type: ignore[arg-type]
    assert conflict.status is RevalidationStatus.STALE and conflict.reason_ids == ("concurrent_revision",)


def test_configurable_minor_score_drift_still_requires_absolute_high_confidence() -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    config = _config(pages)

    class SlightlyLower:
        def score_selector(self, field: str, selector: str, snapshot: PageSnapshot, page_kind: object) -> float:
            del field, selector, snapshot, page_kind
            return 0.90

    strict = ConfigRevalidator(FakeAcquirer(pages), Noop(), Noop(), SlightlyLower(), Noop(), MemoryRegistry(config))
    assert strict.revalidate(_entry(config), INDEX).status is RevalidationStatus.STALE  # type: ignore[arg-type]
    tolerant = ConfigRevalidator(
        FakeAcquirer(pages),
        Noop(),
        Noop(),
        SlightlyLower(),
        Noop(),
        MemoryRegistry(config),
        minor_drift_tolerance=0.05,
    )
    assert tolerant.revalidate(_entry(config), INDEX).status is RevalidationStatus.VALID  # type: ignore[arg-type]


def test_config_identity_url_and_terminal_hard_errors_fail_closed_before_navigation() -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    config = _config(pages)
    entry = _entry(config)
    missing = _service(MemoryRegistry(None), FakeAcquirer(pages)).revalidate(entry, INDEX)  # type: ignore[arg-type]
    assert missing.status is RevalidationStatus.INVALID and "config_invalid" in missing.reason_ids
    mismatch = _service(MemoryRegistry(config), FakeAcquirer(pages)).revalidate(_entry(config, domain="wrong.test"), INDEX)  # type: ignore[arg-type]
    assert mismatch.reason_ids == ("config_identity_mismatch",)
    malformed = _service(MemoryRegistry(config), FakeAcquirer(pages)).revalidate(entry, "https://example.test:bad/private")  # type: ignore[arg-type]
    assert malformed.reason_ids == ("input_url_invalid",)

    class HardPage(FakeAcquirer):
        def fetch_page(self, url: str, *, max_body_bytes: int | None = None, locked_origin: str | None = None) -> AcquiredPage:
            del max_body_bytes, locked_origin
            return AcquiredPage(_snapshot(url, "<title>not found</title>", 404), url)

    hard = _service(MemoryRegistry(config), HardPage(pages)).revalidate(entry, INDEX)  # type: ignore[arg-type]
    assert hard.status is RevalidationStatus.INVALID and hard.reason_ids == ("hard_error",)


def test_conflicting_stale_and_invalid_transitions_reload_without_downgrade() -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    config = _config(pages)
    drifted = {**pages, INDEX: _index(wrapper="<header></header>")}
    stale = _service(MemoryRegistry(config, conflict="stale"), FakeAcquirer(drifted)).revalidate(_entry(config), INDEX)  # type: ignore[arg-type]
    assert stale.reason_ids == ("fingerprint_mismatch", "concurrent_revision")
    invalid = _service(MemoryRegistry(config, conflict="invalid"), FakeAcquirer(pages)).revalidate(_entry(config), "https://other.test/book")  # type: ignore[arg-type]
    assert invalid.reason_ids == ("domain_mismatch", "concurrent_revision")


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (("bad", (), {}, {}, "2026-07-11T09:00:00Z"), TypeError),
        ((RevalidationStatus.VALID, ("unsafe reason",), {}, {}, "2026-07-11T09:00:00Z"), ValueError),
        ((RevalidationStatus.VALID, (), {"content": float("nan")}, {}, "2026-07-11T09:00:00Z"), ValueError),
        ((RevalidationStatus.VALID, (), {}, {"other": True}, "2026-07-11T09:00:00Z"), ValueError),
        ((RevalidationStatus.VALID, (), {}, {}, "not-a-time"), ValueError),
        ((RevalidationStatus.VALID, (), {}, {}, "2026-07-11T09:00:00"), ValueError),
    ],
)
def test_result_rejects_unsafe_or_malformed_summaries(args: tuple[object, ...], error: type[Exception]) -> None:
    with pytest.raises(error):
        RevalidationResult(*args)  # type: ignore[arg-type]


def test_constructor_and_origin_boundaries_are_strict() -> None:
    with pytest.raises(TypeError):
        ConfigRevalidator()
    for kwargs in ({"max_pages": 2}, {"max_revalidation_bytes": 0}, {"minimum_score": 2}, {"minor_drift_tolerance": -1}):
        with pytest.raises(ValueError):
            ConfigRevalidator(registry=MemoryRegistry(None), **kwargs)  # type: ignore[arg-type]
    assert ConfigRevalidator._origin_key("https://EXAMPLE.test.:443/x") == ("https", "example.test", 443)
    assert ConfigRevalidator._origin_display("https://example.test:444/x") == "https://example.test:444"
    assert ConfigRevalidator._canonical("https://EXAMPLE.test:443/x?q=1") == "https://example.test/x?q=1"
    for url in ("ftp://example.test/x", "https://user:pass@example.test/x", "https://\ud800.test/x"):
        with pytest.raises(ValueError):
            ConfigRevalidator._origin_key(url)


def test_repr_default_scoring_and_invalid_scorer_outputs_are_safe() -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    config = _config(pages)
    entry = _entry(config)
    result = RevalidationResult(RevalidationStatus.VALID, (), {}, {}, "2026-07-11T09:00:00Z")
    assert "selector" not in repr(result)
    default = ConfigRevalidator(registry=MemoryRegistry(config))
    score = default._score("title", "h1", _snapshot(INDEX, pages[INDEX]), PageKind.BOOK_INDEX, 1)
    assert 0 <= score <= 1
    assert default._score("future_field", "h1", _snapshot(INDEX, pages[INDEX]), PageKind.BOOK_INDEX, 1) == 1

    class Missing:
        pass

    class BadDirect:
        def score_selector(self, *args: object) -> bool:
            del args
            return True

    class BadScored:
        score = None

    class BadIndirect:
        def score(self, *args: object) -> BadScored:
            del args
            return BadScored()

    for scorer in (Missing(), BadDirect(), BadIndirect()):
        service = ConfigRevalidator(FakeAcquirer(pages), Noop(), Noop(), scorer, Noop(), MemoryRegistry(config))  # type: ignore[arg-type]
        invalid = service.revalidate(entry, INDEX)  # type: ignore[arg-type]
        assert invalid.status is RevalidationStatus.INVALID


def test_fetch_catalog_and_anchor_guards_cover_every_bounded_boundary() -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    config = _config(pages)
    service = _service(MemoryRegistry(config), FakeAcquirer(pages))
    service._origin = service._origin_key(INDEX)
    page = AcquiredPage(_snapshot(INDEX, pages[INDEX]), INDEX)
    with pytest.raises(ValueError):
        service._catalog_links(page, "")
    invalid_catalog = AcquiredPage(_snapshot(INDEX, "<nav><div href='/c1'></div><div href='/c2'></div></nav>"), INDEX)
    with pytest.raises(ValueError):
        service._catalog_links(invalid_catalog, "nav > *")
    cross_catalog = AcquiredPage(_snapshot(INDEX, "<a href='https://other.test/1'>1</a><a href='/c2'>2</a>"), INDEX)
    with pytest.raises(AcquisitionError):
        service._catalog_links(cross_catalog, "a")
    duplicate_catalog = AcquiredPage(_snapshot(INDEX, "<a href='/c1'>1</a><a href='/c1'>1</a>"), INDEX)
    with pytest.raises(ValueError):
        service._catalog_links(duplicate_catalog, "a")
    assert service._single_anchor(page, ".missing", required=False) is None
    with pytest.raises(ValueError):
        service._single_anchor(page, "h1", required=True)
    cross_anchor = AcquiredPage(_snapshot(INDEX, "<a href='https://other.test/x'>x</a>"), INDEX)
    with pytest.raises(AcquisitionError):
        service._single_anchor(cross_anchor, "a", required=True)

    service._fetches = 3
    with pytest.raises(ValueError):
        service._fetch(INDEX)
    service._fetches = 0
    with pytest.raises(AcquisitionError):
        service._fetch("https://other.test/x")
    service._bytes = service.max_revalidation_bytes
    with pytest.raises(AcquisitionError):
        service._fetch(INDEX)

    class Redirecting(FakeAcquirer):
        def fetch_page(self, url: str, *, max_body_bytes: int | None = None, locked_origin: str | None = None) -> AcquiredPage:
            del url, max_body_bytes, locked_origin
            return AcquiredPage(_snapshot("https://other.test/x", "x"), "https://other.test/x")

    redirected = _service(MemoryRegistry(config), Redirecting(pages))
    redirected._origin = redirected._origin_key(INDEX)
    with pytest.raises(AcquisitionError):
        redirected._fetch(INDEX)

    class Oversized(FakeAcquirer):
        def fetch_page(self, url: str, *, max_body_bytes: int | None = None, locked_origin: str | None = None) -> AcquiredPage:
            del max_body_bytes, locked_origin
            return AcquiredPage(_snapshot(url, "xx"), url)

    oversized = ConfigRevalidator(
        Oversized(pages), Noop(), Noop(), AlwaysHighScorer(), Noop(), MemoryRegistry(config), max_revalidation_bytes=1
    )
    oversized._origin = oversized._origin_key(INDEX)
    with pytest.raises(AcquisitionError):
        oversized._fetch(INDEX)
