from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import subprocess
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from novel_crawler.adaptation import ConfigRegistry as ExportedConfigRegistry
from novel_crawler.adaptation.config_schema import SiteConfig
from novel_crawler.adaptation.registry import (
    ConfigConflictError,
    ConfigRegistry,
    ConfigStatus,
    RegistryLimitError,
    RegistryLockTimeout,
)
from novel_crawler.adaptation.registry_io import RegistryIOError


def config(
    *,
    config_id: str = "cfg_0123456789abcdef",
    domain: str = "example.com",
    path: str = "/book/{int}",
    validated: str = "2026-07-11T08:00:00Z",
) -> SiteConfig:
    return SiteConfig.from_dict(
        {
            "schema_version": 1,
            "config_id": config_id,
            "site": "Example",
            "domain": domain,
            "url_patterns": [path],
            "selectors": {"clean": [".ad"], "book": {"title": "h1"}, "chapter": {"content": "article"}},
            "request_policy": {"timeout_seconds": 15, "max_retries": 2, "rate_limit_seconds": 0.5},
            "generated_at": "2026-07-11T07:00:00Z",
            "last_validated": validated,
            "field_scores": {},
            "validation_samples": [],
            "fingerprint_salt": "ab" * 32,
        }
    )


def _process_register(root: str, queue: multiprocessing.Queue[tuple[str, int]]) -> None:
    entry = ConfigRegistry(root).register(config())
    queue.put((entry.config_id, entry.version))


def _process_hold_lock(root: str, entered: multiprocessing.synchronize.Event) -> None:
    registry = ConfigRegistry(root)
    with registry._global_lock():
        entered.set()
        threading.Event().wait(30)


def _process_crash_after_revision(root: str) -> None:
    registry = ConfigRegistry(root)
    original = registry._io.atomic_publish_noreplace

    def crash(path: Path, payload: bytes) -> None:
        original(path, payload)
        if path.name.startswith("rev-"):
            os._exit(77)

    registry._io.atomic_publish_noreplace = crash  # type: ignore[method-assign]
    registry.register(config())


def _process_crash_before_revision_replace(root: str) -> None:
    registry = ConfigRegistry(root)
    if os.name == "nt":
        api = registry._io._api  # type: ignore[attr-defined]
        original = api.move_write_through

        def crash(source: Path, destination: Path, *, replace: bool = True) -> None:
            if destination.name.startswith("rev-"):
                os._exit(78)
            original(source, destination, replace=replace)

        api.move_write_through = crash
    else:
        original_replace = os.replace

        def crash_replace(source: object, destination: object, **kwargs: object) -> None:
            if str(destination).startswith("rev-"):
                os._exit(78)
            original_replace(source, destination, **kwargs)  # type: ignore[arg-type]

        os.replace = crash_replace  # type: ignore[assignment]
    registry.register(config())


