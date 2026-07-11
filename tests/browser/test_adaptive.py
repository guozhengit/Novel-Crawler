from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

from novel_crawler.adaptation.config_manager import ConfigResolution, ResolutionKind
from novel_crawler.browser import AdaptiveBrowserService as ExportedAdaptiveBrowserService
from novel_crawler.browser.adaptive import AdaptiveBrowserService
from novel_crawler.browser.coordinator import VerificationRequired
from novel_crawler.browser.models import VerificationOutcome, VerificationStatus, VerificationTicket


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


class FakeCoordinator:
    def __init__(self, outcomes: list[VerificationOutcome]) -> None:
        self.outcomes = outcomes
        self.begin_calls: list[tuple[str, str]] = []
        self.continue_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.ticket = VerificationTicket(
            "private-ticket", VerificationStatus.WAITING, "https://example.test", datetime.now(UTC) + timedelta(minutes=5)
        )

    def begin(self, url: str, *, task_key: str) -> VerificationTicket:
        self.begin_calls.append((url, task_key))
        return self.ticket

    def continue_verification(self, token: str) -> VerificationOutcome:
        self.continue_calls.append(token)
        return self.outcomes.pop(0)

    def cancel(self, token: str) -> VerificationOutcome:
        self.cancel_calls.append(token)
        return VerificationOutcome(VerificationStatus.CANCELLED, "https://example.test")

    def expire_sweep(self) -> int:
        return 0


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
    assert service.resolve(url, "download").kind is ResolutionKind.VERIFICATION_FAILED


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
            self.cleanup_results = [False, False, True]

        def retry_cleanup(self, token: str) -> bool:
            assert token == "cleanup-token"
            return self.cleanup_results.pop(0)

    url = "https://example.test/private"
    coordinator = RetryableCleanup()
    service = AdaptiveBrowserService(FakeManager([required(url)]), FakeAcquirer(), coordinator)
    failed = service.resolve(url, "download")
    assert failed.ticket is coordinator.ticket
    assert failed.reason_ids == ("verification_cleanup_pending",)
    cleaned = service.retry_cleanup(failed.ticket)
    assert cleaned.ticket is None
    assert cleaned.reason_ids == ("verification_cleanup_completed",)


def test_wait_cancel_failure_timeout_and_task_keys_are_isolated() -> None:
    url = "https://example.test/private"
    manager = FakeManager([required(url), required(url)])
    coordinator = FakeCoordinator([VerificationOutcome(VerificationStatus.WAITING, "https://example.test", 1)])
    service = AdaptiveBrowserService(manager, FakeAcquirer(), coordinator)
    one = service.resolve(url, "one")
    two = service.resolve(url, "two")
    assert one.ticket is not None and two.ticket is not None
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
