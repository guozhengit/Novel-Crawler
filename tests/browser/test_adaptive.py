from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

from novel_crawler.adaptation.config_manager import ConfigManager, ConfigResolution, ResolutionKind
from novel_crawler.adaptation.registry import ConfigRegistry
from novel_crawler.adaptation.revalidation import ConfigRevalidator
from novel_crawler.adaptation.service import ProbeService
from novel_crawler.browser import AdaptiveBrowserService as ExportedAdaptiveBrowserService
from novel_crawler.browser.adaptive import AdaptiveBrowserService, AdaptiveResult
from novel_crawler.browser.coordinator import (
    BrowserAcquirer,
    BrowserCleanupRequired,
    VerificationCoordinator,
    VerificationRequired,
)
from novel_crawler.browser.models import VerificationOutcome, VerificationStatus, VerificationTicket
from novel_crawler.browser.sessions import BrowserSessionStore
from tests.browser.test_coordinator import PUBLIC_POLICY, FakeContext, FakeDriver, FakeHttp, browser_snapshot


class FakeManager:
    def __init__(self, values: list[ConfigResolution | Exception]) -> None:
        self.values = values
        self.calls: list[str] = []

    def resolve(self, url: str) -> ConfigResolution:
        self.calls.append(url)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeAcquirer:
    def __init__(self) -> None:
        self.activations: list[tuple[str, str, int]] = []
        self.deactivations: list[tuple[str, str]] = []

    def activate_persistent_profile(self, url: str, *, task_key: str, pages: int) -> None:
        self.activations.append((url, task_key, pages))

    def deactivate_persistent_profile(self, url: str, *, task_key: str) -> None:
        self.deactivations.append((url, task_key))

    def retry_cleanup(self, token: str) -> bool:
        return True


class FakeCoordinator:
    def __init__(self, outcomes: list[VerificationOutcome]) -> None:
        self.outcomes = outcomes
        self.begin_calls: list[tuple[str, str]] = []
        self.continue_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self._counter = 0
        self.ticket = VerificationTicket(
            "private-ticket", VerificationStatus.WAITING, "https://example.test", datetime.now(UTC) + timedelta(minutes=5)
        )

    def begin(self, url: str, *, task_key: str) -> VerificationTicket:
        self.begin_calls.append((url, task_key))
        if self.ticket.status is VerificationStatus.WAITING:
            self._counter += 1
            self.ticket = VerificationTicket(
                f"private-ticket-{self._counter}",
                VerificationStatus.WAITING,
                self.ticket.safe_origin,
                self.ticket.expires_at,
            )
        return self.ticket

    def continue_verification(self, token: str) -> VerificationOutcome:
        self.continue_calls.append(token)
        return self.outcomes.pop(0)

    def cancel(self, token: str) -> VerificationOutcome:
        self.cancel_calls.append(token)
        return VerificationOutcome(VerificationStatus.CANCELLED, "https://example.test")

    def expire_sweep(self) -> int:
        return 0

    def retry_cleanup(self, token: str) -> bool:
        return True


def required(url: str) -> VerificationRequired:
    return VerificationRequired(original_url=url, safe_origin="https://example.test")


def test_adaptive_service_is_exported_from_browser_package() -> None:
    assert ExportedAdaptiveBrowserService is AdaptiveBrowserService


def test_resolve_reuses_one_private_ticket_and_serialization_is_safe() -> None:
    url = "https://example.test/private?token=secret"
    manager = FakeManager([required(url)])
    coordinator = FakeCoordinator([])
    service = AdaptiveBrowserService(manager, FakeAcquirer(), coordinator)

    first = service.resolve(url, "download")
    second = service.resolve(url, "download")

    assert first.kind is ResolutionKind.WAITING_FOR_USER
    assert first.ticket is coordinator.ticket
    assert second.ticket is coordinator.ticket
    assert coordinator.begin_calls == [(url, "download")]
    rendered = repr(first) + first.to_json() + json.dumps(first.to_dict())
    assert "private-ticket" not in rendered
    assert "token=secret" not in rendered


