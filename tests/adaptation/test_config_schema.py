from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, is_dataclass

import pytest

from novel_crawler.adaptation.config_schema import SafeUrlPattern, SiteConfig
from novel_crawler.adaptation.fingerprint import StructureFingerprint


def valid_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "config_id": "cfg_0123456789abcdef",
        "site": "Example Books",
        "domain": "EXAMPLE.com.",
        "url_patterns": ["/book/{int}", "https://example.com/chapter/{slug}"],
        "selectors": {
            "clean": [".advert", "script"],
            "book": {"title": "h1.book-title", "chapter_list": "#chapters a"},
            "chapter": {"title": "h1", "content": "article.content"},
        },
        "request_policy": {"timeout_seconds": 12.5, "max_retries": 2, "rate_limit_seconds": 0.5},
        "generated_at": "2026-07-11T08:00:00Z",
        "last_validated": "2026-07-11T09:00:00+00:00",
        "field_scores": {"book.title": 0.95, "chapter.content": 1.0},
        "validation_samples": [{"page_kind": "chapter", "matched_fields": 2, "node_count_bucket": "100-999"}],
        "fingerprint_salt": "11" * 32,
    }


def test_sensitive_round_trip_and_safe_summary() -> None:
    config = SiteConfig.from_dict(valid_payload())
    exported = config.to_dict(include_sensitive=True)
    restored = SiteConfig.from_json(json.dumps(exported))

    assert restored == config
    assert config.domain == "example.com"
    assert config.generated_at == "2026-07-11T08:00:00Z"
    assert "selectors" not in config.to_dict()
    assert "fingerprint_salt" not in config.to_dict()
    assert "selectors" not in config.safe_summary()
    assert "/book/" not in json.dumps(config.safe_summary())
    assert "selectors" not in repr(config)
    assert config.config_id not in repr(config)
    assert config.site not in repr(config)
    assert config.config_id not in json.dumps(config.safe_summary())
    assert not is_dataclass(config)


def test_config_is_deeply_immutable() -> None:
    config = SiteConfig.from_dict(valid_payload())
    with pytest.raises(FrozenInstanceError):
        config.domain = "evil.example"  # type: ignore[misc]
    with pytest.raises(TypeError):
        config.selectors["book"]["title"] = "body"  # type: ignore[index]


def test_safe_url_template_compiles_escaped_linear_regex() -> None:
    pattern = SafeUrlPattern.parse("/book/{int}/chapter/{slug}", "example.com")
    assert pattern.matches("/book/12/chapter/hello-world")
    assert not pattern.matches("/book/1/2/chapter/x")
    assert SafeUrlPattern.parse("/assets/*", "example.com").matches("/assets/app.js")
    assert SafeUrlPattern.parse("/files/**", "example.com").matches("/files/a/b.txt")
    assert SafeUrlPattern.parse("/c{int}", "example.com").matches("/c3")
    assert SafeUrlPattern.parse("/{int}.html", "example.com").matches("/42.html")
    assert not SafeUrlPattern.parse("/c{int}", "example.com").matches("/cnews")


@pytest.mark.parametrize("change", [
    {"extra": True},
    {"schema_version": 2},
    {"field_scores": {"x": 1.1}},
    {"generated_at": "yesterday"},
    {"domain": "user:pass@example.com"},
    {"url_patterns": ["https://example.com/x?token=secret"]},
    {"url_patterns": ["(a+)+$"]},
    {"url_patterns": ["/x/[0-9]+"]},
    {"url_patterns": ["/x/(foo|bar)"]},
    {"url_patterns": ["/x/(?=secret)"]},
    {"url_patterns": ["/x/**/tail"]},
    {"selectors": {"clean": [], "book": {"title": "div["}, "chapter": {}}},
    {"validation_samples": [{"page_kind": "chapter", "title": "private prose"}]},
])
def test_strictly_rejects_invalid_or_private_payloads(change: dict[str, object]) -> None:
    payload = valid_payload()
    payload.update(change)
    with pytest.raises((TypeError, ValueError)):
        SiteConfig.from_dict(payload)


def test_rejects_missing_fields_and_generates_safe_id() -> None:
    payload = valid_payload()
    del payload["config_id"]
    with pytest.raises(ValueError, match="missing"):
        SiteConfig.from_dict(payload)

    generated = SiteConfig.new(
        site="Example", domain="example.com", url_patterns=("/book/{int}",),
        selectors={"clean": (), "book": {"title": "h1"}, "chapter": {"content": "article"}},
    )
    assert generated.config_id.startswith("cfg_")
    assert len(generated.config_id) >= 20
    assert len(generated.fingerprint_salt) == 32


def test_create_requires_complete_three_page_fingerprint_baseline() -> None:
    payload = valid_payload()
    with pytest.raises(ValueError, match="validation_samples"):
        SiteConfig.create(
            site="Example",
            domain="example.com",
            url_patterns=["/book/*"],
            selectors=payload["selectors"],
            validation_samples=[],
            fingerprint_salt=b"s" * 32,
        )

    samples = [
        {"page_kind": "book", "fingerprint": StructureFingerprint(1, "book", "1" * 64).to_dict()},
        {"page_kind": "chapter_first", "fingerprint": StructureFingerprint(1, "chapter", "2" * 64).to_dict()},
        {"page_kind": "chapter_second", "fingerprint": StructureFingerprint(1, "chapter", "3" * 64).to_dict()},
    ]
    created = SiteConfig.create(
        site="Example",
        domain="example.com",
        url_patterns=["/book/*"],
        selectors=payload["selectors"],
        validation_samples=samples,
        fingerprint_salt=b"s" * 32,
    )
    assert [sample["page_kind"] for sample in created.validation_samples] == ["book", "chapter_first", "chapter_second"]


