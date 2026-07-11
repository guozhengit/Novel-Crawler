from __future__ import annotations

import multiprocessing
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from novel_crawler.acquisition.classifier import PageClassifier
from novel_crawler.acquisition.http import AcquisitionError
from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.acquisition.security import UrlSafetyPolicy
from novel_crawler.browser.coordinator import (
    BrowserAcquirer,
    BrowserCleanupRequired,
    VerificationCoordinator,
    VerificationRequired,
    _AttemptLedger,
)
from novel_crawler.browser.driver import BrowserPageSnapshot, DriverLaunchFailure
from novel_crawler.browser.models import VerificationStatus
from novel_crawler.browser.sessions import BrowserSessionLease, BrowserSessionStore, SessionLockTimeout

PUBLIC_POLICY = UrlSafetyPolicy(resolver=lambda host, port: ("93.184.216.34",))


def _reserve_ledger_in_process(root: str, now_iso: str, output: object) -> None:
    now = datetime.fromisoformat(now_iso)
    sessions = BrowserSessionStore(root)
    ledger = _AttemptLedger(sessions, lambda: now, timedelta(minutes=5))
    key = ledger.opaque_key("https://example.test/", "download")
    try:
        reservation = ledger.reserve(key, 2)
    except Exception as exc:
        output.put((False, type(exc).__name__))  # type: ignore[attr-defined]
    else:
        output.put((True, reservation))  # type: ignore[attr-defined]


def snapshot(html: str, url: str = "https://example.test/private?q=secret") -> PageSnapshot:
    return PageSnapshot(url, url, 200, {}, "utf-8", html, html.encode(), "GET", (), datetime.now(UTC))


class FakeHttp:
    def __init__(self, html: str) -> None:
        self.page = AcquiredPage(snapshot(html), "https://example.test/private?q=secret")
        self.calls: list[str] = []
        self.options: list[dict[str, Any]] = []

    def fetch_page(self, url: str, **kwargs: Any) -> AcquiredPage:
        self.calls.append(url)
        self.options.append(kwargs)
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
    ticket = coordinator.begin("https://example.test/private?q=secret", task_key="download")
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
    ticket = coordinator.begin("https://example.test/private?q=secret", task_key="download")
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
    first = coordinator.begin("https://one.example/a", task_key="one")
    with pytest.raises(VerificationRequired, match="verification_capacity"):
        coordinator.begin("https://two.example/a", task_key="two")
    assert coordinator.cancel(first.token).status is VerificationStatus.CANCELLED
    second = coordinator.begin("https://two.example/a", task_key="two")
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
    ticket = coordinator.begin("https://example.test/a", task_key="download")
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
    ticket = coordinator.begin("https://example.test/private?q=secret", task_key="download")
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
        coordinator.begin("https:///missing-host", task_key="download")
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
        coordinator.begin("https://example.test/private?q=secret", task_key="download")
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
    ticket = coordinator.begin("https://example.test/a", task_key="download")
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
    ticket = coordinator.begin("https://example.test/a", task_key="download")
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


def test_begin_requires_safe_task_key_and_persistent_attempts_cannot_reset(tmp_path: Path) -> None:
    contexts = [FakeContext([browser_snapshot("<p>x</p>")], []) for _ in range(2)]
    sessions = BrowserSessionStore(tmp_path)
    coordinator = VerificationCoordinator(sessions, driver=FakeDriver(contexts), safety_policy=PUBLIC_POLICY)
    with pytest.raises(TypeError):
        coordinator.begin("https://example.test/a")  # type: ignore[call-arg]
    with pytest.raises(VerificationRequired, match="verification_task_invalid"):
        coordinator.begin("https://example.test/a", task_key="cookie=secret")
    first = coordinator.begin("https://example.test/a", task_key="download")
    coordinator.cancel(first.token)
    second = coordinator.begin("https://example.test/a", task_key="download")
    coordinator.cancel(second.token)
    restarted = VerificationCoordinator(sessions, driver=FakeDriver([]), safety_policy=PUBLIC_POLICY)
    with pytest.raises(VerificationRequired, match="verification_attempts_exhausted"):
        restarted.begin("https://example.test/a", task_key="download")