def test_verified_continuation_activates_three_profile_pages_and_reruns_resolution() -> None:
    url = "https://example.test/private?token=secret"
    terminal = ConfigResolution(ResolutionKind.REJECTED, reason_ids=("probe_rejected",))
    manager = FakeManager([required(url), terminal])
    acquirer = FakeAcquirer()
    coordinator = FakeCoordinator([VerificationOutcome(VerificationStatus.COMPLETED, "https://example.test")])
    service = AdaptiveBrowserService(manager, acquirer, coordinator)
    waiting = service.resolve(url, "download")

    result = service.continue_verification(waiting.ticket)

    assert result.kind is ResolutionKind.REJECTED
    assert manager.calls == [url, url]
    assert acquirer.activations == [(url, "download", 3)]
    assert acquirer.deactivations == [(url, "download")]
    assert service.continue_verification(waiting.ticket) is result


def test_nested_challenge_resumes_original_request_url() -> None:
    request_url = "https://example.test/book"
    challenge_url = "https://example.test/chapter/2?token=secret"
    terminal = ConfigResolution(ResolutionKind.REJECTED, reason_ids=("probe_rejected",))
    manager = FakeManager([required(challenge_url), terminal])
    acquirer = FakeAcquirer()
    coordinator = FakeCoordinator([VerificationOutcome(VerificationStatus.COMPLETED, "https://example.test")])
    service = AdaptiveBrowserService(manager, acquirer, coordinator)

    waiting = service.resolve(request_url, "download")
    assert service.continue_verification(waiting.ticket).kind is ResolutionKind.REJECTED
    assert manager.calls == [request_url, request_url]
    assert acquirer.activations == [(challenge_url, "download", 3)]


def test_begin_failure_is_mapped_instead_of_escaping() -> None:
    class FailingBegin(FakeCoordinator):
        def begin(self, url: str, *, task_key: str) -> VerificationTicket:
            raise VerificationRequired("verification_capacity")

    url = "https://example.test/private"
    service = AdaptiveBrowserService(FakeManager([required(url)]), FakeAcquirer(), FailingBegin([]))
    result = service.resolve(url, "download")
    assert result.kind is ResolutionKind.VERIFICATION_FAILED
    assert result.reason_ids == ("verification_capacity",)


def test_failed_start_ticket_is_never_presented_as_waiting() -> None:
    url = "https://example.test/private"
    coordinator = FakeCoordinator([])
    coordinator.ticket = VerificationTicket("cleanup-token", VerificationStatus.FAILED, "https://example.test")
    service = AdaptiveBrowserService(FakeManager([required(url)]), FakeAcquirer(), coordinator)
    result = service.resolve(url, "download")
    assert result.kind is ResolutionKind.CLEANUP_REQUIRED
    assert result.cleanup_ticket == "cleanup-token"
    assert service.resolve(url, "download").cleanup_ticket == result.cleanup_ticket
    assert len(coordinator.begin_calls) == 1


def test_same_origin_task_reuses_ticket_across_different_urls() -> None:
    first = "https://example.test/book"
    manager = FakeManager([required(first)])
    coordinator = FakeCoordinator([])
    service = AdaptiveBrowserService(manager, FakeAcquirer(), coordinator)
    one = service.resolve(first, "download")
    two = service.resolve("https://example.test/chapter/2", "download")
    assert two.ticket is one.ticket
    assert manager.calls == [first]


def test_failed_start_cleanup_remains_retryable() -> None:
    class RetryableCleanup(FakeCoordinator):
        def __init__(self) -> None:
            super().__init__([])
            self.ticket = VerificationTicket("cleanup-token", VerificationStatus.FAILED, "https://example.test")
            self.cleanup_results = [False, True]

        def retry_cleanup(self, token: str) -> bool:
            assert token == "cleanup-token"
            return self.cleanup_results.pop(0)

    url = "https://example.test/private"
    coordinator = RetryableCleanup()
    service = AdaptiveBrowserService(FakeManager([required(url)]), FakeAcquirer(), coordinator)
    failed = service.resolve(url, "download")
    assert failed.cleanup_ticket == "cleanup-token"
    assert service.retry_cleanup(failed.cleanup_ticket).kind is ResolutionKind.CLEANUP_REQUIRED
    cleaned = service.retry_cleanup(failed.cleanup_ticket)
    assert cleaned.reason_ids == ("verification_cleanup_completed",)


