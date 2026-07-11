from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from novel_crawler.acquisition.classifier import PageClassifier
from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.acquisition.security import UrlSafetyPolicy
from novel_crawler.browser.coordinator import BrowserAcquirer, VerificationCoordinator, VerificationRequired
from novel_crawler.browser.driver import BrowserPageSnapshot
from novel_crawler.browser.models import VerificationStatus
from novel_crawler.browser.sessions import BrowserSessionStore, SessionLockTimeout

PUBLIC_POLICY = UrlSafetyPolicy(resolver=lambda host, port: ("93.184.216.34",))


def snapshot(html: str, url: str = "https://example.test/private?q=secret") -> PageSnapshot:
    return PageSnapshot(url, url, 200, {}, "utf-8", html, html.encode(), "GET", (), datetime.now(UTC))


class FakeHttp:
    def __init__(self, html: str) -> None:
        self.page = AcquiredPage(snapshot(html), "https://example.test/private?q=secret")
        self.calls: list[str] = []

    def fetch_page(self, url: str, **kwargs: Any) -> AcquiredPage:
        self.calls.append(url)
        return self.page


@dataclass
class FakeContext:
    snapshots: list[BrowserPageSnapshot]
    calls: list[object]

    def navigate(self, url: str) -> BrowserPageSnapshot:
        self.calls.append(("navigate", url))
        return self.snapshots.pop(0)

    def capture(self) -> BrowserPageSnapshot:
        self.calls.append("capture")
        return self.snapshots.pop(0)

    def close(self) -> None:
        self.calls.append("close")


class FakeDriver:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = contexts
        self.calls: list[object] = []

    def launch(self, *, user_data_dir: Path, headless: bool, policy: object) -> FakeContext:
        self.calls.append(("launch", user_data_dir, headless, policy))
        return self.contexts.pop(0)


def browser_snapshot(html: str, url: str = "https://example.test/private?q=secret") -> BrowserPageSnapshot:
    return BrowserPageSnapshot(url, url, 200, {"content-type": "text/html; charset=utf-8"}, html.encode())


def test_browser_acquirer_uses_http_for_classified_content_without_browser(tmp_path: Path) -> None:
    html = "<title>第1章</title><article id='content'>正文</article>"
    http = FakeHttp(html)
    driver = FakeDriver([])
    acquirer = BrowserAcquirer(http=http, classifier=PageClassifier(), driver=driver, sessions=BrowserSessionStore(tmp_path), safety_policy=PUBLIC_POLICY)
    assert acquirer.fetch_page("https://example.test/private?q=secret").snapshot.method == "GET"
    assert driver.calls == []


def test_browser_acquirer_falls_back_headless_for_unknown_and_uses_persistent_profile(tmp_path: Path) -> None:
    calls: list[object] = []
    context = FakeContext([browser_snapshot("<title>第1章</title><article id='content'>正文</article>")], calls)
    driver = FakeDriver([context])
    store = BrowserSessionStore(tmp_path)
    acquirer = BrowserAcquirer(http=FakeHttp("<div id='app'></div>"), driver=driver, sessions=store, safety_policy=PUBLIC_POLICY)
    page = acquirer.fetch_page("https://example.test/private?q=secret")
    assert page.snapshot.method == "browser"
    assert calls[-1] == "close"
    launch = driver.calls[0]
    assert launch[0] == "launch" and launch[2] is True
    with store.acquire("example.test") as lease:
        assert launch[1] == lease.profile_path


def test_auth_is_never_auto_bypassed_and_visible_begin_returns_safe_ticket(tmp_path: Path) -> None:
    visible = FakeContext([browser_snapshot("<title>Login</title><form><input type='password'></form>")], [])
    driver = FakeDriver([visible])
    coordinator = VerificationCoordinator(BrowserSessionStore(tmp_path), driver=driver, safety_policy=PUBLIC_POLICY)
    acquirer = BrowserAcquirer(
        http=FakeHttp("<title>Login</title><form><input type='password'></form>"),
        driver=driver,
        sessions=coordinator.sessions,
        coordinator=coordinator,
        safety_policy=PUBLIC_POLICY,
    )
    with pytest.raises(VerificationRequired) as caught:
        acquirer.fetch_page("https://example.test/private?q=secret")
    ticket = caught.value.ticket
    assert ticket is not None and ticket.status is VerificationStatus.WAITING
    assert ticket.safe_origin == "https://example.test/"
    assert "private" not in repr(ticket) and "secret" not in repr(ticket)
    assert driver.calls[0][2] is False
    coordinator.cancel(ticket.token)


