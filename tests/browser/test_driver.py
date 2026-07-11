from __future__ import annotations

import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from novel_crawler.acquisition.security import UrlSafetyPolicy
from novel_crawler.browser.driver import (
    BrowserContextWorker,
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


def test_request_policy_blocks_public_cdn_assets_and_non_http_documents() -> None:
    guard = BrowserRequestPolicy(public_policy())
    guard.lock("https://example.test/start")
    assert guard.decide("https://cdn.test/app.js", resource_type="script", is_navigation=False) is RequestDecision.BLOCK
    assert guard.decide("https://cdn.test/page", resource_type="document", is_navigation=True) is RequestDecision.BLOCK
    assert guard.decide("data:image/png;base64,AA==", resource_type="image", is_navigation=False) is RequestDecision.BLOCK
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

    def evaluate(self, expression: str) -> int:
        assert "outerHTML.length" in expression
        return len(self.content_value)


class FakeRawContext:
    def __init__(self, *, fail_route: bool = False) -> None:
        self.pages = [FakePage()]
        self.route_callback: object | None = None
        self.closed = False
        self.fail_route = fail_route
        self.web_socket_callback: object | None = None
        self.init_scripts: list[str] = []

    def route(self, pattern: str, callback: object) -> None:
        assert pattern == "**/*"
        if self.fail_route:
            raise RuntimeError("route crashed")
        self.route_callback = callback

    def close(self) -> None:
        self.closed = True

    def route_web_socket(self, pattern: str, callback: object) -> None:
        assert pattern == "**/*"
        self.web_socket_callback = callback

    def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)


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
    assert callable(raw.web_socket_callback)
    web_socket = SimpleNamespace(closed=False, close=lambda **kwargs: setattr(web_socket, "closed", kwargs))
    raw.web_socket_callback(web_socket)  # type: ignore[operator]
    assert web_socket.closed


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


def test_default_driver_uses_pinned_proxy_and_locked_down_chromium_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = FakeRawContext()
    launch_options: dict[str, object] = {}

    def launch(**kwargs: object) -> FakeRawContext:
        launch_options.update(kwargs)
        return raw

    runtime = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch),
        stop=lambda: None,
    )
    manager = SimpleNamespace(start=lambda: runtime)
    package = ModuleType("playwright")
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: manager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    class FakeProxy:
        proxy_url = "socks5://127.0.0.1:4321"

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("novel_crawler.browser.driver.PinnedSocksProxy", lambda *args, **kwargs: FakeProxy())
    guard = BrowserRequestPolicy(public_policy())
    guard.lock("https://example.test/start")
    context = DefaultPlaywrightDriver().launch(user_data_dir=tmp_path, headless=True, policy=guard)
    assert launch_options["proxy"] == {"server": "socks5://127.0.0.1:4321", "bypass": ""}
    assert launch_options["service_workers"] == "block"
    assert launch_options["accept_downloads"] is False
    args = launch_options["args"]
    assert "--renderer-process-limit=4" in args
    assert any("max-old-space-size" in value for value in args)
    assert raw.init_scripts and "unregister" in raw.init_scripts[0]
    context.close()


def test_browser_worker_owns_every_context_call_on_one_dedicated_thread(tmp_path: Path) -> None:
    thread_ids: list[int] = []

    class OwnedContext:
        def navigate(self, url: str) -> BrowserPageSnapshot:
            thread_ids.append(threading.get_ident())
            return BrowserPageSnapshot(url, url, 200, {}, b"<p>ok</p>")

        def capture(self) -> BrowserPageSnapshot:
            thread_ids.append(threading.get_ident())
            return BrowserPageSnapshot("https://example.test/", "https://example.test/", 200, {}, b"<p>ok</p>")

        def close(self) -> None:
            thread_ids.append(threading.get_ident())

    class OwnedDriver:
        def launch(self, *, user_data_dir: Path, headless: bool, policy: BrowserRequestPolicy) -> OwnedContext:
            thread_ids.append(threading.get_ident())
            return OwnedContext()

    guard = BrowserRequestPolicy(public_policy())
    guard.lock("https://example.test/")
    worker = BrowserContextWorker(OwnedDriver(), user_data_dir=tmp_path, headless=True, policy=guard, ttl=1)
    worker.start()
    worker.navigate("https://example.test/")
    worker.capture()
    worker.close()
    assert len(set(thread_ids)) == 1
    assert thread_ids[0] != threading.get_ident()


