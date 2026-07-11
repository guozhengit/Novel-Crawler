from __future__ import annotations

from collections.abc import Iterable

import pytest

from novel_crawler.acquisition.security import UrlSafetyError, UrlSafetyPolicy, redact_url


class StubResolver:
    def __init__(self, answers: dict[str, tuple[str, ...]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> Iterable[str]:
        self.calls.append((host, port))
        return self.answers[host]


def test_validate_resolves_idna_host_and_explicit_port() -> None:
    resolver = StubResolver({"xn--bcher-kva.example": ("93.184.216.34",)})

    target = UrlSafetyPolicy(resolver=resolver).validate("https://bücher.example:8443/book?q=secret")

    assert target.host == "xn--bcher-kva.example"
    assert target.port == 8443
    assert target.addresses == ("93.184.216.34",)
    assert resolver.calls == [("xn--bcher-kva.example", 8443)]


@pytest.mark.parametrize(
    ("url", "address"),
    [
        ("http://127.0.0.1/", "127.0.0.1"),
        ("http://10.1.2.3/", "10.1.2.3"),
        ("http://169.254.1.2/", "169.254.1.2"),
        ("http://169.254.169.254/latest/meta-data", "169.254.169.254"),
        ("http://[::1]/", "::1"),
        ("http://[fc00::1]/", "fc00::1"),
        ("http://[fe80::1]/", "fe80::1"),
    ],
)
def test_validate_rejects_unsafe_literal_addresses(url: str, address: str) -> None:
    resolver = StubResolver({})

    with pytest.raises(UrlSafetyError) as caught:
        UrlSafetyPolicy(resolver=resolver).validate(url)

    assert caught.value.code == "unsafe_address"
    assert caught.value.safe_url == url
    assert resolver.calls == []


def test_validate_permits_public_ipv6_literal_without_dns() -> None:
    target = UrlSafetyPolicy(resolver=StubResolver({})).validate("https://[2606:2800:220:1:248:1893:25c8:1946]/")

    assert target.host == "2606:2800:220:1:248:1893:25c8:1946"
    assert target.port == 443
    assert target.addresses == ("2606:2800:220:1:248:1893:25c8:1946",)


def test_validate_rejects_mixed_public_and_private_dns_answers() -> None:
    resolver = StubResolver({"example.com": ("93.184.216.34", "192.168.1.10")})

    with pytest.raises(UrlSafetyError, match="unsafe_address"):
        UrlSafetyPolicy(resolver=resolver).validate("https://example.com/")


@pytest.mark.parametrize("host", ["localhost", "localhost.", "LOCALHOST", "api.localhost"])
def test_validate_rejects_localhost_names(host: str) -> None:
    resolver = StubResolver({})

    with pytest.raises(UrlSafetyError) as caught:
        UrlSafetyPolicy(resolver=resolver).validate(f"http://{host}/")

    assert caught.value.code == "localhost"
    assert resolver.calls == []


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("ftp://example.com/file", "scheme_not_allowed"),
        ("https://user:password@example.com/", "credentials_not_allowed"),
        ("https:///missing-host", "malformed_host"),
        ("https://example.com:99999/", "invalid_port"),
        ("https://example.com:not-a-port/", "invalid_port"),
        ("https://bad host.example/", "malformed_host"),
        ("https://bad_host.example/", "malformed_host"),
    ],
)
def test_validate_rejects_invalid_urls_without_resolving(url: str, code: str) -> None:
    with pytest.raises(UrlSafetyError) as caught:
        UrlSafetyPolicy(resolver=StubResolver({})).validate(url)

    assert caught.value.code == code


def test_error_url_and_string_redact_query_fragment_and_credentials() -> None:
    url = "https://alice:super-secret@example.com/book?token=top-secret#private"

    with pytest.raises(UrlSafetyError) as caught:
        UrlSafetyPolicy(resolver=StubResolver({})).validate(url)

    assert caught.value.safe_url == "https://example.com/book"
    assert "super-secret" not in str(caught.value)
    assert "top-secret" not in str(caught.value)
    assert redact_url(url) == "https://example.com/book"


def test_validate_redirect_validates_target_and_allows_public_cross_domain() -> None:
    resolver = StubResolver({"other.example": ("8.8.8.8",)})
    policy = UrlSafetyPolicy(resolver=resolver)

    target = policy.validate_redirect("https://source.example/start?secret=1", "https://other.example/next?token=2")

    assert target.host == "other.example"
    assert resolver.calls == [("other.example", 443)]


def test_validate_redirect_rejects_unsafe_target_and_redacts_it() -> None:
    policy = UrlSafetyPolicy(resolver=StubResolver({}))

    with pytest.raises(UrlSafetyError) as caught:
        policy.validate_redirect("https://source.example/", "http://127.0.0.1/admin?token=secret")

    assert caught.value.safe_url == "http://127.0.0.1/admin"
    assert "secret" not in str(caught.value)
