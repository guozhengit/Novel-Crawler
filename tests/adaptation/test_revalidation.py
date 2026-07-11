from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.acquisition.http import AcquisitionError
from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.adaptation.config_schema import SiteConfig
from novel_crawler.adaptation.extractor import CandidateExtractor
from novel_crawler.adaptation.fingerprint import fingerprint_html
from novel_crawler.adaptation.registry import ConfigConflictError, ConfigRegistry, ConfigStatus
from novel_crawler.adaptation.revalidation import (
    ConfigRevalidator,
    RevalidationResult,
    RevalidationStatus,
    _RevalidationRunContext,
)

INDEX = "https://example.test/book"
C1 = "https://example.test/c1"
C2 = "https://example.test/c2"


def _index(*, wrapper: str = "", text: str = "Book A") -> str:
    links = '<div id="chapters">' + "".join(f'<a class="chapter-link" href="/c{number}">Chapter {number}</a>' for number in range(1, 11)) + "</div>"
    return f"<html><body>{wrapper}<h1>{text}</h1>{links}</body></html>"


def _chapter(number: int, *, wrapper: str = "", prose: str = "x" * 100) -> str:
    paragraphs = "".join(f"<p>{prose}</p>" for _ in range(8))
    return (
        f"<html><body>{wrapper}<h1>Chapter {number}</h1><article>{paragraphs}</article>"
        + f'<a rel="next" href="/c{number + 1}">Next</a>'
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

    def mark_validated(self, config_id: str, checked_at: str, *, expected_version: int, expected_status: ConfigStatus) -> None:
        del config_id, checked_at, expected_version, expected_status
        if self.conflict == "valid":
            raise ConfigConflictError("changed")
        self.transitions.append(("valid", ()))

    def mark_stale(self, config_id: str, *, expected_version: int, expected_status: ConfigStatus) -> None:
        del config_id, expected_version, expected_status
        if self.conflict == "stale":
            raise ConfigConflictError("changed")
        self.transitions.append(("stale", ()))

    def mark_invalid(self, config_id: str, reasons: tuple[str, ...], *, expected_version: int, expected_status: ConfigStatus) -> None:
        del config_id, expected_version, expected_status
        if self.conflict == "invalid":
            raise ConfigConflictError("changed")
        self.transitions.append(("invalid", reasons))


def _config(pages: dict[str, str], *, validated: str = "2026-07-11T08:00:00Z") -> SiteConfig:
    salt = b"s" * 32
    extractor = CandidateExtractor()
    book_candidates = {item.field.value: item.selector for item in extractor.extract(_snapshot(INDEX, pages[INDEX]), PageKind.BOOK_INDEX)}
    chapter_candidates = {item.field.value: item.selector for item in extractor.extract(_snapshot(C1, pages[C1]), PageKind.CHAPTER)}
    book_selectors = {key: book_candidates[key] for key in ("title", "chapter_list")}
    chapter_selectors = {key: chapter_candidates[key] for key in ("chapter_title", "content", "next_link", "index_link")}
    samples = [
        {"page_kind": "book", "fingerprint": fingerprint_html(pages[INDEX], "book", book_selectors, salt).to_dict()},
        {"page_kind": "chapter_first", "fingerprint": fingerprint_html(pages[C1], "chapter", chapter_selectors, salt).to_dict()},
        {"page_kind": "chapter_second", "fingerprint": fingerprint_html(pages[C2], "chapter", chapter_selectors, salt).to_dict()},
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
                "book": book_selectors,
                "chapter": chapter_selectors,
            },
            "request_policy": {"timeout_seconds": 5, "max_retries": 0, "rate_limit_seconds": 0},
            "generated_at": "2026-07-11T07:00:00Z",
            "last_validated": validated,
            "field_scores": {"title": 0.91, "chapter_list": 0.90, "chapter_title": 0.96, "content": 0.96},
            "validation_samples": samples,
            "fingerprint_salt": salt,
        }
    )