def test_browser_worker_monotonic_deadline_closes_without_coordinator_calls(tmp_path: Path) -> None:
    closed = threading.Event()

    class Context:
        def navigate(self, url: str) -> BrowserPageSnapshot:
            raise AssertionError

        def capture(self) -> BrowserPageSnapshot:
            raise AssertionError

        def close(self) -> None:
            closed.set()

    class DeadlineDriver:
        def launch(self, *, user_data_dir: Path, headless: bool, policy: BrowserRequestPolicy) -> Context:
            return Context()

    guard = BrowserRequestPolicy(public_policy())
    guard.lock("https://example.test/")
    worker = BrowserContextWorker(DeadlineDriver(), user_data_dir=tmp_path, headless=False, policy=guard, ttl=0.1)
    worker.start()
    assert closed.wait(1)
    with pytest.raises(RuntimeError, match="browser_worker_expired"):
        worker.capture()


def test_capture_rejects_dom_before_materializing_content() -> None:
    raw = FakeRawContext()
    raw.pages[0].content_value = "x" * 20
    content_calls = 0

    def content() -> str:
        nonlocal content_calls
        content_calls += 1
        return raw.pages[0].content_value

    raw.pages[0].content = content  # type: ignore[method-assign]
    guard = BrowserRequestPolicy(public_policy())
    guard.lock("https://example.test/")
    context = _PlaywrightContext(SimpleNamespace(stop=lambda: None), raw, guard, max_body_bytes=10)
    raw.pages[0].url = "https://example.test/"
    with pytest.raises(ValueError, match="browser_body_too_large"):
        context.capture()
    assert content_calls == 0


def test_context_requires_websocket_routing_and_worker_close_can_retry(tmp_path: Path) -> None:
    raw = FakeRawContext()
    raw.route_web_socket = None  # type: ignore[method-assign]
    guard = BrowserRequestPolicy(public_policy())
    guard.lock("https://example.test/")
    with pytest.raises(RuntimeError, match="websocket_routing_required"):
        _PlaywrightContext(SimpleNamespace(stop=lambda: None), raw, guard)

    class CloseRetry:
        failures = 1

        def navigate(self, url: str) -> BrowserPageSnapshot:
            raise AssertionError

        def capture(self) -> BrowserPageSnapshot:
            raise AssertionError

        def close(self) -> None:
            if self.failures:
                self.failures -= 1
                raise RuntimeError("close failed")

    context = CloseRetry()

    class Driver:
        def launch(self, *, user_data_dir: Path, headless: bool, policy: BrowserRequestPolicy) -> CloseRetry:
            return context

    worker = BrowserContextWorker(Driver(), user_data_dir=tmp_path, headless=True, policy=guard, ttl=1)
    worker.start()
    with pytest.raises(RuntimeError, match="close failed"):
        worker.close()
    worker.close()


def test_worker_and_driver_limit_validation_and_launch_failure(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="browser limits"):
        DefaultPlaywrightDriver(max_network_bytes=0)
    guard = BrowserRequestPolicy(public_policy())
    guard.lock("https://example.test/")
    with pytest.raises(ValueError, match="worker ttl"):
        BrowserContextWorker(SimpleNamespace(), user_data_dir=tmp_path, headless=True, policy=guard, ttl=0)  # type: ignore[arg-type]

    class Broken:
        def launch(self, *, user_data_dir: Path, headless: bool, policy: BrowserRequestPolicy) -> object:
            raise RuntimeError("launch failed")

    worker = BrowserContextWorker(Broken(), user_data_dir=tmp_path, headless=True, policy=guard, ttl=1)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="launch failed"):
        worker.start()
    with pytest.raises(RuntimeError, match="browser_worker_closed"):
        worker.capture()


def test_policy_requires_lock_and_validation_for_private_properties() -> None:
    guard = BrowserRequestPolicy(public_policy())
    with pytest.raises(RuntimeError, match="origin_not_locked"):
        _ = guard.locked_url
    with pytest.raises(RuntimeError, match="origin_not_validated"):
        _ = guard.resolved_target


def test_policy_fails_closed_for_malformed_request_url() -> None:
    guard = BrowserRequestPolicy(public_policy())
    guard.lock("https://example.test/")
    assert guard.decide("https://[malformed", resource_type="script", is_navigation=False) is RequestDecision.BLOCK


def test_browser_snapshot_falls_back_when_declared_charset_is_invalid() -> None:
    raw = BrowserPageSnapshot(
        "https://example.test/",
        "https://example.test/",
        200,
        {"content-type": "text/html; charset=does-not-exist"},
        b"plain ascii",
    )
    snapshot = raw.to_page_snapshot()
    assert snapshot.html == "plain ascii"
