from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from novel_crawler.acquisition.security import UrlSafetyPolicy
from novel_crawler.browser.driver import (
    BrowserPageSnapshot,
    BrowserRequestPolicy,
    DefaultPlaywrightDriver,
    RequestDecision,
    _PlaywrightContext,
)


def public_policy() -> UrlSafetyPolicy:
    return UrlSafetyPolicy(resolver=lambda host, port: ("93.184.216.34",))


def test_browser_snapshot_converts_to_safe_bounded_page_snapshot() -> None:
    raw = BrowserPageSnapshot(
        requested_url="https://example.test/private?q=secret",
        final_url="https://example.test/chapter?token=secret",
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8", "Set-Cookie": "secret"},
        body="正文".encode(),
        retrieved_at=datetime.now(UTC),
    )
    converted = raw.to_page_snapshot(max_body_bytes=32)
    assert converted.method == "browser"
    acquired = raw.to_acquired_page(max_body_bytes=32)
    assert acquired.navigation_url.endswith("/chapter?token=secret")
    assert acquired.snapshot.requested_url == "https://example.test/"
    assert acquired.snapshot.final_url == "https://example.test/"
    assert acquired.snapshot.method == "browser"
    assert acquired.snapshot.html == "正文"
    assert acquired.snapshot.body == "正文".encode()
    assert acquired.snapshot.headers == {"content-type": "text/html; charset=utf-8"}
    assert acquired.snapshot.sample_id.startswith("sample-")
    assert "secret" not in repr(acquired)


def test_browser_snapshot_rejects_body_over_hard_or_requested_limit() -> None:
    raw = BrowserPageSnapshot("https://example.test/a", "https://example.test/a", 200, {}, b"1234")
    with pytest.raises(ValueError, match="browser_body_too_large"):
        raw.to_acquired_page(max_body_bytes=3)
    with pytest.raises(ValueError, match="max_body_bytes"):
        raw.to_acquired_page(max_body_bytes=0)


def test_raw_browser_snapshot_repr_never_exposes_urls_body_or_headers() -> None:
    raw = BrowserPageSnapshot(
        "https://example.test/private?token=secret",
        "https://example.test/private?token=secret",
        200,
        {"set-cookie": "session=secret"},
        b"private body",
    )
    rendered = repr(raw)
    assert "secret" not in rendered
    assert "private" not in rendered
    assert "set-cookie" not in rendered


def test_request_policy_blocks_private_cross_origin_documents_and_allows_same_origin_assets() -> None:
    guard = BrowserRequestPolicy(public_policy())
    guard.lock("https://example.test/start")
    assert guard.decide("https://example.test/app.js", resource_type="script", is_navigation=False) is RequestDecision.ALLOW
    assert guard.decide("https://cdn.test/app.js", resource_type="script", is_navigation=False) is RequestDecision.BLOCK
    assert guard.decide("https://other.test/page", resource_type="document", is_navigation=True) is RequestDecision.BLOCK
    private = BrowserRequestPolicy(UrlSafetyPolicy(resolver=lambda host, port: ("127.0.0.1",)))
    private.lock("https://example.test/start", validate=False)
    assert private.decide("https://example.test/a.js", resource_type="script", is_navigation=False) is RequestDecision.BLOCK


def test_request_policy_can_allow_validated_public_cdn_assets_but_never_documents() -> None:
    guard = BrowserRequestPolicy(public_policy(), allow_public_cdn_subresources=True)
    guard.lock("https://example.test/start")
    assert guard.decide("https://cdn.test/app.js", resource_type="script", is_navigation=False) is RequestDecision.ALLOW
    assert guard.decide("https://cdn.test/page", resource_type="document", is_navigation=True) is RequestDecision.BLOCK
    assert guard.decide("data:image/png;base64,AA==", resource_type="image", is_navigation=False) is RequestDecision.ALLOW
    assert guard.decide("data:text/html,private", resource_type="document", is_navigation=True) is RequestDecision.BLOCK
    assert guard.decide("file:///etc/passwd", resource_type="other", is_navigation=False) is RequestDecision.BLOCK


