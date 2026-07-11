from __future__ import annotations

import json
import multiprocessing
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from novel_crawler.adaptation.config_manager import ConfigManager, ConfigResolution, ResolutionKind
from novel_crawler.adaptation.decision import DecisionConfig, DecisionKind, DecisionPolicy
from novel_crawler.adaptation.fingerprint import StructureFingerprint
from novel_crawler.adaptation.registry import ConfigRegistry, ConfigStatus, RegistryEntry
from novel_crawler.adaptation.revalidation import ConfigRevalidator, RevalidationResult, RevalidationStatus
from novel_crawler.adaptation.service import ProbeService
from novel_crawler.adaptation.url_paths import canonical_path
from novel_crawler.adaptation.validation import ConfigDraft, ValidationResult
from novel_crawler.browser.coordinator import VerificationRequired
from tests.adaptation.test_service import FakeAcquirer


def _draft(domain: str = "example.test") -> ConfigDraft:
    fingerprints = {
        "book": StructureFingerprint(1, "book", "1" * 64),
        "chapter_first": StructureFingerprint(1, "chapter", "2" * 64),
        "chapter_second": StructureFingerprint(1, "chapter", "3" * 64),
    }
    return ConfigDraft(
        "draft-v1",
        domain,
        {"title": 0.95, "chapter_list": 0.95, "chapter_title": 0.95, "content": 0.95},
        {"title": "h1.book", "chapter_list": "nav a", "chapter_title": "h1", "content": "article"},
        fingerprints=fingerprints,
        fingerprint_salt=b"s" * 32,
        navigation_paths=("/book/1", "/chapter/1", "/chapter/2"),
    )


def _validation(outcome: DecisionKind, draft: ConfigDraft | None = None) -> ValidationResult:
    confidence = 0.95 if outcome is DecisionKind.AUTO_ACCEPT else 0.75 if outcome is DecisionKind.REQUIRE_CONFIRMATION else 0.0
    return ValidationResult(outcome is not DecisionKind.REJECT, confidence, () if outcome is not DecisionKind.REJECT else ("page_rejected",), (outcome,), {"pages": 3, "failures": 0}, draft)


class Probe:
    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def probe(self, url: str, *, overrides: Mapping[str, str] | None = None) -> ValidationResult:
        self.calls.append(url)
        if overrides and self.result.config_draft is not None:
            draft = self.result.config_draft
            private = draft.to_config()
            updated = ConfigDraft(
                draft.version,
                draft.domain,
                draft.scores,
                {**private["selectors"], **overrides},
                fingerprints=private["fingerprints"],
                fingerprint_salt=private["fingerprint_salt"],
                navigation_paths=private["navigation_paths"],
            )
            return _validation(self.result.outcome, updated)
        return self.result


class Revalidator:
    def __init__(self, status: RevalidationStatus, registry: ConfigRegistry | None = None) -> None:
        self.status = status
        self.registry = registry
        self.calls = 0

    def revalidate(self, entry: object, url: str) -> RevalidationResult:
        del url
        self.calls += 1
        if self.status is RevalidationStatus.STALE and self.registry is not None:
            self.registry.mark_stale(entry.config_id, expected_version=entry.version)  # type: ignore[attr-defined]
        return RevalidationResult(self.status, (), {}, {}, "2026-07-11T09:00:00Z", entry if self.status is RevalidationStatus.VALID else None)  # type: ignore[arg-type]


class _ProcessProbe:
    def __init__(self, counter: object) -> None:
        self.counter = counter

    def probe(self, url: str, *, overrides: Mapping[str, str] | None = None) -> ValidationResult:
        del url, overrides
        lock = self.counter.get_lock()  # type: ignore[attr-defined]
        with lock:
            self.counter.value += 1  # type: ignore[attr-defined]
        time.sleep(0.1)
        return _validation(DecisionKind.AUTO_ACCEPT, _draft())