def test_launch_failure_rolls_back_capacity_and_attempt_reservation(tmp_path: Path) -> None:
    class FailOnceDriver(FakeDriver):
        def launch(self, *, user_data_dir: Path, headless: bool, policy: object) -> FakeContext:
            if not self.contexts:
                raise RuntimeError("launch failure")
            return super().launch(user_data_dir=user_data_dir, headless=headless, policy=policy)

    driver = FailOnceDriver([])
    coordinator = VerificationCoordinator(
        BrowserSessionStore(tmp_path), driver=driver, max_active=1, safety_policy=PUBLIC_POLICY
    )
    with pytest.raises(VerificationRequired, match="verification_start_failed"):
        coordinator.begin("https://example.test/a", task_key="download")
    driver.contexts.append(FakeContext([browser_snapshot("<p>x</p>")], []))
    ticket = coordinator.begin("https://example.test/a", task_key="download")
    coordinator.cancel(ticket.token)


def test_failed_close_keeps_stale_lease_and_capacity_until_explicit_retry(tmp_path: Path) -> None:
    class CloseFailsOnce(FakeContext):
        def __init__(self) -> None:
            super().__init__([browser_snapshot("<p>x</p>")], [])
            self.failures = 1

        def close(self) -> None:
            self.calls.append("close")
            if self.failures:
                self.failures -= 1
                raise RuntimeError("cookie=secret path=C:/private")

    context = CloseFailsOnce()
    sessions = BrowserSessionStore(tmp_path, lock_timeout=0.05)
    coordinator = VerificationCoordinator(
        sessions, driver=FakeDriver([context]), max_active=1, safety_policy=PUBLIC_POLICY
    )
    ticket = coordinator.begin("https://example.test/a", task_key="download")
    outcome = coordinator.cancel(ticket.token)
    assert outcome.status is VerificationStatus.FAILED
    with pytest.raises(VerificationRequired, match="verification_capacity"):
        coordinator.begin("https://other.test/a", task_key="other")
    with pytest.raises(SessionLockTimeout):
        sessions.acquire("example.test", timeout=0.01)
    assert coordinator.retry_cleanup(ticket.token)
    with sessions.acquire("example.test", timeout=0.1):
        pass


def test_browser_acquirer_passes_limits_and_classifiable_statuses_to_http(tmp_path: Path) -> None:
    http = FakeHttp("<title>Login</title><form><input type='password'></form>")
    acquirer = BrowserAcquirer(
        http=http,
        driver=FakeDriver([]),
        sessions=BrowserSessionStore(tmp_path),
        safety_policy=PUBLIC_POLICY,
        max_body_bytes=123,
    )
    with pytest.raises(VerificationRequired):
        acquirer.fetch_page("https://example.test/a")
    assert http.calls == ["https://example.test/a"]
    assert http.options == [{"max_body_bytes": 123, "classifiable_statuses": frozenset({401, 403, 429, 503})}]


def test_success_clears_attempt_ledger_for_future_runs(tmp_path: Path) -> None:
    chapter = browser_snapshot("<title>Chapter 1</title><article id='content'>body</article>")
    contexts = [
        FakeContext([chapter, chapter, chapter], []),
        FakeContext([chapter], []),
        FakeContext([chapter], []),
    ]
    sessions = BrowserSessionStore(tmp_path)
    coordinator = VerificationCoordinator(sessions, driver=FakeDriver(contexts), safety_policy=PUBLIC_POLICY)
    first = coordinator.begin("https://example.test/a", task_key="download")
    assert coordinator.continue_verification(first.token).status is VerificationStatus.COMPLETED
    second = coordinator.begin("https://example.test/a", task_key="download")
    coordinator.cancel(second.token)
    third = coordinator.begin("https://example.test/a", task_key="download")
    coordinator.cancel(third.token)


def test_confirmed_browser_crash_marks_stale_and_releases_lease(tmp_path: Path) -> None:
    class DeadContext(FakeContext):
        def capture(self) -> BrowserPageSnapshot:
            raise RuntimeError("browser crashed cookie=secret")

        def is_alive(self) -> bool:
            return False

    sessions = BrowserSessionStore(tmp_path)
    coordinator = VerificationCoordinator(
        sessions,
        driver=FakeDriver([DeadContext([browser_snapshot("<p>x</p>")], [])]),
        safety_policy=PUBLIC_POLICY,
    )
    ticket = coordinator.begin("https://example.test/a", task_key="download")
    assert coordinator.continue_verification(ticket.token).status is VerificationStatus.FAILED
    with sessions.acquire("example.test", timeout=0.1):
        pass


