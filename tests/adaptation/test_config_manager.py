from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from novel_crawler.adaptation.config_manager import ConfigManager, ConfigResolution, ResolutionKind
from novel_crawler.adaptation.decision import DecisionKind
from novel_crawler.adaptation.fingerprint import StructureFingerprint
from novel_crawler.adaptation.registry import ConfigRegistry, ConfigStatus, RegistryEntry
from novel_crawler.adaptation.revalidation import RevalidationResult, RevalidationStatus
from novel_crawler.adaptation.validation import ConfigDraft, ValidationResult


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
    )


def _validation(outcome: DecisionKind, draft: ConfigDraft | None = None) -> ValidationResult:
    confidence = 0.95 if outcome is DecisionKind.AUTO_ACCEPT else 0.75 if outcome is DecisionKind.REQUIRE_CONFIRMATION else 0.0
    return ValidationResult(outcome is not DecisionKind.REJECT, confidence, () if outcome is not DecisionKind.REJECT else ("page_rejected",), (outcome,), {"pages": 3, "failures": 0}, draft)


class Probe:
    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def probe(self, url: str) -> ValidationResult:
        self.calls.append(url)
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
        return RevalidationResult(self.status, (), {}, {}, "2026-07-11T09:00:00Z")


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


def test_reject_and_incomplete_baseline_never_write(tmp_path: Path) -> None:
    rejected_root = tmp_path / "rejected"
    registry = ConfigRegistry(rejected_root)
    before = set(rejected_root.rglob("*"))
    result = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.REJECT))).resolve("https://example.test/book/1")
    assert result.kind is ResolutionKind.REJECTED
    assert registry.list() == () and set(rejected_root.rglob("*")) == before

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
    assert result.config.url_patterns[0].template == "/book/{slug}"
    assert "access_token" not in result.config.to_json(include_sensitive=True)


def test_resolution_invariants_and_exact_root_pattern() -> None:
    with pytest.raises(ValueError):
        ConfigResolution(ResolutionKind.REJECTED, config=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ConfigResolution(ResolutionKind.REUSED, confirmation_token="private")
    assert ConfigManager._url_parts("https://example.test/") == ("example.test", "/")
    assert ConfigManager(ConfigRegistry.__new__(ConfigRegistry), Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.REJECT))).resolve("https://example.test/%ZZ").kind is ResolutionKind.REJECTED


def test_valid_revalidation_never_falls_back_to_entry_removed_by_concurrent_change() -> None:
    class VanishingRegistry:
        def __init__(self) -> None:
            self.calls = 0
            self.loaded = False

        def lookup(self, url: str) -> object | None:
            del url
            self.calls += 1
            return object() if self.calls == 1 else None

        def load(self, entry: object) -> object:
            self.loaded = True
            raise AssertionError(f"must not load vanished entry {entry!r}")

    registry = VanishingRegistry()
    result = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.REJECT))).resolve("https://example.test/book/1")  # type: ignore[arg-type]
    assert result.kind is ResolutionKind.TRANSIENT_FAILURE
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
    )

    class FlakyRegistry:
        def __init__(self) -> None:
            self.ids: list[str] = []

        def lookup(self, url: str) -> None:
            del url
            return None

        def register(self, config: object) -> object:
            self.ids.append(config.config_id)  # type: ignore[attr-defined]
            if len(self.ids) == 1:
                raise RuntimeError("manifest publication failed")
            return object()

    registry = FlakyRegistry()
    manager = ConfigManager(registry, Revalidator(RevalidationStatus.VALID), Probe(_validation(DecisionKind.REQUIRE_CONFIRMATION, clean_draft)))  # type: ignore[arg-type]
    pending = manager.resolve("https://example.test/book/1")
    with pytest.raises(RuntimeError):
        manager.confirm(pending.confirmation_token, {"content": "main article"})
    confirmed = manager.confirm(pending.confirmation_token, {"content": "main article"})
    assert confirmed.config is not None and confirmed.config.selectors["clean"] == (".advert",)
    assert len(set(registry.ids)) == 1


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
    assert result.kind is ResolutionKind.REJECTED and result.config is None


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