def _process_resolve(root: str, counter: object, barrier: object, queue: object) -> None:
    registry = ConfigRegistry(root)
    manager = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), _ProcessProbe(counter))  # type: ignore[arg-type]
    barrier.wait()  # type: ignore[attr-defined]
    queue.put(manager.resolve("https://example.test/book/1").kind.value)  # type: ignore[attr-defined]


def test_auto_register_then_reuse_without_second_probe(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    probe = Probe(_validation(DecisionKind.AUTO_ACCEPT, _draft()))
    manager = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), probe)

    first = manager.resolve("https://example.test/book/123?token=not-stored")
    second = manager.resolve("https://example.test/book/123?different=1")

    assert first.kind is ResolutionKind.REGISTERED
    assert second.kind is ResolutionKind.REUSED
    assert len(probe.calls) == 1
    assert first.config is not None and second.config is not None
    assert first.config.config_id == second.config.config_id
    assert first.config.url_patterns[0].template == "/book/{int}"
    assert len(first.config.validation_samples) == 3
    assert first.config.domain == "example.test"
    assert "token" not in first.config.to_json(include_sensitive=True)


def test_transient_revalidation_never_probes_or_mutates(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    setup = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.AUTO_ACCEPT, _draft())))
    setup.resolve("https://example.test/book/1")
    before = registry.list(include_history=True)
    probe = Probe(_validation(DecisionKind.AUTO_ACCEPT, _draft()))
    result = ConfigManager(registry, Revalidator(RevalidationStatus.TRANSIENT_FAILURE), probe).resolve("https://example.test/book/1")
    assert result.kind is ResolutionKind.TRANSIENT_FAILURE
    assert probe.calls == []
    assert registry.list(include_history=True) == before


def test_concurrent_revalidation_restarts_entire_resolution_before_reuse(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    created = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.AUTO_ACCEPT, _draft()))).resolve("https://example.test/book/1")
    assert created.config is not None

    class RacingRevalidator:
        def __init__(self) -> None:
            self.calls = 0

        def revalidate(self, entry: RegistryEntry, url: str) -> RevalidationResult:
            del url
            self.calls += 1
            if self.calls == 1:
                return RevalidationResult(RevalidationStatus.STALE, ("concurrent_revision",), {}, {}, "2026-07-11T09:00:00Z")
            return RevalidationResult(RevalidationStatus.VALID, (), {}, {}, "2026-07-11T09:00:00Z", entry)

    racing = RacingRevalidator()
    probe = Probe(_validation(DecisionKind.REJECT))
    result = ConfigManager(registry, racing, probe).resolve("https://example.test/book/1")
    assert result.kind is ResolutionKind.REUSED and result.config is not None
    assert racing.calls == 2 and probe.calls == []


def test_stale_reprobes_and_keeps_old_history_under_new_id(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    initial = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.AUTO_ACCEPT, _draft()))).resolve("https://example.test/book/1")
    reprobe = Probe(_validation(DecisionKind.AUTO_ACCEPT, _draft()))
    replaced = ConfigManager(registry, Revalidator(RevalidationStatus.STALE, registry), reprobe).resolve("https://example.test/book/1")
    assert replaced.kind is ResolutionKind.REGISTERED
    assert replaced.config is not None and initial.config is not None
    assert replaced.config.config_id != initial.config.config_id
    assert any(entry.config_id == initial.config.config_id and entry.status is ConfigStatus.STALE for entry in registry.list())


def test_confirmation_expiry_one_use_cancel_and_override_validation(tmp_path: Path) -> None:
    now = datetime(2026, 7, 11, 9, tzinfo=UTC)
    manager = ConfigManager(ConfigRegistry(tmp_path), Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.REQUIRE_CONFIRMATION, _draft())), clock=lambda: now)
    pending = manager.resolve("https://example.test/chapter/hello?q=private")
    assert pending.kind is ResolutionKind.CONFIRMATION_REQUIRED and pending.confirmation_token
    with pytest.raises(ValueError):
        manager.confirm(pending.confirmation_token, {"content": "p:-soup-contains(secret)"})
    confirmed = manager.confirm(pending.confirmation_token, {"content": "main article"})
    assert confirmed.kind is ResolutionKind.REGISTERED
    with pytest.raises(KeyError):
        manager.confirm(pending.confirmation_token)

    cancelled = manager.resolve("https://example.test/other/path")
    assert manager.cancel(cancelled.confirmation_token) is True
    assert manager.cancel(cancelled.confirmation_token) is False
    with pytest.raises(KeyError):
        manager.confirm(cancelled.confirmation_token)

    expired = manager.resolve("https://example.test/final/path")
    manager._clock = lambda: now + timedelta(minutes=11)
    with pytest.raises(KeyError):
        manager.confirm(expired.confirmation_token)