def test_resolve_singleflight_calls_manager_and_begin_once() -> None:
    url = "https://example.test/private"
    entered = threading.Event()
    release = threading.Event()

    class BlockingManager(FakeManager):
        def resolve(self, requested: str) -> ConfigResolution:
            self.calls.append(requested)
            entered.set()
            release.wait(2)
            raise required(requested)

    manager = BlockingManager([])
    coordinator = FakeCoordinator([])
    service = AdaptiveBrowserService(manager, FakeAcquirer(), coordinator)
    results: list[object] = []
    threads = [threading.Thread(target=lambda: results.append(service.resolve(url, "download"))) for _ in range(2)]
    threads[0].start()
    assert entered.wait(1)
    threads[1].start()
    release.set()
    for thread in threads:
        thread.join()
    assert manager.calls == [url]
    assert coordinator.begin_calls == [(url, "download")]
    assert results[0] is results[1]


def test_different_request_keys_resolve_concurrently() -> None:
    barrier = threading.Barrier(3)

    class ParallelManager(FakeManager):
        def resolve(self, url: str) -> ConfigResolution:
            self.calls.append(url)
            barrier.wait()
            return ConfigResolution(ResolutionKind.REJECTED, reason_ids=("probe_rejected",))

    manager = ParallelManager([])
    service = AdaptiveBrowserService(manager, FakeAcquirer(), FakeCoordinator([]))
    threads = [threading.Thread(target=service.resolve, args=(f"https://{host}.test/book", "download")) for host in ("one", "two")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len(manager.calls) == 2


def test_real_adaptation_flow_resumes_three_distinct_pages_in_persistent_profile(tmp_path) -> None:
    book = "https://example.test/book"
    c1 = "https://example.test/c1"
    c2 = "https://example.test/c2"
    index_html = '<h1>Book A</h1><div id="list"><a href="/c1">Chapter 1</a><a href="/c2">Chapter 2</a><a href="/c3">Chapter 3</a></div>'
    c1_html = '<h1>Chapter 1</h1><article><p>' + "a" * 100 + '</p><p>x</p></article><a rel="next" href="/c2">Next</a>'
    c2_html = '<h1>Chapter 2</h1><article><p>' + "b" * 110 + '</p><p>y</p></article>'
    auth = browser_snapshot('<title>Login</title><form><input type="password"></form>', book)
    visible = FakeContext([auth, browser_snapshot(index_html, book), browser_snapshot(index_html, book)], [])
    headless = [
        FakeContext([browser_snapshot(index_html, book)], []),
        FakeContext([browser_snapshot(c1_html, c1)], []),
        FakeContext([browser_snapshot(c2_html, c2)], []),
    ]
    class CookieDriver(FakeDriver):
        def __init__(self, contexts):
            super().__init__(contexts)
            self.cookies = {}

        def launch(self, *, user_data_dir, headless: bool, policy):
            if headless:
                assert self.cookies.get(user_data_dir) == "verified"
            else:
                self.cookies[user_data_dir] = "verified"
            return super().launch(user_data_dir=user_data_dir, headless=headless, policy=policy)

    driver = CookieDriver([visible, *headless])
    sessions = BrowserSessionStore(tmp_path / "sessions")
    acquirer = BrowserAcquirer(
        http=FakeHttp('<title>Login</title><form><input type="password"></form>'),
        driver=driver,
        sessions=sessions,
        safety_policy=PUBLIC_POLICY,
    )
    registry = ConfigRegistry(tmp_path / "registry")
    manager = ConfigManager(
        registry,
        ConfigRevalidator(acquirer=acquirer, registry=registry),
        ProbeService(acquirer=acquirer),
    )
    coordinator = VerificationCoordinator(sessions, driver=driver, safety_policy=PUBLIC_POLICY)
    service = AdaptiveBrowserService(manager, acquirer, coordinator)

    waiting = service.resolve(book, "download")
    result = service.continue_verification(waiting.ticket)

    if result.kind is ResolutionKind.CONFIRMATION_REQUIRED:
        assert result.confirmation_token is not None
        result = AdaptiveResult(manager.confirm(result.confirmation_token))
    assert result.kind is ResolutionKind.REGISTERED
    assert [context.calls[0][1] for context in headless] == [book, c1, c2]
    profile_paths = [call[1] for call in driver.calls]
    assert len(set(profile_paths)) == 1
    assert registry.lookup(book) is not None


def test_wait_cancel_failure_timeout_and_task_keys_are_isolated() -> None:
    url = "https://example.test/private"
    manager = FakeManager([required(url), required(url)])
    coordinator = FakeCoordinator([VerificationOutcome(VerificationStatus.WAITING, "https://example.test", 1)])
    service = AdaptiveBrowserService(manager, FakeAcquirer(), coordinator)
    one = service.resolve(url, "one")
    two = service.resolve(url, "two")
    assert one.ticket is not None and two.ticket is not None
    assert one.ticket.token != two.ticket.token
    assert len(coordinator.begin_calls) == 2
    assert service.continue_verification(one.ticket).kind is ResolutionKind.WAITING_FOR_USER
    assert service.cancel(two.ticket).kind is ResolutionKind.CANCELLED


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (VerificationStatus.TIMED_OUT, ResolutionKind.TIMED_OUT),
        (VerificationStatus.FAILED, ResolutionKind.VERIFICATION_FAILED),
    ],
)
def test_terminal_verification_statuses_fail_closed(status: VerificationStatus, kind: ResolutionKind) -> None:
    url = "https://example.test/private"
    service = AdaptiveBrowserService(
        FakeManager([required(url)]),
        FakeAcquirer(),
        FakeCoordinator([VerificationOutcome(status, "https://example.test")]),
    )
    waiting = service.resolve(url, "download")
    assert service.continue_verification(waiting.ticket).kind is kind