def _service(registry: object, acquirer: FakeAcquirer) -> ConfigRevalidator:
    return ConfigRevalidator(acquirer=acquirer, registry=registry)  # type: ignore[arg-type]


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
    assert result.fingerprint_matches == {"book": True, "chapter_first": True, "chapter_second": True}


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


def test_second_chapter_only_drift_is_stale_and_chapter_start_checks_input_fingerprint(tmp_path: Path) -> None:
    original = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    second_drift = {**original, C2: _chapter(2, wrapper="<header></header>")}
    registry, entry = _registered(tmp_path / "second", original)
    second_result = _service(registry, FakeAcquirer(second_drift)).revalidate(entry, INDEX)
    assert second_result.status is RevalidationStatus.STALE
    assert second_result.fingerprint_matches["chapter_second"] is False

    first_drift = {**original, C1: _chapter(1, wrapper="<header></header>")}
    registry2, entry2 = _registered(tmp_path / "first", original)
    first_result = _service(registry2, FakeAcquirer(first_drift)).revalidate(entry2, C1)
    assert first_result.status is RevalidationStatus.STALE
    assert first_result.fingerprint_matches["chapter_first"] is False


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


def test_real_scoring_or_aggregate_decision_below_high_confidence_is_stale(tmp_path: Path) -> None:
    original = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    weak = {**original, C2: _chapter(2, prose="short prose")}
    registry, entry = _registered(tmp_path, original)
    result = _service(registry, FakeAcquirer(weak)).revalidate(entry, INDEX)
    assert result.status is RevalidationStatus.STALE
    assert result.reason_ids == ("score_below_threshold",)


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


def test_secondary_auth_and_error_are_classified_before_selector_replay(tmp_path: Path) -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    auth_pages = {**pages, C2: "<title>Sign in</title><form><input type=password></form>"}
    registry, entry = _registered(tmp_path / "auth", pages)
    auth = _service(registry, FakeAcquirer(auth_pages)).revalidate(entry, INDEX)
    assert auth.status is RevalidationStatus.STALE and auth.reason_ids == ("auth_required",)

    error_pages = {**pages, C2: "<title>404 not found</title>"}
    registry2, entry2 = _registered(tmp_path / "error", pages)
    error = _service(registry2, FakeAcquirer(error_pages)).revalidate(entry2, INDEX)
    assert error.status is RevalidationStatus.INVALID and error.reason_ids == ("hard_error",)


def test_revoked_entry_is_terminal_without_fetch_or_mutation(tmp_path: Path) -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    registry, active = _registered(tmp_path, pages)
    revoked = registry.mark_revoked(active.config_id, expected_version=active.version, expected_status=ConfigStatus.ACTIVE)
    acquirer = FakeAcquirer(pages)
    result = _service(registry, acquirer).revalidate(revoked, INDEX)
    assert result.status is RevalidationStatus.INVALID and result.reason_ids == ("config_revoked",)
    assert acquirer.calls == [] and registry.list()[0] == revoked


def test_missing_fingerprint_baseline_is_stale_not_valid(tmp_path: Path) -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    payload = _config(pages).to_dict(include_sensitive=True)
    payload["validation_samples"] = []
    registry = ConfigRegistry(tmp_path)
    entry = registry.register(SiteConfig.from_dict(payload))
    result = _service(registry, FakeAcquirer(pages)).revalidate(entry, INDEX)
    assert result.status is RevalidationStatus.STALE
    assert result.reason_ids == ("fingerprint_baseline_missing",)
    assert registry.list()[0].status is ConfigStatus.STALE