def test_slow_override_confirmation_does_not_block_unrelated_token_cancel(tmp_path: Path) -> None:
    manager = ConfigManager(ConfigRegistry(tmp_path), Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.REQUIRE_CONFIRMATION, _draft())))
    first = manager.resolve("https://example.test/book/1")
    second = manager.resolve("https://example.test/book/2")
    entered = threading.Event()
    release = threading.Event()

    class BlockingProbe(Probe):
        def probe(self, url: str, *, overrides: Mapping[str, str] | None = None) -> ValidationResult:
            entered.set()
            release.wait(timeout=2)
            return super().probe(url, overrides=overrides)

    manager.probe = BlockingProbe(_validation(DecisionKind.REQUIRE_CONFIRMATION, _draft()))
    thread = threading.Thread(target=manager.confirm, args=(first.confirmation_token, {"content": "article"}))
    thread.start()
    assert entered.wait(timeout=1)
    assert manager.cancel(second.confirmation_token) is True
    release.set()
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_confirmation_rejects_when_probe_does_not_apply_override(tmp_path: Path) -> None:
    class IgnoringProbe(Probe):
        def probe(self, url: str, *, overrides: Mapping[str, str] | None = None) -> ValidationResult:
            del overrides
            self.calls.append(url)
            return self.result

    registry = ConfigRegistry(tmp_path)
    manager = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), IgnoringProbe(_validation(DecisionKind.REQUIRE_CONFIRMATION, _draft())))
    pending = manager.resolve("https://example.test/book/1")
    result = manager.confirm(pending.confirmation_token, {"content": "main article"})
    assert result.kind is ResolutionKind.REJECTED and result.reason_ids == ("override_not_applied",)
    assert registry.list() == ()


def test_reject_and_incomplete_baseline_never_write(tmp_path: Path) -> None:
    rejected_root = tmp_path / "rejected"
    registry = ConfigRegistry(rejected_root)
    result = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.REJECT))).resolve("https://example.test/book/1")
    assert result.kind is ResolutionKind.REJECTED
    assert registry.list() == () and not list((rejected_root / "configs").glob("*.json"))

    incomplete = ConfigDraft("draft-v1", "example.test", {"content": 0.9}, {"content": "article"})
    result2 = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.AUTO_ACCEPT, incomplete))).resolve("https://example.test/book/1")
    assert result2.kind is ResolutionKind.REJECTED and registry.list() == ()


def test_resolution_and_draft_serializers_hide_sensitive_values(tmp_path: Path) -> None:
    draft = _draft("xn--fsqu00a.xn--0zwm56d")
    manager = ConfigManager(ConfigRegistry(tmp_path), Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.REQUIRE_CONFIRMATION, draft)))
    result = manager.resolve("https://例子.测试/book/private-title?q=secret")
    serialized = result.to_json()
    assert result.config is None and result.confirmation_token
    for secret in (result.confirmation_token, "selector", "article", "private-title", "xn--", "digest", "salt"):
        assert secret not in serialized
        assert secret not in json.dumps(draft.to_dict())
    with pytest.raises(AttributeError):
        result.kind = ResolutionKind.REJECTED  # type: ignore[misc]


