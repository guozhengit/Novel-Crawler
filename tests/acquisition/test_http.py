from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from typing import Any

import pytest

from novel_crawler.acquisition.http import AcquisitionError, HttpPageAcquirer, TransportResponse
from novel_crawler.acquisition.models import PageSnapshot, RedirectHop
from novel_crawler.acquisition.security import UrlSafetyPolicy
from novel_crawler.core.fetcher import Fetcher, FetchOptions


class FakeTransport:
    def __init__(self, responses: list[TransportResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> TransportResponse:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(status: int = 200, *, headers: dict[str, str] | None = None, body: bytes = b"ok") -> TransportResponse:
    return TransportResponse(status, headers or {"Content-Type": "text/html; charset=utf-8"}, body)


def policy_with_calls(addresses: dict[str, str]) -> tuple[UrlSafetyPolicy, list[tuple[str, int]]]:
    calls: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> tuple[str]:
        calls.append((host, port))
        return (addresses[host],)

    return UrlSafetyPolicy(resolver=resolver), calls


def test_models_are_immutable() -> None:
    hop = RedirectHop("https://example.test/old", 301)
    snapshot = PageSnapshot(
        requested_url="https://example.test/old",
        final_url="https://example.test/new",
        status_code=200,
        headers={},
        encoding="utf-8",
        html="ok",
        method="GET",
        redirects=(hop,),
        retrieved_at=datetime.now().astimezone(),
    )

    with pytest.raises(FrozenInstanceError):
        hop.status_code = 302  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.html = "changed"  # type: ignore[misc]


def test_fetch_pins_transport_to_policy_ip_and_preserves_original_authority() -> None:
    policy, resolver_calls = policy_with_calls({"example.test": "93.184.216.34"})
    transport = FakeTransport([response()])

    snapshot = HttpPageAcquirer(transport, policy).fetch("https://example.test:8443/a?q=1")

    assert snapshot.final_url == "https://example.test:8443/a?q=1"
    assert resolver_calls == [("example.test", 8443)]
    assert transport.calls == [
        {
            "approved_ip": "93.184.216.34",
            "original_host": "example.test",
            "port": 8443,
            "scheme": "https",
            "path": "/a?q=1",
            "headers": {"Host": "example.test:8443", "User-Agent": "novel-crawler/0.1"},
            "timeout": 25,
        }
    ]


def test_default_port_host_header_omits_port() -> None:
    policy, _ = policy_with_calls({"example.test": "93.184.216.34"})
    transport = FakeTransport([response()])
    HttpPageAcquirer(transport, policy).fetch("http://example.test/")
    assert transport.calls[0]["headers"]["Host"] == "example.test"


def test_redirect_is_relative_revalidated_and_has_no_second_dns_lookup() -> None:
    policy, resolver_calls = policy_with_calls(
        {"first.test": "93.184.216.34", "next.test": "142.250.72.14"}
    )
    transport = FakeTransport(
        [response(302, headers={"Location": "https://next.test/final"}), response(body=b"done")]
    )

    snapshot = HttpPageAcquirer(transport, policy).fetch("https://first.test/start")

    assert resolver_calls == [("first.test", 443), ("next.test", 443)]
    assert [call["approved_ip"] for call in transport.calls] == ["93.184.216.34", "142.250.72.14"]
    assert [call["original_host"] for call in transport.calls] == ["first.test", "next.test"]
    assert snapshot.redirects == (RedirectHop("https://first.test/start", 302),)
    assert snapshot.final_url == "https://next.test/final"


def test_relative_redirect_is_joined_before_next_request() -> None:
    policy, calls = policy_with_calls({"example.test": "93.184.216.34"})
    transport = FakeTransport([response(303, headers={"Location": "../final?x=1"}), response()])
    snapshot = HttpPageAcquirer(transport, policy).fetch("https://example.test/a/start")
    assert calls == [("example.test", 443), ("example.test", 443)]
    assert snapshot.final_url == "https://example.test/final?x=1"
    assert transport.calls[1]["path"] == "/final?x=1"


def test_response_headers_are_filtered_case_insensitively_and_frozen() -> None:
    policy, _ = policy_with_calls({"example.test": "93.184.216.34"})
    transport = FakeTransport(
        [response(headers={"CONTENT-TYPE": "text/html", "ETag": "x", "Set-Cookie": "secret", "X-Key": "no"})]
    )
    snapshot = HttpPageAcquirer(transport, policy).fetch("https://example.test/")
    assert dict(snapshot.headers) == {"content-type": "text/html", "etag": "x"}
    with pytest.raises(TypeError):
        snapshot.headers["x"] = "y"  # type: ignore[index]


@pytest.mark.parametrize(
    ("content_type", "body", "expected_encoding", "expected"),
    [
        ("text/html; charset=gbk", "中文".encode("gbk"), "gbk", "中文"),
        ("text/html", "正文".encode(), "utf-8", "正文"),
        ("text/html", "章节".encode("gb18030"), "gb18030", "章节"),
    ],
)
def test_decodes_html(content_type: str, body: bytes, expected_encoding: str, expected: str) -> None:
    policy, _ = policy_with_calls({"example.test": "93.184.216.34"})
    snapshot = HttpPageAcquirer(
        FakeTransport([response(headers={"Content-Type": content_type}, body=body)]), policy
    ).fetch("https://example.test/")
    assert snapshot.encoding == expected_encoding
    assert snapshot.html == expected


def test_redirect_loop_and_limit_are_redacted() -> None:
    policy, _ = policy_with_calls({"example.test": "93.184.216.34"})
    loop = FakeTransport([response(301, headers={"Location": "/same?token=secret"})])
    with pytest.raises(AcquisitionError) as loop_error:
        HttpPageAcquirer(loop, policy).fetch("https://example.test/same?token=secret")
    assert loop_error.value.code == "redirect_loop"
    assert "token" not in str(loop_error.value)

    limited = FakeTransport([response(302, headers={"Location": "/b"}), response(302, headers={"Location": "/c"})])
    with pytest.raises(AcquisitionError) as limit_error:
        HttpPageAcquirer(limited, policy, max_redirects=1).fetch("https://example.test/a")
    assert limit_error.value.code == "too_many_redirects"


@pytest.mark.parametrize(
    ("status", "recoverable"), [(408, True), (429, True), (500, True), (503, True), (404, False), (403, False)]
)
def test_http_status_error_semantics(status: int, recoverable: bool) -> None:
    policy, _ = policy_with_calls({"example.test": "93.184.216.34"})
    with pytest.raises(AcquisitionError) as caught:
        HttpPageAcquirer(FakeTransport([response(status)]), policy).fetch("https://example.test/x?secret=1")
    assert caught.value.code == f"http_{status}"
    assert caught.value.recoverable is recoverable
    assert caught.value.safe_url == "https://example.test/x"


def test_timeout_is_recoverable_and_redacted() -> None:
    policy, _ = policy_with_calls({"example.test": "93.184.216.34"})
    with pytest.raises(AcquisitionError) as caught:
        HttpPageAcquirer(FakeTransport([TimeoutError("socket timeout")]), policy).fetch(
            "https://example.test/x?secret=1"
        )
    assert (caught.value.code, caught.value.recoverable) == ("timeout", True)
    assert "secret" not in str(caught.value)


def test_missing_redirect_location_is_terminal() -> None:
    policy, _ = policy_with_calls({"example.test": "93.184.216.34"})
    with pytest.raises(AcquisitionError) as caught:
        HttpPageAcquirer(FakeTransport([response(302)]), policy).fetch("https://example.test/")
    assert (caught.value.code, caught.value.recoverable) == ("redirect_missing_location", False)


def test_legacy_fetcher_opt_in_delegates_without_changing_text_api() -> None:
    class FakeAcquirer:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def fetch(self, url: str) -> PageSnapshot:
            self.urls.append(url)
            return PageSnapshot(
                requested_url=url,
                final_url=url,
                status_code=200,
                headers={},
                encoding="utf-8",
                html="正文",
                method="GET",
                redirects=(),
                retrieved_at=datetime.now().astimezone(),
            )

    acquirer = FakeAcquirer()
    fetcher = Fetcher(options=FetchOptions(retries=1), acquirer=acquirer)
    assert fetcher.fetch_text("https://example.test/chapter", referer="https://ignored.test/") == "正文"
    assert fetcher.fetch_bytes("https://example.test/raw") == "正文".encode()
    assert acquirer.urls == ["https://example.test/chapter", "https://example.test/raw"]