def test_continue_waits_then_fails_after_two_attempts_and_releases_lease(tmp_path: Path) -> None:
    auth = browser_snapshot("<title>Login</title><form><input type='password'></form>")
    context = FakeContext([auth, auth, auth], [])
    driver = FakeDriver([context])
    sessions = BrowserSessionStore(tmp_path, lock_timeout=0.05)
    coordinator = VerificationCoordinator(sessions, driver=driver, max_attempts=2, safety_policy=PUBLIC_POLICY)
    ticket = coordinator.begin("https://example.test/private?q=secret")
    assert coordinator.continue_verification(ticket.token).status is VerificationStatus.WAITING
    assert coordinator.continue_verification(ticket.token).status is VerificationStatus.FAILED
    with sessions.acquire("example.test", timeout=0.1):
        pass
    assert context.calls[-1] == "close"


def test_verified_continue_reloads_original_in_same_context_and_returns_page(tmp_path: Path) -> None:
    auth = browser_snapshot("<title>Login</title><form><input type='password'></form>")
    chapter = browser_snapshot("<title>第1章</title><article id='content'>正文</article>")
    context = FakeContext([auth, chapter, chapter], [])
    coordinator = VerificationCoordinator(BrowserSessionStore(tmp_path), driver=FakeDriver([context]), safety_policy=PUBLIC_POLICY)
    ticket = coordinator.begin("https://example.test/private?q=secret")
    outcome = coordinator.continue_verification(ticket.token)
    assert outcome.status is VerificationStatus.COMPLETED
    assert outcome.page is not None and outcome.page.navigation_url.endswith("?q=secret")
    assert context.calls == [("navigate", "https://example.test/private?q=secret"), "capture", ("navigate", "https://example.test/private?q=secret"), "close"]


def test_cancel_timeout_capacity_and_safe_unknown_tokens(tmp_path: Path) -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    contexts = [FakeContext([browser_snapshot("<p>x</p>")], []) for _ in range(3)]
    coordinator = VerificationCoordinator(
        BrowserSessionStore(tmp_path), driver=FakeDriver(contexts), max_active=1, ttl=timedelta(minutes=10), clock=lambda: now,
        safety_policy=PUBLIC_POLICY,
    )
    first = coordinator.begin("https://one.example/a")
    with pytest.raises(VerificationRequired, match="verification_capacity"):
        coordinator.begin("https://two.example/a")
    assert coordinator.cancel(first.token).status is VerificationStatus.CANCELLED
    second = coordinator.begin("https://two.example/a")
    now += timedelta(minutes=11)
    assert coordinator.continue_verification(second.token).status is VerificationStatus.TIMED_OUT
    with pytest.raises(VerificationRequired, match="verification_token_invalid") as caught:
        coordinator.continue_verification("not-a-token")
    assert "not-a-token" not in str(caught.value)


def test_same_token_has_one_winner_and_same_domain_conflicts(tmp_path: Path) -> None:
    auth = browser_snapshot("<title>Login</title><form><input type='password'></form>")
    chapter = browser_snapshot("<title>第1章</title><article id='content'>正文</article>")
    context = FakeContext([auth, chapter, chapter], [])
    sessions = BrowserSessionStore(tmp_path, lock_timeout=0.05)
    coordinator = VerificationCoordinator(sessions, driver=FakeDriver([context]), safety_policy=PUBLIC_POLICY)
    ticket = coordinator.begin("https://example.test/a")
    with pytest.raises(SessionLockTimeout):
        sessions.acquire("example.test", timeout=0.01)
    results: list[object] = []
    barrier = threading.Barrier(3)

    def run() -> None:
        barrier.wait()
        try:
            results.append(coordinator.continue_verification(ticket.token))
        except Exception as exc:
            results.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sum(getattr(item, "status", None) is VerificationStatus.COMPLETED for item in results) == 1
    assert sum(isinstance(item, VerificationRequired) for item in results) == 1


def test_browser_crash_fails_safely_releases_lease_and_redacts_exception(tmp_path: Path) -> None:
    class Crashed(FakeContext):
        def capture(self) -> BrowserPageSnapshot:
            raise RuntimeError("cookie=secret C:/private/profile")

    context = Crashed([browser_snapshot("<p>x</p>")], [])
    sessions = BrowserSessionStore(tmp_path)
    coordinator = VerificationCoordinator(sessions, driver=FakeDriver([context]), safety_policy=PUBLIC_POLICY)
    ticket = coordinator.begin("https://example.test/private?q=secret")
    result = coordinator.continue_verification(ticket.token)
    assert result.status is VerificationStatus.FAILED
    assert "secret" not in repr(result)
    with sessions.acquire("example.test", timeout=0.1):
        pass