def test_idna_slug_pattern_is_narrow_and_drops_query(tmp_path: Path) -> None:
    draft = _draft("xn--fsqu00a.xn--0zwm56d")
    result = ConfigManager(
        ConfigRegistry(tmp_path),
        Revalidator(RevalidationStatus.VALID),
        Probe(_validation(DecisionKind.AUTO_ACCEPT, draft)),
    ).resolve("https://例子.测试/book/private-title?access_token=private")
    assert result.kind is ResolutionKind.REGISTERED and result.config is not None
    assert result.config.domain == "xn--fsqu00a.xn--0zwm56d"
    assert {item.template for item in result.config.url_patterns} == {"/book/{int}", "/chapter/{int}"}
    assert "access_token" not in result.config.to_json(include_sensitive=True)


def test_materializes_all_verified_canonical_paths_and_percent_encoded_digits_match(tmp_path: Path) -> None:
    original = _draft()
    private = original.to_config()
    draft = ConfigDraft(
        original.version,
        original.domain,
        original.scores,
        private["selectors"],
        fingerprints=private["fingerprints"],
        fingerprint_salt=private["fingerprint_salt"],
        navigation_paths=("/book/%31", "/chapter/%31", "/chapter/%32"),
    )
    registry = ConfigRegistry(tmp_path)
    result = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.AUTO_ACCEPT, draft))).resolve("https://example.test/book/%31")
    assert result.config is not None
    assert {pattern.template for pattern in result.config.url_patterns} == {"/book/{int}", "/chapter/{int}"}
    assert registry.lookup("https://example.test/book/%31") is not None
    assert registry.lookup("https://example.test/chapter/2") is not None
    assert canonical_path("/book%2fadmin") == "/book%2Fadmin"
    assert canonical_path("/a/%2e%2e/b") == "/a/%2E%2E/b"


@pytest.mark.parametrize(
    ("chapter_paths", "expected", "third", "matches"),
    [
        (("/c1", "/c2"), "/c{int}", "/c3", True),
        (("/1.html", "/2.html"), "/{int}.html", "/3.html", True),
        (("/chapter10", "/chapter11"), "/chapter{int}", "/chapter20", True),
        (("/9.html", "/10.html"), "/{int}.html", "/20.html", True),
        (("/c099", "/c100"), "/c{int}", "/c101", True),
        (("/章1", "/章2"), "/%E7%AB%A0{int}", "/章3", True),
        (("/一", "/丁"), None, "/七", False),
        (("/v1c2", "/v2c3"), None, "/v3c4", False),
        (("/c1", "/d2"), None, "/c3", False),
        (("/c1", "/news"), None, "/c3", False),
        (("/a/1/x", "/b/2/x"), None, "/a/3/x", False),
    ],
)
def test_pairwise_sibling_inference_is_conservative(tmp_path: Path, chapter_paths: tuple[str, str], expected: str | None, third: str, matches: bool) -> None:
    original = _draft()
    private = original.to_config()
    draft = ConfigDraft(
        original.version,
        original.domain,
        original.scores,
        private["selectors"],
        fingerprints=private["fingerprints"],
        fingerprint_salt=private["fingerprint_salt"],
        navigation_paths=("/book", *chapter_paths),
    )
    registry = ConfigRegistry(tmp_path)
    result = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.AUTO_ACCEPT, draft))).resolve("https://example.test/book")
    assert result.config is not None
    templates = {pattern.template for pattern in result.config.url_patterns}
    assert (expected in templates) is (expected is not None)
    assert (registry.lookup(f"https://example.test{third}") is not None) is matches


