from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict
from datetime import datetime
from typing import Any

import pytest
import urllib3

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
        body=b"ok",
        method="GET",
        redirects=(hop,),
        retrieved_at=datetime.now().astimezone(),
    )

    with pytest.raises(FrozenInstanceError):
        hop.status_code = 302  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.html = "changed"  # type: ignore[misc]


def test_models_enforce_redacted_url_storage() -> None:
    hop = RedirectHop("https://user:password@example.test/old?token=hop#fragment", 301)
    snapshot = PageSnapshot(
        requested_url="https://example.test/old?token=request",
        final_url="https://example.test/new?token=final#fragment",
        status_code=200, headers={}, encoding="utf-8", html="ok", body=b"ok", method="GET",
        redirects=(hop,), retrieved_at=datetime.now().astimezone(),
    )
    rendered = repr(snapshot) + repr(asdict(snapshot))
    assert hop.url == "https://example.test/old"
    assert snapshot.requested_url == "https://example.test/old"
    assert snapshot.final_url == "https://example.test/new"
    assert "token" not in rendered


def test_fetch_pins_transport_to_policy_ip_and_preserves_original_authority() -> None:
    policy, resolver_calls = policy_with_calls({"example.test": "93.184.216.34"})
    transport = FakeTransport([response()])

    snapshot = HttpPageAcquirer(transport, policy).fetch("https://example.test:8443/a?q=1")

    assert snapshot.final_url == "https://example.test:8443/a"
    assert resolver_calls == [("example.test", 8443)]
    call = transport.calls[0]
    assert {key: value for key, value in call.items() if key not in {"timeout", "max_body_bytes"}} == {
        "approved_ip": "93.184.216.34", "original_host": "example.test", "port": 8443,
        "scheme": "https", "path": "/a?q=1",
        "headers": {"Host": "example.test:8443", "User-Agent": "novel-crawler/0.1"},
    }
    assert 0 < call["timeout"] <= 25
    assert call["max_body_bytes"] == 10 * 1024 * 1024


def test_default_port_host_header_omits_port() -> None:
    policy, _ = policy_with_calls({"example.test": "93.184.216.34"})
    transport = FakeTransport([response()])
    HttpPageAcquirer(transport, policy).fetch("http://example.test/")
    assert transport.calls[0]["headers"]["Host"] == "example.test"


def test_fetch_tries_every_approved_ip_in_order_with_one_timeout_budget() -> None:
    resolver_calls: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> tuple[str, str]:
        resolver_calls.append((host, port))
        return ("2001:4860:4860::8888", "93.184.216.34")

    transport = FakeTransport([OSError("IPv6 unreachable"), response(body=b"fallback")])
    snapshot = HttpPageAcquirer(transport, UrlSafetyPolicy(resolver=resolver), timeout=7).fetch("https://example.test/")
    assert snapshot.body == b"fallback"
    assert resolver_calls == [("example.test", 443)]
    assert [call["approved_ip"] for call in transport.calls] == ["2001:4860:4860::8888", "93.184.216.34"]
    assert 0 < transport.calls[1]["timeout"] <= transport.calls[0]["timeout"] <= 7


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
    assert snapshot.final_url == "https://example.test/final"
    assert transport.calls[1]["path"] == "/final?x=1"


def test_snapshot_urls_never_retain_credentials_query_or_fragment() -> None:
    policy, _ = policy_with_calls({"example.test": "93.184.216.34"})
    transport = FakeTransport(
        [response(302, headers={"Location": "/final?next-secret=2#hidden"}), response()]
    )
    snapshot = HttpPageAcquirer(transport, policy).fetch(
        "https://example.test/start?request-secret=1#fragment"
    )

    rendered = repr(snapshot) + repr(asdict(snapshot))
    assert snapshot.requested_url == "https://example.test/start"
    assert snapshot.final_url == "https://example.test/final"
    assert snapshot.redirects == (RedirectHop("https://example.test/start", 302),)
    for secret in ("user", "password", "request-secret", "next-secret", "fragment", "hidden"):
        assert secret not in rendered