@pytest.mark.parametrize("field,value", [
    ("config_id", "bad"), ("site", ""), ("domain", "-bad.example"),
    ("url_patterns", "not-a-list"), ("url_patterns", ["ftp://example.com/x"]),
    ("url_patterns", ["https://other.example/x"]), ("url_patterns", ["/["]),
    ("url_patterns", ["https://exa_mple.com/x"]), ("url_patterns", ["https://example.com:99999/x"]),
    ("request_policy", {}),
    ("request_policy", {"timeout_seconds": 0, "max_retries": 2, "rate_limit_seconds": 0.5}),
    ("request_policy", {"timeout_seconds": 1, "max_retries": True, "rate_limit_seconds": 0.5}),
    ("request_policy", {"timeout_seconds": 1, "max_retries": 2, "rate_limit_seconds": -1}),
    ("field_scores", []),
])
def test_rejects_invalid_field_shapes(field: str, value: object) -> None:
    payload = valid_payload()
    payload[field] = value
    with pytest.raises((TypeError, ValueError)):
        SiteConfig.from_dict(payload)


@pytest.mark.parametrize("sample", [
    "bad", {"page_kind": "other"}, {"page_kind": "chapter", "matched_fields": -1},
    {"page_kind": "chapter", "node_count_bucket": "many"},
    {"page_kind": "chapter", "success": 1},
    {"page_kind": "chapter", "selector_match_counts": {"content": -1}},
])
def test_rejects_invalid_validation_summaries(sample: object) -> None:
    payload = valid_payload()
    payload["validation_samples"] = sample if isinstance(sample, str) else [sample]
    with pytest.raises((TypeError, ValueError)):
        SiteConfig.from_dict(payload)


def test_json_validation_and_public_json_redaction() -> None:
    config = SiteConfig.from_dict(valid_payload())
    assert "selectors" not in config.to_json()
    with pytest.raises(ValueError):
        SiteConfig.from_json("{")
    with pytest.raises(TypeError):
        SiteConfig.from_dict([])  # type: ignore[arg-type]


def test_version_pipeline_applies_injected_migration_to_runtime_target(monkeypatch: pytest.MonkeyPatch) -> None:
    import novel_crawler.adaptation.config_schema as schema

    called = []
    parsed_versions = []

    def v1_to_v2(payload: dict[str, object]) -> dict[str, object]:
        called.append(payload["schema_version"])
        return {**payload, "schema_version": 2}

    def parse_v2(payload: Mapping[str, object]) -> dict[str, object]:
        parsed_versions.append(payload.get("schema_version"))
        if payload.get("schema_version") != 2:
            raise ValueError("v2 parser requires schema_version 2")
        return dict(payload)

    version_registry = schema.DEFAULT_SCHEMA_REGISTRY.register_parser(2, parse_v2).register_migration(1, v1_to_v2)
    monkeypatch.setattr(schema, "CURRENT_SCHEMA_VERSION", 2)
    config = schema.parse_config(valid_payload(), version_registry=version_registry)
    assert called == [1]
    assert config.schema_version == 2
    serialized = config.to_sensitive_dict()
    assert schema.parse_config(serialized, version_registry=version_registry).to_sensitive_dict() == serialized
    assert parsed_versions == [2, 2]

    without_migration = schema.DEFAULT_SCHEMA_REGISTRY.register_parser(2, parse_v2)
    with pytest.raises(ValueError, match="missing migration from schema_version 1"):
        schema.parse_config(valid_payload(), version_registry=without_migration)


def test_version_pipeline_requires_a_parser_for_current_runtime_target(monkeypatch: pytest.MonkeyPatch) -> None:
    import novel_crawler.adaptation.config_schema as schema

    monkeypatch.setattr(schema, "CURRENT_SCHEMA_VERSION", 2)
    with pytest.raises(ValueError, match="missing parser for current schema_version 2"):
        schema.parse_config(valid_payload())


def test_v1_runtime_target_still_parses_without_migration() -> None:
    assert SiteConfig.from_dict(valid_payload()).schema_version == 1


def test_selector_limits_apply_across_all_config_groups() -> None:
    payload = valid_payload()
    payload["selectors"] = {
        "clean": [f".ad{i}" for i in range(10)],
        "book": {f"b{i}": "h1" for i in range(6)},
        "chapter": {f"c{i}": "p" for i in range(5)},
    }
    with pytest.raises(ValueError, match="20"):
        SiteConfig.from_dict(payload)

    long_selector = "." + "a" * 500
    payload["selectors"] = {"clean": [long_selector] * 9, "book": {}, "chapter": {}}
    with pytest.raises(ValueError, match="4096"):
        SiteConfig.from_dict(payload)


@pytest.mark.parametrize("selector", ["p:-soup-contains(private-text)", "p:-SOUP-CONTAINS-OWN(private-text)"])
def test_rejects_soupsieve_text_contains_selectors(selector: str) -> None:
    payload = valid_payload()
    payload["selectors"] = {"clean": [], "book": {"title": selector}, "chapter": {}}
    with pytest.raises(ValueError):
        SiteConfig.from_dict(payload)
