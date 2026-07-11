from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from novel_crawler.acquisition.classifier import Classification, PageClassifier, PageKind
from novel_crawler.acquisition.http import AcquisitionError, HttpPageAcquirer, TransportResponse
from novel_crawler.acquisition.models import PageSnapshot
from novel_crawler.acquisition.security import UrlSafetyPolicy

PAGES: dict[str, tuple[int, str, bytes]] = {
    "/book": (200, "text/html; charset=utf-8", """
        <html><head><title>星海纪元 - 章节目录</title></head><body>
        <h1>星海纪元</h1><div id='list'>
        <a href='/1'>第一章 启程</a><a href='/2.html'>第二章 风暴</a>
        <a href='/read?cid=3'>第三章 星门</a><a href='/?chapter_id=4'>第四章 归途</a>
        </div><form class='login-widget'><input type='password'></form></body></html>""".encode()),
    "/chapter/1": (200, "text/html; charset=utf-8", """
        <html><head><title>第一章 启程</title></head><body><h1>第一章 启程</h1>
        <article id='content'><p>夜色落在港口，旅人终于登上远航的飞船。</p>
        <p>这是足够长的小说正文，用来确认页面包含连续的叙事内容。</p></article>
        <a rel='next' href='/chapter/2'>下一章</a></body></html>""".encode()),
    "/chapter/2": (200, "text/html; charset=utf-8", """
        <html><head><title>第二章 风暴</title></head><body><h1>第二章 风暴</h1>
        <div class='chapter-content'>风暴席卷甲板，船员们在呼啸声中继续前进。这是一段小说正文内容。</div>
        <a href='/chapter/1'>上一章</a></body></html>""".encode()),
    "/login": (200, "text/html; charset=utf-8", b"<title>Login</title><form><input type='password'></form>"),
    "/challenge": (200, "text/html; charset=utf-8", b"<title>Just a moment...</title><form action='/captcha'><input name='captcha'></form><p>Verify you are human</p>"),
    "/search": (200, "text/html; charset=utf-8", "<title>搜索结果</title><form role='search'></form><a href='/book'>星海纪元</a>".encode()),
    "/noise": (200, "text/html; charset=utf-8", """
        <title>社区动态</title><section class='comments'><h2>评论</h2>
        <a href='/chapter/1'>第一章真好看</a><a href='/chapter/2'>第二章也不错</a></section>
        <aside class='recommendations'><a href='/book/a'>推荐小说一</a><a href='/book/b'>推荐小说二</a></aside>""".encode()),
    "/error": (500, "text/html; charset=utf-8", b"<title>Novel chapter 1</title><article>content</article>"),
    "/gbk": (200, "text/html; charset=gbk", "<title>第三章 归来</title><h1>第三章 归来</h1><div id='content'>这是采用国标编码的小说正文，主人公终于回到故乡。</div>".encode("gbk")),
}


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "chapter/1")
            self.end_headers()
            return
        status, content_type, body = PAGES.get(self.path, (404, "text/plain", b"missing"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class LocalFixtureTransport:
    """Injected integration adapter; maps an approved public IP to a local fixture only in tests."""

    def __init__(self, fixture_port: int) -> None:
        self.fixture_port = fixture_port

    def request(
        self, *, approved_ip: str, original_host: str, port: int, scheme: str, path: str,
        headers: Mapping[str, str], timeout: float, max_body_bytes: int,
    ) -> TransportResponse:
        assert approved_ip == "93.184.216.34"
        assert original_host == "fixture.example"
        assert scheme == "http"
        connection = http.client.HTTPConnection("127.0.0.1", self.fixture_port, timeout=timeout)
        try:
            connection.request("GET", path, headers=dict(headers))
            response = connection.getresponse()
            return TransportResponse(response.status, dict(response.getheaders()), response.read(max_body_bytes + 1))
        finally:
            connection.close()


@contextmanager
def fixture_acquirer() -> Iterator[HttpPageAcquirer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        policy = UrlSafetyPolicy(resolver=lambda host, port: ("93.184.216.34",))
        yield HttpPageAcquirer(LocalFixtureTransport(server.server_port), policy)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def snapshot(status: int, html: str, url: str = "https://example.test/page") -> PageSnapshot:
    return PageSnapshot(url, url, status, {}, "utf-8", html, html.encode(), "GET", (), datetime.now(UTC))


def test_classification_is_frozen_bounded_and_uses_stable_evidence_ids() -> None:
    result = PageClassifier().classify(snapshot(200, "<title>Login</title><form><input type='password'></form>"))
    assert result == Classification(PageKind.AUTH_OR_CHALLENGE, result.confidence, ("auth.password_input",), result.sample_id, result.safe_origin)
    assert 0 <= result.confidence <= 1
    assert all("Login" not in item for item in result.evidence)
    with pytest.raises(FrozenInstanceError):
        result.confidence = 0  # type: ignore[misc]


def test_error_and_auth_precede_content_signals() -> None:
    classifier = PageClassifier()
    error = classifier.classify(snapshot(503, "<article id='content'>第一章 正文</article>"))
    auth = classifier.classify(snapshot(
        200,
        "<title>Verify human</title><form action='/captcha'><input name='captcha'></form><article>正文</article>",
    ))
    assert error.kind is PageKind.ERROR
    assert error.evidence == ("error.http_status",)
    assert auth.kind is PageKind.AUTH_OR_CHALLENGE


def test_actual_http_error_precedes_auth_but_soft_error_title_does_not() -> None:
    html = "<title>404 Not Found - Login</title><form><input type='password'></form>"
    classifier = PageClassifier()
    assert classifier.classify(snapshot(404, html)).kind is PageKind.ERROR
    result = classifier.classify(snapshot(200, html))
    assert result.kind is PageKind.AUTH_OR_CHALLENGE
    assert result.evidence == ("auth.password_input",)


def test_primary_login_page_wins_over_article_and_recommended_chapters() -> None:
    html = """<title>用户登录</title><h1>登录</h1>
    <form><input type='password'></form><article>欢迎回来，请登录后继续阅读。</article>
    <section class='related'><a href='/1'>第一章</a><a href='/2'>第二章</a><a href='/3'>第三章</a></section>"""
    assert PageClassifier().classify(snapshot(200, html)).kind is PageKind.AUTH_OR_CHALLENGE


def test_login_widget_and_reading_challenge_class_do_not_override_content() -> None:
    chapter = snapshot(
        200,
        """<title>第八章 夜航</title><article class='reading-challenge'>
        夜色落在群山之间，主人公继续赶路。这是明确且充分的章节正文内容。
        </article><form class='login-widget'><input type='password'></form>""",
        "https://example.test/8.html",
    )
    assert PageClassifier().classify(chapter).kind is PageKind.CHAPTER


def test_lone_challenge_class_and_unrelated_password_field_are_unknown() -> None:
    classifier = PageClassifier()
    assert classifier.classify(snapshot(200, "<div class='reading-challenge'>Weekly reading challenge</div>")).kind is PageKind.UNKNOWN
    assert classifier.classify(snapshot(200, "<input type='password'>")).kind is PageKind.UNKNOWN


def test_chinese_text_and_numeric_or_query_urls_form_book_index_cluster() -> None:
    html = """<title>山海录</title><main>
      <a href='/1001'>第一章 入山</a><a href='/1002.html'>第二章 问路</a>
      <a href='/read?cid=1003'>第三章 古庙</a><a href='/?chapter_id=1004'>第四章 夜雨</a>
    </main>"""
    result = PageClassifier().classify(snapshot(200, html, "https://example.test/book/42"))
    assert result.kind is PageKind.BOOK_INDEX
    assert result.evidence == ("book_index.chapter_link_cluster",)


def test_numeric_urls_and_chapter_query_are_never_standalone_content_signals() -> None:
    classifier = PageClassifier()
    pagination = "<title>Company</title><nav><a href='/1'>1</a><a href='/2'>2</a><a href='/3'>3</a></nav>"
    news = "<title>Company announcement</title><article>Quarterly company news and ordinary editorial copy.</article>"
    query = "<title>Product details</title><article>Ordinary product information with enough descriptive copy.</article>"
    assert classifier.classify(snapshot(200, pagination)).kind is PageKind.UNKNOWN
    assert classifier.classify(snapshot(200, news, "https://example.test/2026/07/11")).kind is PageKind.UNKNOWN
    assert classifier.classify(snapshot(200, query, "https://example.test/view?cid=123")).kind is PageKind.UNKNOWN


def test_search_page_with_numeric_pagination_remains_search() -> None:
    html = "<title>搜索结果</title><nav><a href='/1'>1</a><a href='/2'>2</a><a href='/3'>3</a></nav>"
    assert PageClassifier().classify(snapshot(200, html)).kind is PageKind.SEARCH_OR_LIST


def test_fixture_pages_classify_and_acquisition_handles_redirect_and_gbk() -> None:
    expected = {
        "/book": PageKind.BOOK_INDEX,
        "/chapter/1": PageKind.CHAPTER,
        "/chapter/2": PageKind.CHAPTER,
        "/login": PageKind.AUTH_OR_CHALLENGE,
        "/challenge": PageKind.AUTH_OR_CHALLENGE,
        "/search": PageKind.SEARCH_OR_LIST,
        "/noise": PageKind.UNKNOWN,
        "/gbk": PageKind.CHAPTER,
    }
    classifier = PageClassifier()
    with fixture_acquirer() as acquirer:
        for path, kind in expected.items():
            acquired = acquirer.fetch(f"http://fixture.example:{acquirer.transport.fixture_port}{path}")  # type: ignore[attr-defined]
            result = classifier.classify(acquired)
            assert result.kind is kind, (path, result)
            assert 0 <= result.confidence <= 1
        redirected = acquirer.fetch(f"http://fixture.example:{acquirer.transport.fixture_port}/redirect")  # type: ignore[attr-defined]
        assert redirected.final_url == f"http://fixture.example:{acquirer.transport.fixture_port}/"  # type: ignore[attr-defined]
        assert classifier.classify(redirected).kind is PageKind.CHAPTER
        gbk = acquirer.fetch(f"http://fixture.example:{acquirer.transport.fixture_port}/gbk")  # type: ignore[attr-defined]
        assert gbk.encoding == "gbk"
        assert "第三章" in gbk.html
        with pytest.raises(AcquisitionError) as caught:
            acquirer.fetch(f"http://fixture.example:{acquirer.transport.fixture_port}/error")  # type: ignore[attr-defined]
        assert caught.value.code == "http_500"


def test_blank_and_unrelated_pages_are_unknown() -> None:
    classifier = PageClassifier()
    for html in ("", "<title>Company home</title><nav>About Products Contact</nav>"):
        assert classifier.classify(snapshot(200, html)).kind is PageKind.UNKNOWN