def test_retry_cleanup_can_report_repeated_close_failure(tmp_path: Path) -> None:
    class NeverCloses(FakeContext):
        def close(self) -> None:
            raise RuntimeError("close failed")

    coordinator = VerificationCoordinator(
        BrowserSessionStore(tmp_path),
        driver=FakeDriver([NeverCloses([browser_snapshot("<p>x</p>")], [])]),
        safety_policy=PUBLIC_POLICY,
    )
    ticket = coordinator.begin("https://example.test/a", task_key="download")
    assert coordinator.cancel(ticket.token).status is VerificationStatus.FAILED
    assert coordinator.retry_cleanup(ticket.token) is False


def test_begin_failure_with_failed_close_is_quarantined_before_lease_release(tmp_path: Path) -> None:
    class NavigateAndCloseFail(FakeContext):
        def __init__(self) -> None:
            super().__init__([], [])
            self.close_failures = 1

        def navigate(self, url: str) -> BrowserPageSnapshot:
            raise RuntimeError("navigation failed")

        def close(self) -> None:
            if self.close_failures:
                self.close_failures -= 1
                raise RuntimeError("close failed")

    sessions = BrowserSessionStore(tmp_path, lock_timeout=0.05)
    coordinator = VerificationCoordinator(
        sessions,
        driver=FakeDriver([NavigateAndCloseFail()]),
        max_active=1,
        safety_policy=PUBLIC_POLICY,
    )
    with pytest.raises(VerificationRequired, match="verification_start_failed") as caught:
        coordinator.begin("https://example.test/a", task_key="download")
    cleanup_token = caught.value.ticket.token if caught.value.ticket is not None else ""
    assert cleanup_token
    with pytest.raises(SessionLockTimeout):
        sessions.acquire("example.test", timeout=0.01)
    assert coordinator.retry_cleanup(cleanup_token)


def test_attempt_ledger_is_atomic_across_coordinator_instances(tmp_path: Path) -> None:
    sessions = BrowserSessionStore(tmp_path)
    first = VerificationCoordinator(
        sessions,
        driver=FakeDriver([FakeContext([browser_snapshot("<p>x</p>")], [])]),
        safety_policy=PUBLIC_POLICY,
    )
    second = VerificationCoordinator(
        sessions,
        driver=FakeDriver([FakeContext([browser_snapshot("<p>x</p>")], [])]),
        safety_policy=PUBLIC_POLICY,
    )
    one = first.begin("https://example.test/a", task_key="download")
    first.cancel(one.token)
    two = second.begin("https://example.test/a", task_key="download")
    second.cancel(two.token)
    third = VerificationCoordinator(sessions, driver=FakeDriver([]), safety_policy=PUBLIC_POLICY)
    with pytest.raises(VerificationRequired, match="verification_attempts_exhausted"):
        third.begin("https://example.test/a", task_key="download")


def test_worker_deadline_callback_releases_confirmed_closed_context_without_sweep(tmp_path: Path) -> None:
    context = FakeContext([browser_snapshot("<p>x</p>")], [])
    sessions = BrowserSessionStore(tmp_path, lock_timeout=0.05)
    coordinator = VerificationCoordinator(
        sessions,
        driver=FakeDriver([context]),
        ttl=timedelta(seconds=0.1),
        safety_policy=PUBLIC_POLICY,
    )
    ticket = coordinator.begin("https://example.test/a", task_key="download")
    time.sleep(0.25)
    with sessions.acquire("example.test", timeout=0.1):
        pass
    with pytest.raises(VerificationRequired, match="verification_token_invalid"):
        coordinator.continue_verification(ticket.token)


def test_worker_deadline_failed_close_keeps_capacity_until_retry(tmp_path: Path) -> None:
    class DeadlineCloseFails(FakeContext):
        def __init__(self) -> None:
            super().__init__([browser_snapshot("<p>x</p>")], [])
            self.failures = 1

        def close(self) -> None:
            if self.failures:
                self.failures -= 1
                raise RuntimeError("close failed")

    sessions = BrowserSessionStore(tmp_path, lock_timeout=0.05)
    coordinator = VerificationCoordinator(
        sessions,
        driver=FakeDriver([DeadlineCloseFails()]),
        ttl=timedelta(seconds=0.1),
        max_active=1,
        safety_policy=PUBLIC_POLICY,
    )
    ticket = coordinator.begin("https://example.test/a", task_key="download")
    time.sleep(0.25)
    with pytest.raises(VerificationRequired, match="verification_capacity"):
        coordinator.begin("https://other.test/a", task_key="other")
    assert coordinator.retry_cleanup(ticket.token)