def test_register_returns_immutable_safe_entry_and_load_is_explicit(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    original = config()

    entry = registry.register(original)

    assert entry.config_id == original.config_id
    assert entry.domain == "example.com"
    assert entry.status is ConfigStatus.ACTIVE
    assert entry.version == 1
    assert entry.created == "2026-07-11T07:00:00Z"
    assert entry.validated == "2026-07-11T08:00:00Z"
    assert entry.invalid_reason_ids == ()
    assert registry.load(entry) == original
    with pytest.raises(FrozenInstanceError):
        entry.status = ConfigStatus.STALE  # type: ignore[misc]
    rendered = repr(entry)
    assert "selectors" not in rendered and "fingerprint" not in rendered and "article" not in rendered


def test_register_is_idempotent_but_rejects_same_id_with_different_content(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    first = registry.register(config())
    assert registry.register(config()) == first
    assert len(registry.list()) == 1

    with pytest.raises(ConfigConflictError, match="conflicts"):
        registry.register(config(path="/chapter/{int}"))


def test_lookup_uses_exact_canonical_domain_pattern_status_and_deterministic_recency(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    older = registry.register(config(config_id="cfg_aaaaaaaaaaaaaaaa", validated="2026-07-11T08:00:00Z"))
    newer = registry.register(config(config_id="cfg_bbbbbbbbbbbbbbbb", validated="2026-07-11T09:00:00Z"))
    registry.register(config(config_id="cfg_cccccccccccccccc", domain="sub.example.com"))

    assert registry.lookup("https://EXAMPLE.com/book/42") == newer
    assert registry.lookup("https://sub.example.com/book/42").domain == "sub.example.com"  # type: ignore[union-attr]
    assert registry.lookup("https://www.example.com/book/42") is None
    assert registry.lookup("https://example.com/other/42") is None

    registry.mark_stale(newer.config_id)
    assert registry.lookup("https://example.com/book/42") == older


def test_status_transitions_append_immutable_revisions_and_validate_reasons(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    active = registry.register(config())
    stale = registry.mark_stale(active.config_id)
    invalid = registry.mark_invalid(active.config_id, ["selector.missing", "probe.timeout"])
    revoked = registry.mark_revoked(active.config_id)

    assert [stale.status, invalid.status, revoked.status] == [ConfigStatus.STALE, ConfigStatus.INVALID, ConfigStatus.REVOKED]
    assert [stale.version, invalid.version, revoked.version] == [2, 3, 4]
    assert invalid.invalid_reason_ids == ("probe.timeout", "selector.missing")
    assert len(list((tmp_path / "configs").rglob("*.json"))) == 4
    assert registry.list() == (revoked,)
    assert registry.list(include_history=True) == (active, stale, invalid, revoked)
    with pytest.raises(ValueError, match="reason"):
        registry.mark_invalid(active.config_id, ["secret=raw-token"])


def test_transition_state_machine_makes_revoked_terminal_except_admin_unrevoke(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    active = registry.register(config())
    stale = registry.mark_stale(active.config_id, expected_version=active.version, expected_status=ConfigStatus.ACTIVE)
    validated = registry.mark_validated(
        active.config_id,
        "2026-07-11T09:00:00Z",
        expected_version=stale.version,
        expected_status=ConfigStatus.STALE,
    )
    revoked = registry.mark_revoked(
        active.config_id,
        expected_version=validated.version,
        expected_status=ConfigStatus.ACTIVE,
    )
    for transition in (
        lambda: registry.mark_stale(active.config_id, expected_version=revoked.version, expected_status=ConfigStatus.REVOKED),
        lambda: registry.mark_invalid(active.config_id, ["hard_error"], expected_version=revoked.version, expected_status=ConfigStatus.REVOKED),
        lambda: registry.mark_validated(active.config_id, "2026-07-11T10:00:00Z", expected_version=revoked.version, expected_status=ConfigStatus.REVOKED),
        lambda: registry.mark_revoked(active.config_id, expected_version=revoked.version, expected_status=ConfigStatus.REVOKED),
    ):
        with pytest.raises(ConfigConflictError):
            transition()
    assert registry.list()[0] == revoked
    restored = registry.unrevoke(active.config_id, expected_version=revoked.version, expected_status=ConfigStatus.REVOKED)
    assert restored.status is ConfigStatus.STALE and restored.version == revoked.version + 1


@pytest.mark.parametrize(
    ("supplied", "canonical"),
    [
        ("2026-07-11T09:00:00+00:00", "2026-07-11T09:00:00Z"),
        ("2026-07-11T09:00:00.987654Z", "2026-07-11T09:00:00Z"),
    ],
)
def test_mark_validated_persists_one_canonical_config_and_reopens_cleanly(
    tmp_path: Path, supplied: str, canonical: str
) -> None:
    registry = ConfigRegistry(tmp_path)
    active = registry.register(config())

    validated = registry.mark_validated(active.config_id, supplied)

    assert validated.validated == canonical
    assert registry.load(validated).last_validated == canonical
    revision = json.loads(next((tmp_path / "configs").rglob("rev-000002.json")).read_text(encoding="utf-8"))
    assert revision["config"] == registry.load(validated).to_dict(include_sensitive=True)
    reopened = ConfigRegistry(tmp_path)
    assert reopened.list()[0] == validated
    assert reopened.load(validated).to_dict(include_sensitive=True) == revision["config"]
    assert not list((tmp_path / "quarantine").iterdir())


def test_transition_checks_expected_version_and_status_atomically(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    active = registry.register(config())
    with pytest.raises(ConfigConflictError):
        registry.mark_stale(active.config_id, expected_version=active.version, expected_status=ConfigStatus.STALE)
    assert registry.list()[0] == active


def test_manifest_is_safe_and_serializer_keeps_selectors_and_salt_only_in_revision(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    registry.register(config())

    manifest = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    assert "selectors" not in manifest
    assert "fingerprint_salt" not in manifest
    revision = next((tmp_path / "configs").rglob("*.json")).read_text(encoding="utf-8")
    assert '"selectors"' in revision and '"fingerprint_salt"' in revision


def test_concurrent_threads_register_one_revision(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    results = []

    def worker() -> None:
        results.append(registry.register(config()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8
    assert len(set(results)) == 1
    assert len(list((tmp_path / "configs").rglob("*.json"))) == 1


def test_recovery_ignores_temp_rebuilds_manifest_and_isolates_corruption(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    registry.register(config())
    (tmp_path / "manifest.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "configs" / "interrupted.tmp").write_text("raw secret should be ignored", encoding="utf-8")
    revision = next((tmp_path / "configs").rglob("*.json"))
    broken = revision.with_name("rev-000002.json")
    broken.write_text("{not-json", encoding="utf-8")

    recovered = ConfigRegistry(tmp_path)

    assert recovered.list() == ()
    assert recovered.lookup("https://example.com/book/1") is None
    assert list((tmp_path / "quarantine").glob("*.reason.json"))
    assert "raw secret" not in (tmp_path / "manifest.json").read_text(encoding="utf-8")


def test_recovery_rejects_symlink_and_enforces_size_and_count_limits(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    registry.register(config())
    revision = next((tmp_path / "configs").rglob("*.json"))

    with pytest.raises(RegistryLimitError, match="files"):
        ConfigRegistry(tmp_path, max_files=0)
    with pytest.raises(RegistryLimitError, match="bytes"):
        ConfigRegistry(tmp_path, max_config_bytes=8)

    if hasattr(os, "symlink"):
        link = revision.with_name("rev-000002.json")
        try:
            link.symlink_to(revision)
        except OSError:
            pass
        else:
            recovered = ConfigRegistry(tmp_path)
            assert recovered.list() == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_registry_applies_private_permissions_best_effort(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path / "private")
    registry.register(config())
    revision = next((tmp_path / "private" / "configs").rglob("*.json"))

    assert (tmp_path / "private").stat().st_mode & 0o777 == 0o700
    assert revision.stat().st_mode & 0o777 == 0o600


def test_filenames_are_hashes_not_config_or_domain_text(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    registry.register(config(config_id="cfg_pathlike_________", domain="example.com"))

    paths = [str(path.relative_to(tmp_path)) for path in (tmp_path / "configs").rglob("*.json")]
    assert paths and all("example.com" not in path and "cfg_" not in path for path in paths)
    decoded = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert decoded["registry_schema_version"] == 1


def test_package_exports_registry_and_corrupt_manifest_alone_is_quarantined(tmp_path: Path) -> None:
    assert ExportedConfigRegistry is ConfigRegistry
    registry = ConfigRegistry(tmp_path)
    registry.register(config())
    (tmp_path / "manifest.json").write_text('{"registry_schema_version":99,"token":"raw-secret"}', encoding="utf-8")

    recovered = ConfigRegistry(tmp_path)

    assert len(recovered.list()) == 1
    reasons = list((tmp_path / "quarantine").glob("*.reason.json"))
    assert reasons
    assert all("raw-secret" not in reason.read_text(encoding="utf-8") for reason in reasons)


def test_registry_rejects_symlinked_root_or_internal_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(Exception, match="symlink"):
        ConfigRegistry(linked)

    root = tmp_path / "root"
    root.mkdir()
    (root / "configs").symlink_to(target, target_is_directory=True)
    with pytest.raises(Exception, match="symlink"):
        ConfigRegistry(root)


def test_public_api_rejects_bad_arguments_and_unknown_revisions(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lock_timeout"):
        ConfigRegistry(tmp_path / "a", lock_timeout=0)
    with pytest.raises(ValueError, match="limits"):
        ConfigRegistry(tmp_path / "b", max_files=-1)
    with pytest.raises(ValueError, match="limits"):
        ConfigRegistry(tmp_path / "c", max_config_bytes=0)

    registry = ConfigRegistry(tmp_path / "registry")
    with pytest.raises(TypeError, match="SiteConfig"):
        registry.register({})  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        registry.load("cfg_ffffffffffffffff")
    with pytest.raises(TypeError, match="entry_or_id"):
        registry.load(1)  # type: ignore[arg-type]
    registered = registry.register(config())
    with pytest.raises(KeyError, match="revision"):
        registry.load(registered.config_id, version=99)
    with pytest.raises(KeyError):
        registry.mark_stale("cfg_ffffffffffffffff")
    with pytest.raises(ValueError, match="reason"):
        registry.mark_invalid(registered.config_id, [])
    assert registry.lookup("ftp://example.com/book/1") is None
    assert registry.lookup("https://user:password@example.com/book/1") is None
    assert registry.lookup("https://example.com:99999/book/1") is None


def test_filters_history_load_and_write_limits(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path / "normal")
    active = registry.register(config())
    stale = registry.mark_stale(active.config_id)

    assert registry.list(status=ConfigStatus.STALE) == (stale,)
    assert registry.list(domain="other.example") == ()
    assert registry.load(active.config_id, version=1) == config()

    zero = ConfigRegistry(tmp_path / "zero", max_files=0)
    with pytest.raises(RegistryLimitError, match="files"):
        zero.register(config())
    small = ConfigRegistry(tmp_path / "small", max_config_bytes=700)
    with pytest.raises(RegistryLimitError, match="bytes"):
        small.register(config())


def test_recovery_quarantines_unexpected_location_and_history_gap(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    entry = registry.register(config())
    revision = next((tmp_path / "configs").rglob("*.json"))
    envelope = json.loads(revision.read_text(encoding="utf-8"))
    envelope["entry"]["version"] = 2
    gap = revision.with_name("rev-000002.json")
    gap.write_text(json.dumps(envelope), encoding="utf-8")
    revision.unlink()
    unexpected = tmp_path / "configs" / "unexpected.json"
    unexpected.write_text("{}", encoding="utf-8")

    recovered = ConfigRegistry(tmp_path)

    assert recovered.list() == ()
    assert recovered.lookup("https://example.com/book/1") is None
    assert len(list((tmp_path / "quarantine").glob("*.reason.json"))) >= 2
    with pytest.raises(KeyError):
        recovered.load(entry)


def test_cross_process_registration_is_idempotent(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [context.Process(target=_process_register, args=(str(tmp_path), queue)) for _ in range(3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert {queue.get(timeout=2) for _ in processes} == {("cfg_0123456789abcdef", 1)}
    assert len(ConfigRegistry(tmp_path).list(include_history=True)) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "entry_fields",
        "reason_shape",
        "version_shape",
        "schema",
        "envelope_fields",
        "version_mismatch",
        "path_mismatch",
        "digest_shape",
        "digest_mismatch",
        "entry_config_mismatch",
    ],
)
def test_recovery_isolates_each_malformed_revision_shape(tmp_path: Path, mutation: str) -> None:
    root = tmp_path / mutation
    registry = ConfigRegistry(root)
    registry.register(config())
    revision = next((root / "configs").rglob("*.json"))
    envelope = json.loads(revision.read_text(encoding="utf-8"))
    if mutation == "entry_fields":
        del envelope["entry"]["created"]
    elif mutation == "reason_shape":
        envelope["entry"]["invalid_reason_ids"] = "bad"
    elif mutation == "version_shape":
        envelope["entry"]["version"] = True
    elif mutation == "schema":
        envelope["registry_schema_version"] = 99
    elif mutation == "envelope_fields":
        envelope["unexpected"] = True
    elif mutation == "version_mismatch":
        envelope["entry"]["version"] = 2
    elif mutation == "path_mismatch":
        envelope["entry"]["domain"] = "other.example"
    elif mutation == "digest_shape":
        envelope["content_sha256"] = "bad"
    elif mutation == "digest_mismatch":
        envelope["content_sha256"] = "0" * 64
    else:
        envelope["config"]["domain"] = "other.example"
        encoded = json.dumps(envelope["config"], ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        envelope["content_sha256"] = hashlib.sha256(encoded).hexdigest()
    revision_payload = {"entry": envelope["entry"], "config": envelope["config"]}
    revision_encoded = json.dumps(
        revision_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    envelope["revision_sha256"] = hashlib.sha256(revision_encoded).hexdigest()
    revision.write_text(json.dumps(envelope), encoding="utf-8")

    assert ConfigRegistry(root).list() == ()
    assert list((root / "quarantine").glob("*.reason.json"))


def test_transition_and_payload_limits_are_enforced_separately(tmp_path: Path) -> None:
    one = ConfigRegistry(tmp_path / "one", max_files=1)
    entry = one.register(config())
    with pytest.raises(RegistryLimitError, match="files"):
        one.mark_revoked(entry.config_id)

    tiny = ConfigRegistry(tmp_path / "tiny", max_config_bytes=400)
    with pytest.raises(RegistryLimitError, match="bytes"):
        tiny.register(config())


def test_oversized_or_symlinked_manifest_is_quarantined(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized"
    oversized.mkdir()
    (oversized / "manifest.json").write_bytes(b"x" * 4097)
    ConfigRegistry(oversized, max_files=0)
    assert list((oversized / "quarantine").glob("*.reason.json"))

    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    linked_root = tmp_path / "linked-manifest"
    linked_root.mkdir()
    try:
        (linked_root / "manifest.json").symlink_to(target)
    except OSError:
        return
    ConfigRegistry(linked_root)
    assert not (linked_root / "manifest.json").is_symlink()
    assert target.read_text(encoding="utf-8") == "{}"


def test_corruption_after_recovery_is_isolated_during_lookup_and_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ConfigRegistry(tmp_path)
    entry = registry.register(config())
    revision = next((tmp_path / "configs").rglob("*.json"))
    monkeypatch.setattr(registry, "_recover", lambda: None)
    revision.write_text("{broken-after-scan", encoding="utf-8")

    assert registry.lookup("https://example.com/book/1") is None
    with pytest.raises(Exception, match="unavailable"):
        registry.load(entry)
    assert list((tmp_path / "quarantine").glob("*.reason.json"))


def test_in_process_lock_wait_uses_bounded_timeout(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path, lock_timeout=0.05)
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with registry._global_lock():
            entered.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(timeout=1)
    try:
        with pytest.raises(RegistryLockTimeout, match="timed out"):
            registry.list()
    finally:
        release.set()
        thread.join(timeout=2)


def test_rejects_symlinked_ancestor_lock_file_and_hashed_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    ancestor = tmp_path / "ancestor"
    try:
        ancestor.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(Exception, match="symlink"):
        ConfigRegistry(ancestor / "nested")

    root = tmp_path / "lock-root"
    registry = ConfigRegistry(root)
    lock = root / "locks" / "registry.lock"
    lock.unlink()
    outside_file = outside / "outside.lock"
    outside_file.write_bytes(b"unchanged")
    lock.symlink_to(outside_file)
    with pytest.raises(Exception, match="symlink"):
        registry.list()
    assert outside_file.read_bytes() == b"unchanged"

    revision_root = tmp_path / "revision-root"
    registry = ConfigRegistry(revision_root)
    domain_dir = revision_root / "configs" / hashlib.sha256(b"example.com").hexdigest()
    domain_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(Exception, match="symlink"):
        registry.register(config())
    assert not list(outside.rglob("rev-*.json"))


def test_recovery_streaming_scan_and_reason_counts_are_bounded(tmp_path: Path) -> None:
    scan_root = tmp_path / "scan"
    (scan_root / "configs").mkdir(parents=True)
    for index in range(65):
        (scan_root / "configs" / f"junk-{index}.txt").write_text("x", encoding="utf-8")
    with pytest.raises(RegistryLimitError, match="scan"):
        ConfigRegistry(scan_root, max_files=1)

    registry = ConfigRegistry(tmp_path / "reasons")
    entry = registry.register(config())
    with pytest.raises(ValueError, match="64"):
        registry.mark_invalid(entry.config_id, [f"reason.{index}" for index in range(65)])


@pytest.mark.parametrize("mutation", ["created", "typed_id", "status_reasons", "too_many_reasons"])
def test_revision_metadata_semantics_survive_recomputed_digest(tmp_path: Path, mutation: str) -> None:
    root = tmp_path / mutation
    registry = ConfigRegistry(root)
    registry.register(config())
    revision = next((root / "configs").rglob("*.json"))
    envelope = json.loads(revision.read_text(encoding="utf-8"))
    if mutation == "created":
        envelope["entry"]["created"] = "2026-07-11T06:00:00Z"
    elif mutation == "typed_id":
        envelope["entry"]["config_id"] = 42
    elif mutation == "status_reasons":
        envelope["entry"]["invalid_reason_ids"] = ["unexpected.reason"]
    else:
        envelope["entry"]["status"] = "invalid"
        envelope["entry"]["invalid_reason_ids"] = [f"reason.{index}" for index in range(65)]
    revision_payload = {"entry": envelope["entry"], "config": envelope["config"]}
    encoded = json.dumps(revision_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    envelope["revision_sha256"] = hashlib.sha256(encoded).hexdigest()
    revision.write_text(json.dumps(envelope), encoding="utf-8")

    assert ConfigRegistry(root).list() == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_windows_junction_ancestor_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "junction-target"
    outside.mkdir()
    junction = tmp_path / "junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip("junction creation is unavailable")
    with pytest.raises(Exception, match="symlink|reparse"):
        ConfigRegistry(junction / "nested")


def test_revision_is_durable_before_manifest_and_manifest_crash_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ConfigRegistry(tmp_path)
    original = registry._io.atomic_write
    original_publish = registry._io.atomic_publish_noreplace
    writes: list[str] = []
    manifest_writes = 0

    def fail_manifest(path: Path, payload: bytes) -> None:
        nonlocal manifest_writes
        writes.append(path.name)
        if path.name == "manifest.json":
            manifest_writes += 1
            if manifest_writes == 2:
                raise RegistryIOError("injected manifest crash")
        original(path, payload)

    def record_revision(path: Path, payload: bytes) -> None:
        writes.append(path.name)
        original_publish(path, payload)

    monkeypatch.setattr(registry._io, "atomic_write", fail_manifest)
    monkeypatch.setattr(registry._io, "atomic_publish_noreplace", record_revision)
    with pytest.raises(RegistryIOError, match="injected"):
        registry.register(config())

    assert writes[-2].startswith("rev-") and writes[-1] == "manifest.json"
    assert len(list((tmp_path / "configs").rglob("rev-*.json"))) == 1
    recovered = ConfigRegistry(tmp_path)
    assert recovered.load(recovered.list()[0]) == config()


def test_quarantine_events_are_append_only_and_never_name_blocked(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    registry.register(config())
    revision = next((tmp_path / "configs").rglob("rev-*.json"))
    corrupt = b"{same-corrupt-content"
    registry._io.atomic_write(revision, corrupt)
    ConfigRegistry(tmp_path)
    first_events = set((tmp_path / "quarantine").glob("*.reason.json"))
    assert len(first_events) == 1

    registry._io.atomic_write(revision, corrupt)
    ConfigRegistry(tmp_path)
    all_events = set((tmp_path / "quarantine").glob("*.reason.json"))
    assert len(all_events) == 2
    assert first_events < all_events
    assert len(list((tmp_path / "quarantine").glob("*.bad"))) == 2


def test_quarantine_move_failure_is_fail_closed_after_event_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ConfigRegistry(tmp_path)
    registry.register(config())
    revision = next((tmp_path / "configs").rglob("rev-*.json"))
    registry._io.atomic_write(revision, b"{corrupt")

    def fail_move(source: Path, destination: Path) -> None:
        raise RegistryIOError("injected quarantine durability failure")

    monkeypatch.setattr(registry._io, "durable_move", fail_move)
    with pytest.raises(RegistryIOError, match="quarantine durability"):
        registry.list()
    assert revision.exists()
    assert len(list((tmp_path / "quarantine").glob("*.reason.json"))) == 1


def test_valid_digest_gap_cascades_quarantine_to_all_remaining_history(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    entry = registry.register(config())
    registry.mark_stale(entry.config_id)
    registry.mark_revoked(entry.config_id)
    revisions = sorted((tmp_path / "configs").rglob("rev-*.json"))
    revisions[1].unlink()

    recovered = ConfigRegistry(tmp_path)

    assert recovered.list(include_history=True) == ()
    assert not list((tmp_path / "configs").rglob("rev-*.json"))
    assert len(list((tmp_path / "quarantine").glob("*.bad"))) == 2


def test_rehashed_invalid_middle_revision_cascades_to_every_original(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    entry = registry.register(config())
    registry.mark_stale(entry.config_id)
    registry.mark_revoked(entry.config_id)
    middle = sorted((tmp_path / "configs").rglob("rev-*.json"))[1]
    envelope = json.loads(registry._io.read_bounded(middle, 1_048_576))
    envelope["entry"]["version"] = 4
    revision_payload = {"entry": envelope["entry"], "config": envelope["config"]}
    encoded = json.dumps(revision_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    envelope["revision_sha256"] = hashlib.sha256(encoded).hexdigest()
    registry._io.atomic_write(middle, json.dumps(envelope).encode())

    recovered = ConfigRegistry(tmp_path)

    assert recovered.list(include_history=True) == ()
    assert not list((tmp_path / "configs").rglob("rev-*.json"))
    assert len(list((tmp_path / "quarantine").glob("*.bad"))) == 3


@pytest.mark.parametrize("mode", ["corrupt", "delete"])
def test_latest_revision_corruption_or_deletion_cannot_roll_status_back(tmp_path: Path, mode: str) -> None:
    registry = ConfigRegistry(tmp_path)
    entry = registry.register(config())
    registry.mark_stale(entry.config_id)
    registry.mark_revoked(entry.config_id)
    latest = sorted((tmp_path / "configs").rglob("rev-*.json"))[-1]
    if mode == "corrupt":
        registry._io.atomic_write(latest, b"{corrupt-latest")
    else:
        latest.unlink()

    recovered = ConfigRegistry(tmp_path)

    assert recovered.list(include_history=True) == ()
    assert not list((tmp_path / "configs").rglob("rev-*.json"))


def test_unexplained_deletion_of_all_history_fails_closed(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    registry.register(config())
    for revision in (tmp_path / "configs").rglob("rev-*.json"):
        revision.unlink()
    with pytest.raises(Exception, match="missing config history"):
        ConfigRegistry(tmp_path)


def test_revision_publish_never_clobbers_adversarial_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ConfigRegistry(tmp_path)
    original_publish = registry._io.atomic_publish_noreplace
    inserted = b"adversarial-existing-revision"

    def insert_then_publish(path: Path, payload: bytes) -> None:
        registry._io.atomic_write(path, inserted)
        original_publish(path, payload)

    monkeypatch.setattr(registry._io, "atomic_publish_noreplace", insert_then_publish)
    with pytest.raises(Exception, match="conflict|already exists"):
        registry.register(config())
    revision = next((tmp_path / "configs").rglob("rev-*.json"))
    assert registry._io.read_bounded(revision, 1_048_576) == inserted


def test_revision_publish_existing_identical_bytes_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ConfigRegistry(tmp_path)
    original_publish = registry._io.atomic_publish_noreplace

    def insert_same_then_publish(path: Path, payload: bytes) -> None:
        registry._io.atomic_write(path, payload)
        original_publish(path, payload)

    monkeypatch.setattr(registry._io, "atomic_publish_noreplace", insert_same_then_publish)
    entry = registry.register(config())
    assert entry.version == 1
    assert registry.load(entry) == config()


def test_cross_process_lock_is_released_after_holder_crash(tmp_path: Path) -> None:
    ConfigRegistry(tmp_path)
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    process = context.Process(target=_process_hold_lock, args=(str(tmp_path), entered))
    process.start()
    assert entered.wait(timeout=5)
    process.terminate()
    process.join(timeout=5)
    assert process.exitcode is not None
    assert ConfigRegistry(tmp_path, lock_timeout=1).list() == ()


def test_abrupt_crash_after_revision_before_manifest_recovers_complete_config(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_process_crash_after_revision, args=(str(tmp_path),))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 77
    recovered = ConfigRegistry(tmp_path)
    assert recovered.load(recovered.list()[0]) == config()


def test_abrupt_crash_before_revision_replace_leaves_only_ignored_temp(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_process_crash_before_revision_replace, args=(str(tmp_path),))
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 78
    recovered = ConfigRegistry(tmp_path)
    assert recovered.list() == ()


def test_load_exact_rejects_revision_or_status_races(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path)
    entry = registry.register(config())
    assert registry.load_exact(entry.config_id, entry.version, ConfigStatus.ACTIVE).config_id == entry.config_id
    stale = registry.mark_stale(entry.config_id, expected_version=entry.version)
    with pytest.raises(ConfigConflictError):
        registry.load_exact(entry.config_id, entry.version, ConfigStatus.ACTIVE)
    assert registry.load_exact(stale.config_id, stale.version, ConfigStatus.STALE).config_id == entry.config_id


def test_resolution_locks_are_real_and_domain_scoped(tmp_path: Path) -> None:
    registry = ConfigRegistry(tmp_path, lock_timeout=0.1)
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with registry.resolution_lock("a.example"):
            entered.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(timeout=1)
    with registry.resolution_lock("b.example"):
        pass
    with pytest.raises(RegistryLockTimeout, match="timed out"):
        with ConfigRegistry(tmp_path, lock_timeout=0.05).resolution_lock("a.example"):
            pass
    release.set()
    thread.join(timeout=2)
    lock_names = {path.name for path in (tmp_path / "locks").glob("*.lock")}
    assert len([name for name in lock_names if name != "registry.lock"]) >= 2