def test_content_length_and_actual_body_are_strictly_limited() -> None:
    policy, _ = policy_with_calls({"example.test": "93.184.216.34"})
    for candidate in (
        response(headers={"Content-Length": "6"}, body=b"small"),
        response(headers={"Content-Length": "not-a-number"}, body=b"123456"),
    ):
        with pytest.raises(AcquisitionError) as caught:
            HttpPageAcquirer(FakeTransport([candidate]), policy, max_body_bytes=5).fetch(
                "https://example.test/x?secret=1"
            )
        assert (caught.value.code, caught.value.recoverable) == ("response_too_large", False)
        assert "secret" not in str(caught.value)


def test_redirect_hops_and_ip_fallback_share_one_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter((0.0, 1.0, 4.0, 10.0))
    monkeypatch.setattr("novel_crawler.acquisition.http.time.monotonic", lambda: next(clock))
    policy, _ = policy_with_calls({"example.test": "93.184.216.34"})
    transport = FakeTransport(
        [response(302, headers={"Location": "/two"}), response(302, headers={"Location": "/three"})]
    )
    with pytest.raises(AcquisitionError) as caught:
        HttpPageAcquirer(transport, policy, timeout=9).fetch("https://example.test/one")
    assert caught.value.code == "timeout"
    assert [call["timeout"] for call in transport.calls] == [8.0, 5.0]


def test_ip_fallback_reports_timeout_when_shared_deadline_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter((0.0, 1.0, 6.0))
    monkeypatch.setattr("novel_crawler.acquisition.http.time.monotonic", lambda: next(clock))
    policy = UrlSafetyPolicy(resolver=lambda host, port: ("93.184.216.34", "142.250.72.14"))
    transport = FakeTransport([OSError("first IP unavailable")])
    with pytest.raises(AcquisitionError) as caught:
        HttpPageAcquirer(transport, policy, timeout=5).fetch("https://example.test/")
    assert caught.value.code == "timeout"
    assert len(transport.calls) == 1


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
        ("text/html", (text := "第一章开始阅读小说内容今天风和日丽主角来到城市展开一段全新的故事读者可以继续阅读下一页").encode("gb18030"), "gb18030", text),
        ("text/html", (text := "第一章開始閱讀小說內容今天風和日麗主角來到城市展開一段全新的故事讀者可以繼續閱讀下一頁").encode("big5"), "big5", text),
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


def test_transport_exception_cause_cannot_leak_sensitive_url() -> None:
    import traceback

    policy, _ = policy_with_calls({"example.test": "93.184.216.34"})
    error = RuntimeError("failed https://example.test/x?secret=raw-token")
    with pytest.raises(AcquisitionError) as caught:
        HttpPageAcquirer(FakeTransport([error]), policy).fetch("https://example.test/x?secret=request-token")
    rendered = "".join(traceback.format_exception_only(caught.value))
    assert "raw-token" not in rendered
    assert "request-token" not in rendered
    assert caught.value.__cause__ is None


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
                body=b"\xef\xbb\xbf\xffraw",
                method="GET",
                redirects=(),
                retrieved_at=datetime.now().astimezone(),
            )

    acquirer = FakeAcquirer()
    fetcher = Fetcher(options=FetchOptions(retries=1), acquirer=acquirer)
    assert fetcher.fetch_text("https://example.test/chapter", referer="https://ignored.test/") == "正文"
    assert fetcher.fetch_bytes("https://example.test/raw") == b"\xef\xbb\xbf\xffraw"
    assert acquirer.urls == ["https://example.test/chapter", "https://example.test/raw"]