def test_visible_cleanup_signal_is_retained_until_retry() -> None:
    url = "https://example.test/private"
    manager = FakeManager([required(url)])
    coordinator = FakeCoordinator(
        [
            VerificationOutcome(
                VerificationStatus.FAILED,
                "https://example.test",
                cleanup_required=True,
                cleanup_ticket="visible-cleanup-token",
            )
        ]
    )
    service = AdaptiveBrowserService(manager, FakeAcquirer(), coordinator)
    waiting = service.resolve(url, "download")
    cleanup = service.continue_verification(waiting.ticket)
    assert cleanup.kind is ResolutionKind.CLEANUP_REQUIRED
    assert cleanup.cleanup_source == "visible"
    assert "visible-cleanup-token" not in cleanup.to_json() + repr(cleanup)
    assert service.retry_cleanup(cleanup.cleanup_ticket).kind is ResolutionKind.VERIFICATION_FAILED


def test_headless_cleanup_signal_retries_acquirer_then_resolution() -> None:
    url = "https://example.test/private"
    manager = FakeManager(
        [
            BrowserCleanupRequired("headless-cleanup-token", "https://example.test"),
            ConfigResolution(ResolutionKind.REJECTED, reason_ids=("probe_rejected",)),
        ]
    )
    service = AdaptiveBrowserService(manager, FakeAcquirer(), FakeCoordinator([]))
    cleanup = service.resolve(url, "download")
    assert cleanup.kind is ResolutionKind.CLEANUP_REQUIRED
    assert cleanup.cleanup_source == "headless"
    assert service.resolve(url, "download").cleanup_ticket == cleanup.cleanup_ticket
    assert manager.calls == [url]
    assert service.retry_cleanup(cleanup.cleanup_ticket).kind is ResolutionKind.REJECTED


def test_cancel_cleanup_retries_then_returns_cancelled() -> None:
    class CleanupCancel(FakeCoordinator):
        def cancel(self, token: str) -> VerificationOutcome:
            return VerificationOutcome(
                VerificationStatus.FAILED,
                "https://example.test",
                cleanup_required=True,
                cleanup_ticket="cancel-cleanup-token",
            )

    url = "https://example.test/private"
    service = AdaptiveBrowserService(FakeManager([required(url)]), FakeAcquirer(), CleanupCancel([]))
    waiting = service.resolve(url, "download")
    cleanup = service.cancel(waiting.ticket)
    assert cleanup.kind is ResolutionKind.CLEANUP_REQUIRED
    assert service.retry_cleanup(cleanup.cleanup_ticket).kind is ResolutionKind.CANCELLED