def test_ledger_success_removes_only_its_reservation_and_purges_expired_records(tmp_path: Path) -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    sessions = BrowserSessionStore(tmp_path)
    first = _AttemptLedger(sessions, lambda: now, timedelta(minutes=1), max_keys=2, max_records=2)
    second = _AttemptLedger(sessions, lambda: now, timedelta(minutes=1), max_keys=2, max_records=2)
    key = first.opaque_key("https://example.test/", "download")
    reservation_a = first.reserve(key, 2)
    reservation_b = second.reserve(key, 2)
    first.finish(key, reservation_a, consumed=False)
    reservation_c = first.reserve(key, 2)
    with pytest.raises(VerificationRequired, match="verification_attempts_exhausted"):
        second.reserve(key, 2)
    first.finish(key, reservation_b, consumed=True)
    first.finish(key, reservation_c, consumed=True)

    now += timedelta(minutes=2)
    other = first.opaque_key("https://other.test/", "download")
    assert first.reserve(other, 2)


def test_lease_close_failure_keeps_coordinator_capacity_until_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = BrowserSessionStore(tmp_path)
    coordinator = VerificationCoordinator(
        sessions,
        driver=FakeDriver([FakeContext([browser_snapshot("<p>x</p>")], [])]),
        max_active=1,
        safety_policy=PUBLIC_POLICY,
    )
    ticket = coordinator.begin("https://example.test/a", task_key="download")
    original = BrowserSessionLease.close
    failed = False

    def fail_once(lease: BrowserSessionLease) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("release failed")
        original(lease)

    monkeypatch.setattr(BrowserSessionLease, "close", fail_once)
    assert coordinator.cancel(ticket.token).status is VerificationStatus.FAILED
    with pytest.raises(VerificationRequired, match="verification_capacity"):
        coordinator.begin("https://other.test/a", task_key="other")
    assert coordinator.retry_cleanup(ticket.token)