def test_default_transport_pins_http_and_https_pool_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[str, str, int, dict[str, Any]]] = []
    responses: list[Any] = []

    class Pool:
        def __init__(self, kind: str, host: str, port: int, **kwargs: Any) -> None:
            created.append((kind, host, port, kwargs))

        def urlopen(self, *args: Any, **kwargs: Any) -> Any:
            class Response:
                status = 200
                headers: dict[str, str] = {}

                def stream(self, amount: int, decode_content: bool) -> Any:
                    assert decode_content is True
                    yield b"ok"

                def release_conn(self) -> None:
                    self.released = True

                def close(self) -> None:
                    self.closed = True

            response = Response()
            responses.append(response)
            return response

        def close(self) -> None:
            pass

    monkeypatch.setattr(urllib3, "HTTPConnectionPool", lambda host, port, **kw: Pool("http", host, port, **kw))
    monkeypatch.setattr(urllib3, "HTTPSConnectionPool", lambda host, port, **kw: Pool("https", host, port, **kw))
    from novel_crawler.acquisition.http import Urllib3PinnedTransport

    transport = Urllib3PinnedTransport()
    transport.request(
        approved_ip="2001:4860:4860::8888", original_host="example.test", port=8443, scheme="https",
        path="/", headers={"Host": "example.test:8443"}, timeout=2, max_body_bytes=10,
    )
    transport.request(
        approved_ip="93.184.216.34", original_host="example.test", port=80, scheme="http",
        path="/", headers={"Host": "example.test"}, timeout=2, max_body_bytes=10,
    )
    assert created == [
        ("https", "2001:4860:4860::8888", 8443, {"assert_hostname": "example.test", "server_hostname": "example.test"}),
        ("http", "93.184.216.34", 80, {}),
    ]
    assert all(response.released and response.closed for response in responses)


def test_default_transport_streams_decoded_body_and_never_reads_redirect_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_calls: list[tuple[int, bool]] = []
    urlopen_calls: list[dict[str, Any]] = []

    class Response:
        def __init__(self, status: int, chunks: tuple[bytes, ...], headers: dict[str, str]) -> None:
            self.status = status
            self.headers = headers
            self.chunks = chunks
            self.released = False
            self.closed = False

        def stream(self, amount: int, decode_content: bool) -> Any:
            stream_calls.append((amount, decode_content))
            yield from self.chunks

        def release_conn(self) -> None:
            self.released = True

        def close(self) -> None:
            self.closed = True

    queued = [
        Response(302, (b"must-not-read",), {"Location": "/next"}),
        Response(200, (b"123", b"456"), {"Content-Encoding": "gzip"}),
    ]

    class Pool:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def urlopen(self, *args: Any, **kwargs: Any) -> Response:
            urlopen_calls.append(kwargs)
            return queued.pop(0)

        def close(self) -> None:
            pass

    monkeypatch.setattr(urllib3, "HTTPSConnectionPool", Pool)
    from novel_crawler.acquisition.http import Urllib3PinnedTransport

    transport = Urllib3PinnedTransport()
    redirect = transport.request(
        approved_ip="93.184.216.34", original_host="example.test", port=443, scheme="https",
        path="/", headers={}, timeout=2, max_body_bytes=5,
    )
    assert redirect.body == b""
    with pytest.raises(AcquisitionError) as caught:
        transport.request(
            approved_ip="93.184.216.34", original_host="example.test", port=443, scheme="https",
            path="/next", headers={}, timeout=2, max_body_bytes=5,
        )
    assert caught.value.code == "response_too_large"
    assert stream_calls == [(64 * 1024, True)]
    assert all(call["preload_content"] is False for call in urlopen_calls)


def test_default_transport_rejects_large_content_length_before_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamed = False

    class Response:
        status = 200
        headers = {"Content-Length": "6"}

        def stream(self, amount: int, decode_content: bool) -> Any:
            nonlocal streamed
            streamed = True
            yield b"123456"

        def release_conn(self) -> None:
            pass

        def close(self) -> None:
            pass

    class Pool:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def urlopen(self, *args: Any, **kwargs: Any) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(urllib3, "HTTPSConnectionPool", Pool)
    from novel_crawler.acquisition.http import Urllib3PinnedTransport

    with pytest.raises(AcquisitionError) as caught:
        Urllib3PinnedTransport().request(
            approved_ip="93.184.216.34", original_host="example.test", port=443, scheme="https",
            path="/", headers={}, timeout=2, max_body_bytes=5,
        )
    assert caught.value.code == "response_too_large"
    assert streamed is False


def test_ipv6_host_header_is_bracketed() -> None:
    transport = FakeTransport([response()])
    HttpPageAcquirer(transport, UrlSafetyPolicy()).fetch("https://[2001:4860:4860::8888]:8443/")
    assert transport.calls[0]["headers"]["Host"] == "[2001:4860:4860::8888]:8443"