def test_cleanup_during_verified_resume_retries_entire_resolution() -> None:
    url = "https://example.test/private"
    manager = FakeManager(
        [
            required(url),
            BrowserCleanupRequired("resume-cleanup-token", "https://example.test"),
            ConfigResolution(ResolutionKind.REJECTED, reason_ids=("probe_rejected",)),
        ]
    )
    coordinator = FakeCoordinator([VerificationOutcome(VerificationStatus.COMPLETED, "https://example.test")])
    service = AdaptiveBrowserService(manager, FakeAcquirer(), coordinator)
    waiting = service.resolve(url, "download")
    cleanup = service.continue_verification(waiting.ticket)
    assert cleanup.cleanup_source == "headless"
    assert service.resolve(url, "download").cleanup_ticket == cleanup.cleanup_ticket
    assert service.continue_verification(waiting.ticket).cleanup_ticket == cleanup.cleanup_ticket
    assert service.continue_verification(waiting.ticket.token).cleanup_ticket == cleanup.cleanup_ticket
    assert service.cancel(waiting.ticket).cleanup_ticket == cleanup.cleanup_ticket
    assert service.retry_cleanup(cleanup.cleanup_ticket).kind is ResolutionKind.REJECTED
    assert manager.calls == [url, url, url]


def test_cleanup_retry_error_remains_retryable_and_unknown_token_fails() -> None:
    class RetryError(FakeCoordinator):
        def retry_cleanup(self, token: str) -> bool:
            raise VerificationRequired("verification_in_progress")

    url = "https://example.test/private"
    coordinator = RetryError([])
    coordinator.ticket = VerificationTicket("cleanup-token", VerificationStatus.FAILED, "https://example.test")
    service = AdaptiveBrowserService(FakeManager([required(url)]), FakeAcquirer(), coordinator)
    cleanup = service.resolve(url, "download")
    retried = service.retry_cleanup(cleanup.cleanup_ticket)
    assert retried.kind is ResolutionKind.CLEANUP_REQUIRED
    assert retried.reason_ids == ("verification_in_progress",)
    assert service.retry_cleanup("unknown").kind is ResolutionKind.VERIFICATION_FAILED


@pytest.mark.parametrize(
    ("expired", "kind"),
    [(False, ResolutionKind.VERIFICATION_FAILED), (True, ResolutionKind.TIMED_OUT)],
)
def test_continue_coordinator_error_maps_safely(expired: bool, kind: ResolutionKind) -> None:
    class ContinueError(FakeCoordinator):
        def continue_verification(self, token: str) -> VerificationOutcome:
            raise VerificationRequired("verification_token_invalid")

    url = "https://example.test/private"
    coordinator = ContinueError([])
    if expired:
        coordinator.ticket = VerificationTicket(
            "expired-ticket", VerificationStatus.WAITING, "https://example.test", datetime.now(UTC) - timedelta(seconds=1)
        )
    service = AdaptiveBrowserService(FakeManager([required(url)]), FakeAcquirer(), coordinator)
    waiting = service.resolve(url, "download")
    assert service.continue_verification(waiting.ticket).kind is kind


def test_expire_sweep_finishes_expired_context() -> None:
    url = "https://example.test/private"
    coordinator = FakeCoordinator([VerificationOutcome(VerificationStatus.TIMED_OUT, "https://example.test")])
    coordinator.ticket = VerificationTicket(
        "expired-ticket", VerificationStatus.WAITING, "https://example.test", datetime.now(UTC) - timedelta(seconds=1)
    )
    service = AdaptiveBrowserService(FakeManager([required(url)]), FakeAcquirer(), coordinator)
    service.resolve(url, "download")
    assert service.expire_sweep() == 1