def test_real_probe_override_materializes_sibling_pattern_and_revalidates_exact_baseline(tmp_path: Path) -> None:
    pages = {
        "https://example.test/book": '<h1>Book A</h1><div id="list"><a href="/c1">Chapter 1</a><a href="/c2">Chapter 2</a><a href="/c3">Chapter 3</a></div>',
        "https://example.test/c1": '<h1>Chapter 1</h1><article class="content"><p>' + "a" * 80 + '</p><p>x</p></article><a rel="next" href="/c2">Next</a>',
        "https://example.test/c2": '<h1>Chapter 2</h1><article class="content"><p>' + "b" * 90 + "</p><p>y</p></article>",
    }
    registry = ConfigRegistry(tmp_path)
    revalidator = ConfigRevalidator(
        acquirer=FakeAcquirer(pages),
        registry=registry,
        decision=DecisionPolicy(DecisionConfig(high=0.7, medium=0.5)),
        minimum_score=0.7,
    )
    manager = ConfigManager(registry, revalidator, ProbeService(acquirer=FakeAcquirer(pages)))
    pending = manager.resolve("https://example.test/book")
    assert pending.kind is ResolutionKind.CONFIRMATION_REQUIRED
    assert manager._pending[pending.confirmation_token].draft.selector("content") != "article.content"
    confirmed = manager.confirm(pending.confirmation_token, {"content": "article.content"})
    assert confirmed.kind is ResolutionKind.REGISTERED and confirmed.config is not None
    assert confirmed.config.selectors["chapter"]["content"] == "article.content"
    assert {pattern.template for pattern in confirmed.config.url_patterns} == {"/book", "/c{int}"}
    assert registry.lookup("https://example.test/c3") is not None
    reused = manager.resolve("https://example.test/book")
    assert reused.kind is ResolutionKind.REUSED and reused.config is not None
    assert reused.config.config_id == confirmed.config.config_id


def test_resolution_invariants_and_exact_root_pattern() -> None:
    with pytest.raises(ValueError):
        ConfigResolution(ResolutionKind.REJECTED, config=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ConfigResolution(ResolutionKind.REUSED, confirmation_token="private")
    assert ConfigManager._url_parts("https://example.test/") == ("example.test", "/")
    assert ConfigManager(ConfigRegistry.__new__(ConfigRegistry), Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.REJECT))).resolve("https://example.test/%ZZ").kind is ResolutionKind.REJECTED


@pytest.mark.parametrize(
    ("kind", "kwargs"),
    [
        (ResolutionKind.REUSED, {}),
        (ResolutionKind.REGISTERED, {"confirmation_token": "private"}),
        (ResolutionKind.CONFIRMATION_REQUIRED, {}),
        (ResolutionKind.CONFIRMATION_REQUIRED, {"config": object(), "confirmation_token": "private"}),
        (ResolutionKind.REJECTED, {"confirmation_token": "private"}),
        (ResolutionKind.TRANSIENT_FAILURE, {"config": object()}),
    ],
)
def test_resolution_rejects_every_invalid_sensitive_handle_combination(kind: ResolutionKind, kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ConfigResolution(kind, **kwargs)  # type: ignore[arg-type]


def test_valid_revalidation_never_falls_back_to_entry_removed_by_concurrent_change() -> None:
    class VanishingRegistry:
        def __init__(self) -> None:
            self.calls = 0
            self.loaded = False

        def lookup(self, url: str) -> object | None:
            del url
            self.calls += 1
            return RegistryEntry("cfg_abcdefghijklmnop", "example.test", ConfigStatus.ACTIVE, 1, "2026-07-11T09:00:00Z", "2026-07-11T09:00:00Z") if self.calls == 1 else None

        def load(self, entry: object) -> object:
            self.loaded = True
            raise AssertionError(f"must not load vanished entry {entry!r}")

    registry = VanishingRegistry()
    result = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.REJECT))).resolve("https://example.test/book/1")  # type: ignore[arg-type]
    assert result.kind is ResolutionKind.REJECTED
    assert registry.loaded is False