def test_request_policy_requires_validated_locked_origin_before_decisions() -> None:
    guard = BrowserRequestPolicy(public_policy())
    with pytest.raises(RuntimeError, match="origin_not_locked"):
        guard.decide("https://example.test/app.js", resource_type="script", is_navigation=False)


class FakePage:
    def __init__(self) -> None:
        self.url = "https://example.test/final"
        self.content_value = "<p>ok</p>"
        self.goto_calls: list[tuple[str, str]] = []

    def goto(self, url: str, *, wait_until: str) -> object:
        self.goto_calls.append((url, wait_until))
        self.url = url
        return SimpleNamespace(status=201, headers={"content-type": "text/html"})

    def content(self) -> str:
        return self.content_value


class FakeRawContext:
    def __init__(self, *, fail_route: bool = False) -> None:
        self.pages = [FakePage()]
        self.route_callback: object | None = None
        self.closed = False
        self.fail_route = fail_route

    def route(self, pattern: str, callback: object) -> None:
        assert pattern == "**/*"
        if self.fail_route:
            raise RuntimeError("route crashed")
        self.route_callback = callback

    def close(self) -> None:
        self.closed = True


def test_playwright_context_routes_every_request_and_captures_navigation() -> None:
    raw = FakeRawContext()
    runtime = SimpleNamespace(stopped=False, stop=lambda: setattr(runtime, "stopped", True))
    guard = BrowserRequestPolicy(public_policy())
    guard.lock("https://example.test/start")
    context = _PlaywrightContext(runtime, raw, guard)
    allowed = SimpleNamespace(
        request=SimpleNamespace(
            url="https://example.test/app.js", resource_type="script", is_navigation_request=lambda: False
        ),
        continued=False,
        aborted=False,
        continue_=lambda: setattr(allowed, "continued", True),
        abort=lambda code: setattr(allowed, "aborted", code),
    )
    blocked = SimpleNamespace(
        request=SimpleNamespace(
            url="https://other.test/page", resource_type="document", is_navigation_request=lambda: True
        ),
        continued=False,
        aborted=False,
        continue_=lambda: setattr(blocked, "continued", True),
        abort=lambda code: setattr(blocked, "aborted", code),
    )
    assert callable(raw.route_callback)
    raw.route_callback(allowed)  # type: ignore[operator]
    raw.route_callback(blocked)  # type: ignore[operator]
    page = context.navigate("https://example.test/start")
    assert allowed.continued and blocked.aborted == "blockedbyclient"
    assert page.status_code == 201 and page.final_url == "https://example.test/start"
    assert raw.pages[0].goto_calls == [("https://example.test/start", "domcontentloaded")]
    context.close()
    assert raw.closed and runtime.stopped


def test_playwright_capture_rejects_unexpected_cross_origin_final_page() -> None:
    raw = FakeRawContext()
    runtime = SimpleNamespace(stop=lambda: None)
    guard = BrowserRequestPolicy(public_policy())
    guard.lock("https://example.test/start")
    context = _PlaywrightContext(runtime, raw, guard)
    raw.pages[0].url = "https://other.test/private?secret=x"
    with pytest.raises(RuntimeError, match="browser_navigation_blocked") as caught:
        context.capture()
    assert "private" not in str(caught.value) and "secret" not in str(caught.value)


def test_default_driver_closes_partial_context_when_route_setup_crashes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    raw = FakeRawContext(fail_route=True)
    runtime = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=lambda **kwargs: raw),
        stopped=False,
        stop=lambda: setattr(runtime, "stopped", True),
    )
    manager = SimpleNamespace(start=lambda: runtime)
    package = ModuleType("playwright")
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: manager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    guard = BrowserRequestPolicy(public_policy())
    guard.lock("https://example.test/start")
    with pytest.raises(RuntimeError, match="route crashed"):
        DefaultPlaywrightDriver().launch(user_data_dir=tmp_path, headless=True, policy=guard)
    assert raw.closed
    assert runtime.stopped