def test_concurrent_cleanup_retry_runs_underlying_cleanup_once() -> None:
    class CountingAcquirer(FakeAcquirer):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_calls = 0

        def retry_cleanup(self, token: str) -> bool:
            self.cleanup_calls += 1
            return True

    url = "https://example.test/private"
    acquirer = CountingAcquirer()
    manager = FakeManager(
        [BrowserCleanupRequired("headless-cleanup-token", "https://example.test"), ConfigResolution(ResolutionKind.REJECTED)]
    )
    service = AdaptiveBrowserService(manager, acquirer, FakeCoordinator([]))
    cleanup = service.resolve(url, "download")
    barrier = threading.Barrier(3)
    results = []

    def retry() -> None:
        barrier.wait()
        results.append(service.retry_cleanup(cleanup.cleanup_ticket))

    threads = [threading.Thread(target=retry) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert acquirer.cleanup_calls == 1
    assert results[0] is results[1]


def test_profile_deactivation_failure_is_retryable_before_terminal_result() -> None:
    class DeactivateFailsOnce(FakeAcquirer):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 1

        def deactivate_persistent_profile(self, url: str, *, task_key: str) -> None:
            if self.failures:
                self.failures -= 1
                raise RuntimeError("private cleanup failure")
            super().deactivate_persistent_profile(url, task_key=task_key)

    url = "https://example.test/private"
    manager = FakeManager([required(url), ConfigResolution(ResolutionKind.REJECTED)])
    coordinator = FakeCoordinator([VerificationOutcome(VerificationStatus.COMPLETED, "https://example.test")])
    service = AdaptiveBrowserService(manager, DeactivateFailsOnce(), coordinator)
    waiting = service.resolve(url, "download")
    cleanup = service.continue_verification(waiting.ticket)
    assert cleanup.kind is ResolutionKind.CLEANUP_REQUIRED
    assert cleanup.reason_ids == ("profile_cleanup_required",)
    assert service.retry_cleanup(cleanup.cleanup_ticket).kind is ResolutionKind.REJECTED


def test_partial_profile_activation_failure_always_deactivates() -> None:
    class PartialActivation(FakeAcquirer):
        def __init__(self) -> None:
            super().__init__()
            self.deactivated = False

        def activate_persistent_profile(self, url: str, *, task_key: str, pages: int) -> None:
            raise RuntimeError("partial activation")

        def deactivate_persistent_profile(self, url: str, *, task_key: str) -> None:
            self.deactivated = True

    url = "https://example.test/private"
    acquirer = PartialActivation()
    coordinator = FakeCoordinator([VerificationOutcome(VerificationStatus.COMPLETED, "https://example.test")])
    service = AdaptiveBrowserService(FakeManager([required(url)]), acquirer, coordinator)
    waiting = service.resolve(url, "download")
    assert service.continue_verification(waiting.ticket).kind is ResolutionKind.VERIFICATION_FAILED
    assert acquirer.deactivated


def test_combined_cleanup_transitions_to_profile_only_after_headless_success() -> None:
    class CombinedCleanup(FakeAcquirer):
        def __init__(self) -> None:
            super().__init__()
            self.deactivate_failures = 2
            self.headless_retries = 0

        def retry_cleanup(self, token: str) -> bool:
            self.headless_retries += 1
            return True

        def deactivate_persistent_profile(self, url: str, *, task_key: str) -> None:
            if self.deactivate_failures:
                self.deactivate_failures -= 1
                raise RuntimeError("profile cleanup failed")

    url = "https://example.test/private"
    manager = FakeManager(
        [
            required(url),
            BrowserCleanupRequired("combined-token", "https://example.test"),
            ConfigResolution(ResolutionKind.REJECTED),
        ]
    )
    acquirer = CombinedCleanup()
    coordinator = FakeCoordinator([VerificationOutcome(VerificationStatus.COMPLETED, "https://example.test")])
    service = AdaptiveBrowserService(manager, acquirer, coordinator)
    waiting = service.resolve(url, "download")
    cleanup = service.continue_verification(waiting.ticket)
    assert service.retry_cleanup(cleanup.cleanup_ticket).kind is ResolutionKind.CLEANUP_REQUIRED
    assert service.retry_cleanup(cleanup.cleanup_ticket).kind is ResolutionKind.REJECTED
    assert acquirer.headless_retries == 1


def test_concurrent_continue_advances_one_generation_once() -> None:
    url = "https://example.test/private"
    manager = FakeManager([required(url)])
    coordinator = FakeCoordinator([VerificationOutcome(VerificationStatus.WAITING, "https://example.test", 1)])
    service = AdaptiveBrowserService(manager, FakeAcquirer(), coordinator)
    waiting = service.resolve(url, "download")
    barrier = threading.Barrier(3)
    results = []

    def run() -> None:
        barrier.wait()
        results.append(service.continue_verification(waiting.ticket))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(coordinator.continue_calls) == 1
    assert results[0] is results[1]