def test_terminal_ledger_failure_never_precedes_lease_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = BrowserSessionStore(tmp_path, lock_timeout=0.05)
    coordinator = VerificationCoordinator(
        sessions,
        driver=FakeDriver([FakeContext([browser_snapshot("<p>x</p>")], [])]),
        safety_policy=PUBLIC_POLICY,
    )
    ticket = coordinator.begin("https://example.test/a", task_key="download")
    monkeypatch.setattr(coordinator._ledger, "finish", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")))
    assert coordinator.cancel(ticket.token).status is VerificationStatus.CANCELLED
    with sessions.acquire("example.test", timeout=0.1):
        pass
    assert coordinator._ledger_repairs


def test_expire_sweep_retries_deferred_ledger_finish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = BrowserSessionStore(tmp_path)
    coordinator = VerificationCoordinator(
        sessions,
        driver=FakeDriver([FakeContext([browser_snapshot("<p>x</p>")], [])]),
        safety_policy=PUBLIC_POLICY,
    )
    ticket = coordinator.begin("https://example.test/a", task_key="download")
    original = coordinator._ledger.finish
    failures = 1

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal failures
        if failures:
            failures -= 1
            raise OSError("disk")
        original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(coordinator._ledger, "finish", fail_once)
    coordinator.cancel(ticket.token)
    assert coordinator._ledger_repairs
    coordinator.expire_sweep()
    assert not coordinator._ledger_repairs


def test_begin_failure_releases_resources_even_when_ledger_finish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NavigateCrash(FakeContext):
        def navigate(self, url: str) -> BrowserPageSnapshot:
            raise RuntimeError("navigation failed")

    sessions = BrowserSessionStore(tmp_path, lock_timeout=0.05)
    coordinator = VerificationCoordinator(sessions, driver=FakeDriver([NavigateCrash([], [])]), safety_policy=PUBLIC_POLICY)
    monkeypatch.setattr(coordinator._ledger, "finish", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(VerificationRequired, match="verification_start_failed"):
        coordinator.begin("https://example.test/a", task_key="download")
    with sessions.acquire("example.test", timeout=0.1):
        pass
    assert coordinator._ledger_repairs


def test_partial_launch_cleanup_is_quarantined_until_retry_releases_lease(tmp_path: Path) -> None:
    class Cleanup:
        attempts = 0

        def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("cleanup failed")

    cleanup = Cleanup()

    class PartialDriver:
        def launch(self, **kwargs: object) -> object:
            raise DriverLaunchFailure(closed_ok=False, cleanup=cleanup)

    sessions = BrowserSessionStore(tmp_path, lock_timeout=0.05)
    coordinator = VerificationCoordinator(sessions, driver=PartialDriver(), max_active=1, safety_policy=PUBLIC_POLICY)
    with pytest.raises(VerificationRequired, match="verification_start_failed") as caught:
        coordinator.begin("https://example.test/a", task_key="download")
    assert caught.value.ticket is not None
    with pytest.raises(SessionLockTimeout):
        sessions.acquire("example.test", timeout=0.01)
    assert not coordinator.retry_cleanup(caught.value.ticket.token)
    with pytest.raises(SessionLockTimeout):
        sessions.acquire("example.test", timeout=0.01)
    assert coordinator.retry_cleanup(caught.value.ticket.token)
    assert cleanup.attempts == 2
    with sessions.acquire("example.test", timeout=0.1):
        pass


def test_browser_acquirer_quarantines_failed_headless_cleanup_with_retry_token(tmp_path: Path) -> None:
    class CloseFailsOnce(FakeContext):
        def __init__(self) -> None:
            super().__init__([browser_snapshot("<title>Chapter 1</title><article id='content'>ok</article>")], [])
            self.failures = 1

        def close(self) -> None:
            if self.failures:
                self.failures -= 1
                raise RuntimeError("cookie=secret C:/private")

    sessions = BrowserSessionStore(tmp_path, lock_timeout=0.05)
    acquirer = BrowserAcquirer(
        http=FakeHttp("<div id='app'></div>"),
        driver=FakeDriver([CloseFailsOnce()]),
        sessions=sessions,
        safety_policy=PUBLIC_POLICY,
        max_failed_cleanups=1,
    )
    with pytest.raises(BrowserCleanupRequired, match="browser_cleanup_failed") as caught:
        acquirer.fetch_page("https://example.test/a")
    assert "secret" not in repr(caught.value)
    with pytest.raises(SessionLockTimeout):
        sessions.acquire("example.test", timeout=0.01)
    with pytest.raises(AcquisitionError, match="browser_cleanup_capacity"):
        acquirer.fetch_page("https://other.test/a")
    assert acquirer.retry_cleanup(caught.value.token)
    with sessions.acquire("example.test", timeout=0.1):
        pass


def test_ledger_key_and_record_capacity_fail_closed(tmp_path: Path) -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    sessions = BrowserSessionStore(tmp_path)
    ledger = _AttemptLedger(sessions, lambda: now, timedelta(minutes=1), max_keys=1, max_records=2)
    first_key = ledger.opaque_key("https://one.test/", "download")
    second_key = ledger.opaque_key("https://two.test/", "download")
    ledger.reserve(first_key, 2)
    with pytest.raises(VerificationRequired, match="verification_ledger_capacity"):
        ledger.reserve(second_key, 2)

    other_sessions = BrowserSessionStore(tmp_path / "records")
    records = _AttemptLedger(other_sessions, lambda: now, timedelta(minutes=1), max_keys=2, max_records=1)
    records.reserve(first_key, 2)
    with pytest.raises(VerificationRequired, match="verification_ledger_capacity"):
        records.reserve(second_key, 2)


def test_ledger_process_reservation_survives_other_process_success(tmp_path: Path) -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    sessions = BrowserSessionStore(tmp_path)
    ledger = _AttemptLedger(sessions, lambda: now, timedelta(minutes=5))
    key = ledger.opaque_key("https://example.test/", "download")
    reservation_a = ledger.reserve(key, 2)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    process = context.Process(target=_reserve_ledger_in_process, args=(str(tmp_path), now.isoformat(), output))
    process.start()
    process.join(10)
    assert process.exitcode == 0
    ok, reservation_b = output.get(timeout=2)
    assert ok and reservation_b
    ledger.finish(key, reservation_a, consumed=False)
    reservation_c = ledger.reserve(key, 2)
    with pytest.raises(VerificationRequired, match="verification_attempts_exhausted"):
        ledger.reserve(key, 2)
    ledger.finish(key, str(reservation_b), consumed=True)
    ledger.finish(key, reservation_c, consumed=True)
