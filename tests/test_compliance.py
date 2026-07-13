from novel_crawler.compliance import ALLOW_THIRD_PARTY_ENV, decide_third_party_access, is_local_or_test_url


def test_local_test_and_documentation_domains_are_allowed_by_default(monkeypatch) -> None:
    monkeypatch.delenv(ALLOW_THIRD_PARTY_ENV, raising=False)

    assert is_local_or_test_url("http://localhost:8000/book")
    assert is_local_or_test_url("https://example.test/book")
    assert is_local_or_test_url("https://example.com/book")
    assert decide_third_party_access("https://example.test/book").allowed


def test_third_party_public_urls_require_explicit_switch(monkeypatch) -> None:
    monkeypatch.delenv(ALLOW_THIRD_PARTY_ENV, raising=False)

    blocked = decide_third_party_access("https://www.qidian.com/chapter/1/2/")
    assert not blocked.allowed
    assert blocked.code == "third_party_crawl_disabled"

    monkeypatch.setenv(ALLOW_THIRD_PARTY_ENV, "1")
    assert decide_third_party_access("https://www.qidian.com/chapter/1/2/").allowed


def test_switch_name_is_stable() -> None:
    assert ALLOW_THIRD_PARTY_ENV == "NOVEL_CRAWLER_ALLOW_THIRD_PARTY"