def test_public_models_reject_invalid_limits_urls_and_error_codes(tmp_path: Path) -> None:
    sessions = BrowserSessionStore(tmp_path)
    for kwargs in ({"ttl": timedelta(0)}, {"max_active": 0}, {"max_attempts": 0}):
        with pytest.raises(ValueError, match="limits"):
            VerificationCoordinator(sessions, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="error code"):
        VerificationRequired("cookie=secret")
    coordinator = VerificationCoordinator(sessions, driver=FakeDriver([]), safety_policy=PUBLIC_POLICY)
    with pytest.raises(VerificationRequired, match="verification_url_invalid"):
        coordinator.begin("https:///missing-host")
    with pytest.raises(VerificationRequired, match="verification_token_invalid"):
        coordinator.continue_verification("x" * 129)


def test_begin_navigation_crash_closes_partial_context_and_releases_lease(tmp_path: Path) -> None:
    class NavigateCrash(FakeContext):
        def navigate(self, url: str) -> BrowserPageSnapshot:
            raise RuntimeError("cookie=secret C:/private/profile")

    context = NavigateCrash([], [])
    sessions = BrowserSessionStore(tmp_path)
    coordinator = VerificationCoordinator(sessions, driver=FakeDriver([context]), safety_policy=PUBLIC_POLICY)
    with pytest.raises(VerificationRequired, match="verification_start_failed") as caught:
        coordinator.begin("https://example.test/private?q=secret")
    assert "secret" not in str(caught.value)
    assert context.calls[-1] == "close"
    with sessions.acquire("example.test", timeout=0.1):
        pass


def test_reload_that_returns_auth_waits_then_fails_on_second_attempt(tmp_path: Path) -> None:
    auth = browser_snapshot("<title>Login</title><form><input type='password'></form>")
    chapter = browser_snapshot("<title>Chapter 1</title><article id='content'>body</article>")
    context = FakeContext([auth, chapter, auth, chapter, auth], [])
    coordinator = VerificationCoordinator(
        BrowserSessionStore(tmp_path), driver=FakeDriver([context]), safety_policy=PUBLIC_POLICY
    )
    ticket = coordinator.begin("https://example.test/a")
    assert coordinator.continue_verification(ticket.token).status is VerificationStatus.WAITING
    assert coordinator.continue_verification(ticket.token).status is VerificationStatus.FAILED


def test_expire_sweep_closes_expired_entries(tmp_path: Path) -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    context = FakeContext([browser_snapshot("<p>x</p>")], [])
    coordinator = VerificationCoordinator(
        BrowserSessionStore(tmp_path),
        driver=FakeDriver([context]),
        clock=lambda: now,
        safety_policy=PUBLIC_POLICY,
    )
    ticket = coordinator.begin("https://example.test/a")
    now += timedelta(minutes=11)
    assert coordinator.expire_sweep() == 1
    assert context.calls[-1] == "close"
    with pytest.raises(VerificationRequired, match="verification_token_invalid"):
        coordinator.cancel(ticket.token)


def test_headless_auth_closes_fallback_before_requesting_manual_and_fetch_returns_snapshot(tmp_path: Path) -> None:
    auth = browser_snapshot("<title>Login</title><form><input type='password'></form>")
    context = FakeContext([auth], [])
    acquirer = BrowserAcquirer(
        http=FakeHttp("<div id='app'></div>"),
        driver=FakeDriver([context]),
        sessions=BrowserSessionStore(tmp_path),
        safety_policy=PUBLIC_POLICY,
    )
    with pytest.raises(VerificationRequired) as caught:
        acquirer.fetch("https://example.test/a")
    assert caught.value.ticket is None
    assert context.calls[-1] == "close"


def test_headless_browser_failure_is_safe_and_releases_profile(tmp_path: Path) -> None:
    class LaunchCrash(FakeDriver):
        def launch(self, *, user_data_dir: Path, headless: bool, policy: object) -> FakeContext:
            raise RuntimeError(f"cookie=secret path={user_data_dir}")

    sessions = BrowserSessionStore(tmp_path)
    acquirer = BrowserAcquirer(
        http=FakeHttp("<div id='app'></div>"),
        driver=LaunchCrash([]),
        sessions=sessions,
        safety_policy=PUBLIC_POLICY,
    )
    with pytest.raises(Exception, match="browser_failed") as caught:
        acquirer.fetch_page("https://example.test/private?q=secret")
    assert "secret" not in str(caught.value)
    with sessions.acquire("example.test", timeout=0.1):
        pass