def test_shared_stateful_probe_is_serialized_across_domains(tmp_path: Path) -> None:
    class GuardedProbe(Probe):
        def __init__(self) -> None:
            super().__init__(_validation(DecisionKind.REJECT))
            self.active = 0
            self.maximum = 0
            self.lock = threading.Lock()

        def probe(self, url: str) -> ValidationResult:
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return super().probe(url)

    probe = GuardedProbe()
    managers = [ConfigManager(ConfigRegistry(tmp_path / str(i)), Revalidator(RevalidationStatus.VALID), probe) for i in range(2)]
    threads = [threading.Thread(target=manager.resolve, args=(f"https://example{i}.test/book/1",)) for i, manager in enumerate(managers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert probe.maximum == 1


@pytest.mark.parametrize("other_action", ["confirm", "resolve"])
def test_override_reprobe_shares_probe_lock_across_domains(tmp_path: Path, other_action: str) -> None:
    class StatefulProbe:
        def __init__(self) -> None:
            self.active = 0
            self.maximum = 0
            self.measure = False
            self.lock = threading.Lock()

        def probe(self, url: str, *, overrides: Mapping[str, str] | None = None) -> ValidationResult:
            domain = url.split("/")[2]
            if self.measure:
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                time.sleep(0.05)
                with self.lock:
                    self.active -= 1
            draft = _draft(domain)
            if overrides:
                private = draft.to_config()
                draft = ConfigDraft(
                    draft.version,
                    draft.domain,
                    draft.scores,
                    {**private["selectors"], **overrides},
                    fingerprints=private["fingerprints"],
                    fingerprint_salt=private["fingerprint_salt"],
                    navigation_paths=private["navigation_paths"],
                )
            return _validation(DecisionKind.REQUIRE_CONFIRMATION, draft)

    probe = StatefulProbe()
    manager = ConfigManager(ConfigRegistry(tmp_path), Revalidator(RevalidationStatus.VALID), probe)
    first = manager.resolve("https://one.test/book/1")
    second = manager.resolve("https://two.test/book/1")
    probe.measure = True
    actions = [lambda: manager.confirm(first.confirmation_token, {"content": "main article"})]
    if other_action == "confirm":
        actions.append(lambda: manager.confirm(second.confirmation_token, {"content": "main article"}))
    else:
        actions.append(lambda: manager.resolve("https://three.test/book/1"))
    threads = [threading.Thread(target=action) for action in actions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert probe.maximum == 1


def test_clean_selector_is_materialized_and_confirmation_retry_keeps_config_id(tmp_path: Path) -> None:
    draft = _draft()
    private = draft.to_config()
    selectors = dict(private["selectors"])
    selectors["clean_selector"] = ".advert"
    clean_draft = ConfigDraft(
        draft.version,
        draft.domain,
        draft.scores,
        selectors,
        fingerprints=private["fingerprints"],
        fingerprint_salt=private["fingerprint_salt"],
        navigation_paths=private["navigation_paths"],
    )

    class FlakyRegistry:
        def __init__(self) -> None:
            self.ids: list[str] = []
            self.config = None

        def lookup(self, url: str) -> None:
            del url
            return None

        def register(self, config: object) -> RegistryEntry:
            self.ids.append(config.config_id)  # type: ignore[attr-defined]
            self.config = config
            if len(self.ids) == 1:
                raise RuntimeError("manifest publication failed")
            return RegistryEntry(config.config_id, config.domain, ConfigStatus.ACTIVE, 1, config.generated_at, config.last_validated)  # type: ignore[attr-defined]

        def load_exact(self, config_id: str, version: int, status: ConfigStatus) -> object:
            assert self.config is not None
            assert (config_id, version, status) == (self.config.config_id, 1, ConfigStatus.ACTIVE)
            return self.config

    registry = FlakyRegistry()
    probe = Probe(_validation(DecisionKind.REQUIRE_CONFIRMATION, clean_draft))
    manager = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), probe)  # type: ignore[arg-type]
    pending = manager.resolve("https://example.test/book/1")
    with pytest.raises(RuntimeError):
        manager.confirm(pending.confirmation_token, {"content": "main article"})
    confirmed = manager.confirm(pending.confirmation_token, {"content": "main article"})
    assert confirmed.config is not None and confirmed.config.selectors["clean"] == (".advert",)
    assert len(set(registry.ids)) == 1
    assert len(probe.calls) == 2


def test_registration_that_resolves_to_non_active_revision_is_not_reported_registered() -> None:
    class RevokedRegistration:
        def lookup(self, url: str) -> None:
            del url
            return None

        def register(self, config: object) -> RegistryEntry:
            return RegistryEntry(config.config_id, "example.test", ConfigStatus.REVOKED, 2, "2026-07-11T09:00:00Z", "2026-07-11T09:00:00Z")  # type: ignore[attr-defined]

    result = ConfigManager(
        RevokedRegistration(),  # type: ignore[arg-type]
        Revalidator(RevalidationStatus.VALID),
        Probe(_validation(DecisionKind.AUTO_ACCEPT, _draft())),
    ).resolve("https://example.test/book/1")
    assert result.kind is ResolutionKind.TRANSIENT_FAILURE and result.config is None


def test_registration_retry_returns_exact_durable_revision_after_validated_advance(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    manager = ConfigManager(
        registry,
        Revalidator(RevalidationStatus.VALID),
        Probe(_validation(DecisionKind.AUTO_ACCEPT, _draft())),
    )
    config = manager._materialize("https://example.test/book/1", _draft())
    assert config is not None
    original_write_manifest = registry._write_manifest
    calls = 0

    def fail_after_revision() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("manifest publication failed")
        original_write_manifest()

    registry._write_manifest = fail_after_revision  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="manifest publication"):
        manager._register(config, "https://example.test/book/1")
    registry._write_manifest = original_write_manifest  # type: ignore[method-assign]

    concurrent = ConfigRegistry(tmp_path)
    first = concurrent.list()[0]
    latest = concurrent.mark_validated(first.config_id, "2026-07-11T10:00:00+00:00")
    resolution = manager._register(config, "https://example.test/book/1")

    assert resolution.kind is ResolutionKind.REGISTERED
    assert resolution.config == concurrent.load_exact(latest.config_id, latest.version, ConfigStatus.ACTIVE)
    assert resolution.config is not config
    assert resolution.config.last_validated == "2026-07-11T10:00:00Z"


def test_concurrent_resolve_is_idempotent(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    probe = Probe(_validation(DecisionKind.AUTO_ACCEPT, _draft()))
    managers = [ConfigManager(ConfigRegistry(tmp_path), Revalidator(RevalidationStatus.VALID), probe) for _ in range(4)]
    results = []
    threads = [
        threading.Thread(target=lambda item=manager: results.append(item.resolve("https://example.test/book/7")))
        for manager in managers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(registry.list()) == 1
    assert len(probe.calls) == 1
    assert {result.config.config_id for result in results if result.config} == {registry.list()[0].config_id}


def test_cross_process_managers_probe_and_register_once(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    counter = context.Value("i", 0)
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [context.Process(target=_process_resolve, args=(str(tmp_path), counter, barrier, queue)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert counter.value == 1
    assert {queue.get(timeout=2) for _ in processes} == {ResolutionKind.REGISTERED.value, ResolutionKind.REUSED.value}


def test_revoked_never_reused_and_registry_corruption_is_isolated(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path / "revoked")
    first = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.AUTO_ACCEPT, _draft()))).resolve("https://example.test/book/1")
    assert first.config
    entry = registry.list()[0]
    registry.mark_revoked(entry.config_id, expected_version=entry.version)
    probe = Probe(_validation(DecisionKind.REJECT))
    result = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), probe).resolve("https://example.test/book/1")
    assert result.kind is ResolutionKind.REJECTED and len(probe.calls) == 1

    class BrokenRegistry:
        def lookup(self, url: str) -> object:
            del url
            raise RuntimeError("private corrupt payload")

    isolated = ConfigManager(BrokenRegistry(), Revalidator(RevalidationStatus.VALID), probe).resolve("https://example.test/book/1")  # type: ignore[arg-type]
    assert isolated.kind is ResolutionKind.TRANSIENT_FAILURE
    assert "private corrupt payload" not in isolated.to_json()
def test_verification_required_is_not_hidden_by_manager() -> None:
    class RaisingRegistry:
        root = None

        def lookup(self, url: str) -> None:
            raise VerificationRequired(original_url=url, safe_origin="https://example.test")

    manager = ConfigManager(RaisingRegistry(), object(), object())  # type: ignore[arg-type]
    with pytest.raises(VerificationRequired):
        manager.resolve("https://example.test/private")