def test_saved_clean_selector_must_remain_a_scored_noise_candidate(tmp_path: Path) -> None:
    noisy = '<aside class="ad-banner">advertisement</aside>'
    original = {INDEX: _index(wrapper=noisy), C1: _chapter(1, wrapper=noisy), C2: _chapter(2, wrapper=noisy)}
    payload = _config(original).to_dict(include_sensitive=True)
    payload["selectors"]["clean"] = [".ad-banner"]  # type: ignore[index]
    payload["field_scores"]["clean_selector"] = 0.5  # type: ignore[index]
    registry = ConfigRegistry(tmp_path)
    entry = registry.register(SiteConfig.from_dict(payload))
    clean_pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    result = _service(registry, FakeAcquirer(clean_pages)).revalidate(entry, INDEX)
    assert result.status is RevalidationStatus.STALE
    assert result.reason_ids == ("score_below_threshold",)


def test_budget_and_cross_origin_are_enforced_before_fetch(tmp_path: Path) -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    registry, entry = _registered(tmp_path, pages)
    acquirer = FakeAcquirer(pages)
    result = _service(registry, acquirer).revalidate(entry, "https://other.test/book")
    assert result.status is RevalidationStatus.INVALID
    assert acquirer.calls == []

    registry2, entry2 = _registered(tmp_path / "budget", pages)
    acquirer2 = FakeAcquirer(pages)
    result2 = ConfigRevalidator(acquirer=acquirer2, registry=registry2, max_revalidation_bytes=1).revalidate(entry2, INDEX)
    assert result2.status is RevalidationStatus.INVALID
    assert len(acquirer2.calls) == 1 and acquirer2.calls[0][1] == 1


