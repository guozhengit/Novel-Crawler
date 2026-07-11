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


@pytest.mark.parametrize("url", ["http://[::ffff:127.0.0.1]/", "http://[::ffff:a00:1]/"])
def test_validate_rejects_ipv4_mapped_private_ipv6(url: str) -> None:
    with pytest.raises(UrlSafetyError) as caught:
        UrlSafetyPolicy(resolver=StubResolver({})).validate(url)

    assert caught.value.code == "unsafe_address"


def test_validate_permits_ipv4_mapped_public_ipv6() -> None:
    target = UrlSafetyPolicy(resolver=StubResolver({})).validate("https://[::ffff:8.8.8.8]/")

    assert target.addresses == ("::ffff:8.8.8.8",)


@pytest.mark.parametrize("host", ["2130706433", "0177.0.0.1", "0x7f000001"])
def test_noncanonical_ipv4_forms_must_be_resolved_and_checked(host: str) -> None:
    resolver = StubResolver({host: ("127.0.0.1",)})

    with pytest.raises(UrlSafetyError) as caught:
        UrlSafetyPolicy(resolver=resolver).validate(f"http://{host}/")

    assert caught.value.code == "unsafe_address"
    assert resolver.calls == [(host, 80)]


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


@pytest.mark.parametrize("host", ["localhost\u3002", "\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54"])
def test_idna_separator_and_fullwidth_localhost_are_rejected(host: str) -> None:
    with pytest.raises(UrlSafetyError) as caught:
        UrlSafetyPolicy(resolver=StubResolver({})).validate(f"http://{host}/")

    assert caught.value.code == "localhost"


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
        ("https://[fe80::1%25eth0]/", "zone_identifier_not_allowed"),
        ("https://[2606:4700:4700::1111%25eth0]/", "zone_identifier_not_allowed"),
        ("https://example.com:/", "invalid_port"),
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


def test_validate_redirect_resolves_relative_target_against_source() -> None:
    resolver = StubResolver({"source.example": ("8.8.8.8",)})

    target = UrlSafetyPolicy(resolver=resolver).validate_redirect(
        "https://source.example/book/one?secret=1", "../chapter/two?token=2"
    )

    assert target.host == "source.example"
    assert target.port == 443
    assert resolver.calls == [("source.example", 443)]


def test_each_redirect_hop_invokes_policy_resolution() -> None:
    resolver = StubResolver(
        {
            "first.example": ("8.8.8.8",),
            "second.example": ("1.1.1.1",),
        }
    )
    policy = UrlSafetyPolicy(resolver=resolver)

    policy.validate_redirect("https://origin.example/start", "https://first.example/next")
    policy.validate_redirect("https://first.example/next", "https://second.example/final")

    assert resolver.calls == [("first.example", 443), ("second.example", 443)]


@pytest.mark.parametrize(
    ("url", "expected_port"),
    [
        ("http://example.com/", 80),
        ("https://example.com/", 443),
        ("http://example.com:8080/", 8080),
        ("https://example.com:444/", 444),
    ],
)
def test_default_and_explicit_ports_are_passed_to_resolver(url: str, expected_port: int) -> None:
    resolver = StubResolver({"example.com": ("8.8.8.8",)})

    assert UrlSafetyPolicy(resolver=resolver).validate(url).port == expected_port
    assert resolver.calls == [("example.com", expected_port)]