def test_same_revalidator_concurrent_domains_keep_origin_and_budgets_per_run() -> None:
    first_pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    second_pages = {url.replace("example.test", "other.test"): html for url, html in first_pages.items()}
    first = _config(first_pages)
    second_payload = first.to_sensitive_dict()
    second_payload.update(config_id="cfg_qrstuvwxyzabcdef", domain="other.test")
    second = SiteConfig.from_dict(second_payload)
    entries = [_entry(first), _entry(second)]

    class ConcurrentRegistry(MemoryRegistry):
        def __init__(self) -> None:
            super().__init__(None)

        def load(self, entry: object) -> SiteConfig:
            return first if entry.config_id == first.config_id else second  # type: ignore[attr-defined]

    class BarrierAcquirer(FakeAcquirer):
        def __init__(self) -> None:
            super().__init__({**first_pages, **second_pages})
            self.barrier = threading.Barrier(2)

        def fetch_page(self, url: str, *, max_body_bytes: int | None = None, locked_origin: str | None = None) -> AcquiredPage:
            self.barrier.wait(timeout=5)
            return super().fetch_page(url, max_body_bytes=max_body_bytes, locked_origin=locked_origin)

    registry = ConcurrentRegistry()
    acquirer = BarrierAcquirer()
    service = _service(registry, acquirer)
    results: list[RevalidationResult] = []
    threads = [
        threading.Thread(target=lambda entry=entry, url=url: results.append(service.revalidate(entry, url)))
        for entry, url in zip(entries, (INDEX, INDEX.replace("example.test", "other.test")), strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert [result.status for result in results] == [RevalidationStatus.VALID, RevalidationStatus.VALID]
    assert registry.transitions.count(("valid", ())) == 2
    assert not any(kind == "invalid" for kind, _reasons in registry.transitions)
    assert {origin for _url, _budget, origin in acquirer.calls} == {"https://example.test", "https://other.test"}
    budgets_by_origin = {
        origin: [budget for _url, budget, item_origin in acquirer.calls if item_origin == origin]
        for origin in {"https://example.test", "https://other.test"}
    }
    assert all(values[0] == service.max_revalidation_bytes and values == sorted(values, reverse=True) for values in budgets_by_origin.values())


def test_result_is_immutable_and_safe() -> None:
    result = RevalidationResult(RevalidationStatus.STALE, ("fingerprint_mismatch",), {"content": 0.9}, {"chapter_second": False}, "2026-07-11T09:00:00Z")
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


def test_valid_transition_conflict_is_not_retried() -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    config = _config(pages)
    conflict_registry = MemoryRegistry(config, conflict="valid")
    conflict = _service(conflict_registry, FakeAcquirer(pages)).revalidate(_entry(config), INDEX)  # type: ignore[arg-type]
    assert conflict.status is RevalidationStatus.STALE and conflict.reason_ids == ("concurrent_revision",)


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


def test_result_repr_is_safe() -> None:
    result = RevalidationResult(RevalidationStatus.VALID, (), {}, {}, "2026-07-11T09:00:00Z")
    assert "selector" not in repr(result)


def test_fetch_catalog_and_anchor_guards_cover_every_bounded_boundary() -> None:
    pages = {INDEX: _index(), C1: _chapter(1), C2: _chapter(2)}
    config = _config(pages)
    service = _service(MemoryRegistry(config), FakeAcquirer(pages))
    context = _RevalidationRunContext(service._origin_key(INDEX))
    page = AcquiredPage(_snapshot(INDEX, pages[INDEX]), INDEX)
    with pytest.raises(ValueError):
        service._catalog_links(page, "", context)
    invalid_catalog = AcquiredPage(_snapshot(INDEX, "<nav><div href='/c1'></div><div href='/c2'></div></nav>"), INDEX)
    with pytest.raises(ValueError):
        service._catalog_links(invalid_catalog, "nav > *", context)
    cross_catalog = AcquiredPage(_snapshot(INDEX, "<a href='https://other.test/1'>1</a><a href='/c2'>2</a>"), INDEX)
    with pytest.raises(AcquisitionError):
        service._catalog_links(cross_catalog, "a", context)
    duplicate_catalog = AcquiredPage(_snapshot(INDEX, "<a href='/c1'>1</a><a href='/c1'>1</a>"), INDEX)
    with pytest.raises(ValueError):
        service._catalog_links(duplicate_catalog, "a", context)
    assert service._single_anchor(page, ".missing", context, required=False) is None
    with pytest.raises(ValueError):
        service._single_anchor(page, "h1", context, required=True)
    cross_anchor = AcquiredPage(_snapshot(INDEX, "<a href='https://other.test/x'>x</a>"), INDEX)
    with pytest.raises(AcquisitionError):
        service._single_anchor(cross_anchor, "a", context, required=True)

    context.page_count = 3
    with pytest.raises(ValueError):
        service._fetch(INDEX, context)
    context.page_count = 0
    with pytest.raises(AcquisitionError):
        service._fetch("https://other.test/x", context)
    context.byte_count = service.max_revalidation_bytes
    with pytest.raises(AcquisitionError):
        service._fetch(INDEX, context)

    class Redirecting(FakeAcquirer):
        def fetch_page(self, url: str, *, max_body_bytes: int | None = None, locked_origin: str | None = None) -> AcquiredPage:
            del url, max_body_bytes, locked_origin
            return AcquiredPage(_snapshot("https://other.test/x", "x"), "https://other.test/x")

    redirected = _service(MemoryRegistry(config), Redirecting(pages))
    with pytest.raises(AcquisitionError):
        redirected._fetch(INDEX, _RevalidationRunContext(redirected._origin_key(INDEX)))

    class Oversized(FakeAcquirer):
        def fetch_page(self, url: str, *, max_body_bytes: int | None = None, locked_origin: str | None = None) -> AcquiredPage:
            del max_body_bytes, locked_origin
            return AcquiredPage(_snapshot(url, "xx"), url)

    oversized = ConfigRevalidator(acquirer=Oversized(pages), registry=MemoryRegistry(config), max_revalidation_bytes=1)  # type: ignore[arg-type]
    with pytest.raises(AcquisitionError):
        oversized._fetch(INDEX, _RevalidationRunContext(oversized._origin_key(INDEX)))
