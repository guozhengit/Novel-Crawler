from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from novel_crawler.acquisition.models import AcquiredPage
from novel_crawler.acquisition.security import UrlSafetyPolicy
from novel_crawler.adaptation.service import ProbeService
from novel_crawler.browser.driver import BrowserRequestPolicy, DefaultPlaywrightDriver
from novel_crawler.browser.sessions import BrowserSessionStore

PLAYWRIGHT_AVAILABLE = importlib.util.find_spec("playwright") is not None
if PLAYWRIGHT_AVAILABLE:
    from playwright.sync_api import Error as PlaywrightError
else:
    PlaywrightError = Exception

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PLAYWRIGHT_AVAILABLE, reason="legacy Playwright runtime is not installed"),
    pytest.mark.skipif(
        os.environ.get("RUN_PLAYWRIGHT_INTEGRATION") != "1",
        reason="set RUN_PLAYWRIGHT_INTEGRATION=1; the compatible Chromium binary is mandatory",
    ),
]


class _CountingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, responses: dict[str, tuple[bytes, dict[str, str], float]]) -> None:
        self.responses = responses
        self.hits: list[str] = []
        self.accepts = 0
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)

    def get_request(self):
        request = super().get_request()
        request[0].settimeout(1)
        with self._lock:
            self.accepts += 1
        return request


class _Handler(BaseHTTPRequestHandler):
    server: _CountingServer

    def do_GET(self) -> None:
        with self.server._lock:
            self.server.hits.append(self.path)
        body, headers, delay = self.server.responses.get(
            self.path,
            (b"not found", {"Content-Type": "text/plain"}, 0),
        )
        if delay:
            time.sleep(delay)
        self.send_response(200 if self.path in self.server.responses else 404)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, _format: str, *args: object) -> None:
        del args


class _RunningServer:
    def __init__(self, responses: dict[str, tuple[bytes, dict[str, str], float]]) -> None:
        self.server = _CountingServer(responses)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _CountingServer:
        self.thread.start()
        return self.server

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


def _loopback_policy(resolver) -> UrlSafetyPolicy:
    policy = UrlSafetyPolicy(resolver=resolver)
    policy._require_public = lambda address, safe_url: None  # type: ignore[method-assign]
    return policy


def _guard(url: str, resolver=lambda host, port: ("127.0.0.1",)) -> BrowserRequestPolicy:
    guard = BrowserRequestPolicy(_loopback_policy(resolver))
    guard.lock(url)
    return guard


def _launch(tmp_path: Path, url: str, *, resolver=lambda host, port: ("127.0.0.1",), **limits):
    limits.setdefault("operation_timeout", 5)
    return DefaultPlaywrightDriver(**limits).launch(
        user_data_dir=tmp_path / "profile",
        headless=True,
        policy=_guard(url, resolver),
    )


class _ChromiumAcquirer:
    def __init__(self, context: Any) -> None:
        self.context = context
        self.calls: list[str] = []

    def fetch_page(
        self,
        url: str,
        *,
        max_body_bytes: int | None = None,
        locked_origin: str | None = None,
    ) -> AcquiredPage:
        del locked_origin
        self.calls.append(url)
        snapshot = self.context.navigate(url)
        return snapshot.to_acquired_page(max_body_bytes=max_body_bytes or 20 * 1024)


def _javascript_site_responses(cases: list[dict[str, object]]) -> Iterator[tuple[str, tuple[bytes, dict[str, str], float]]]:
    headers = {"Content-Type": "text/html; charset=utf-8"}
    for case in cases:
        case_id = str(case["id"])
        wrapper = str(case["wrapper"])
        content_tag = str(case["content"])
        paragraph_count = int(case["paragraphs"])
        prefix = f"/{case_id}"
        book = f"Fixture Book {case_id}"
        index_script = f"""<!doctype html><body><script>
const meta=document.createElement('meta');meta.name='book_name';meta.content='{book}';document.head.append(meta);
const title=document.createElement('h1');title.className='book-title';title.textContent='{book}';document.body.append(title);
const list=document.createElement('div');list.id='list';list.className='{wrapper}';document.body.append(list);
for(let n=1;n<=3;n++){{const link=document.createElement('a');link.href='{prefix}/chapter-'+n;link.textContent='Chapter '+n;list.append(link);}}
</script>"""
        yield f"{prefix}/book", (index_script.encode(), headers, 0)
        for chapter in (1, 2):
            next_script = ""
            if chapter == 1:
                next_script = (
                    f"const next=document.createElement('a');next.rel='next';"
                    f"next.href='{prefix}/chapter-2';next.textContent='Next';document.body.append(next);"
                )
            chapter_script = f"""<!doctype html><body><script>
const meta=document.createElement('meta');meta.name='book_name';meta.content='{book}';document.head.append(meta);
document.body.className='{wrapper}';
const title=document.createElement('h1');title.textContent='Chapter {chapter}';document.body.append(title);
const content=document.createElement('{content_tag}');content.className='chapter-content';document.body.append(content);
for(let n=1;n<={paragraph_count};n++){{const p=document.createElement('p');p.textContent='{case_id} chapter {chapter} paragraph '+n+' '+('content '.repeat(18));content.append(p);}}
{next_script}
</script>"""
            yield f"{prefix}/chapter-{chapter}", (chapter_script.encode(), headers, 0)


def test_real_chromium_javascript_adaptation_benchmark(tmp_path: Path) -> None:
    fixture_dir = Path(__file__).parents[1] / "release" / "fixtures"
    manifest = json.loads((fixture_dir / "benchmark_manifest.json").read_text(encoding="utf-8"))
    assert manifest["license"] == "CC0-1.0"
    fixture_path = fixture_dir / "adaptation_cases.json"
    cases = [case for case in json.loads(fixture_path.read_text(encoding="utf-8")) if case["kind"] == "javascript"]
    assert len(cases) >= 10
    responses = dict(_javascript_site_responses(cases))
    raw_bodies = [body.decode() for body, _headers, _delay in responses.values()]
    assert len(responses) == len(cases) * 3
    assert all(marker not in body for body in raw_bodies for marker in ("<h1", "<a", "<article", "<p"))

    with _RunningServer(responses) as target:
        origin = f"http://example.test:{target.server_address[1]}"
        context = _launch(tmp_path, f"{origin}/{cases[0]['id']}/book", operation_timeout=60)
        acquirer = _ChromiumAcquirer(context)
        try:
            runs = []
            for _ in range(2):
                outcomes = [ProbeService(acquirer=acquirer).probe(f"{origin}/{case['id']}/book") for case in cases]
                runs.append([(outcome.ok, outcome.reason_ids or ("ok",)) for outcome in outcomes])
        finally:
            context.close()

    success_rate = sum(ok for ok, _reasons in runs[0]) / len(runs[0])
    assert success_rate >= 0.70, runs[0]
    assert runs[0] == runs[1], "outcomes and stable reason codes must repeat under real Chromium"
    assert all(reasons == ("ok",) for ok, reasons in runs[0] if ok)
    assert len(acquirer.calls) == len(cases) * 3 * 2
    assert set(target.hits) == set(responses)


def test_real_chromium_routes_all_network_and_blocks_active_escape_surfaces(tmp_path: Path) -> None:
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(0.4)
    udp_port = udp.getsockname()[1]
    resolver_calls: list[tuple[str, int]] = []

    def resolver(host: str, port: int):
        resolver_calls.append((host, port))
        return ("127.0.0.1",)

    with _RunningServer({}) as cross:
        cross_port = cross.server_address[1]
        html = b"<!doctype html><a id=download download href='/download'>download</a><p>ready</p>"
        responses = {
            "/": (html, {"Content-Type": "text/html; charset=utf-8"}, 0),
            "/same": (b"ok", {"Content-Type": "text/plain"}, 0),
            "/sw.js": (b"self.addEventListener('fetch',()=>{})", {"Content-Type": "text/javascript"}, 0),
            "/download": (
                b"private download",
                {"Content-Type": "application/octet-stream", "Content-Disposition": "attachment; filename=private.bin"},
                0,
            ),
        }
        with _RunningServer(responses) as target:
            port = target.server_address[1]
            url = f"http://example.test:{port}/"
            context = _launch(tmp_path, url, resolver=resolver, operation_timeout=5)
            try:
                snapshot = context.navigate(url)
                assert b"ready" in snapshot.body
                context._page.evaluate(  # type: ignore[attr-defined]
                    """port => {
                      const link=document.createElement('link'); link.rel='preconnect';
                      link.href=`http://127.0.0.1:${port}`; document.head.append(link);
                    }""",
                    cross_port,
                )
                results = context._page.evaluate(  # type: ignore[attr-defined]
                    """async ({crossPort}) => {
                      const result={};
                      await Promise.all([
                        fetch('/same').then(()=>result.same=true).catch(()=>result.same=false),
                        fetch(`http://example.test:${crossPort}/cross`).then(()=>result.cross=true).catch(()=>result.cross=false),
                        fetch(`http://127.0.0.1:${crossPort}/private`).then(()=>result.private=true).catch(()=>result.private=false),
                        fetch(`http://10.0.0.1:${crossPort}/lan`).then(()=>result.lan=true).catch(()=>result.lan=false)
                      ]);
                      return result;
                    }""",
                    {"crossPort": cross_port},
                )
                context._page.evaluate(  # type: ignore[attr-defined]
                    """({udpPort}) => {
                      try { window.testSocket=new WebSocket('ws://' + location.host + '/ws'); } catch (_) {}
                      if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{});
                      if ('RTCPeerConnection' in window) {
                        window.testPeer=new RTCPeerConnection({iceServers:[{urls:`stun:127.0.0.1:${udpPort}`}]});
                        testPeer.createDataChannel('blocked');
                        testPeer.createOffer().then(x=>testPeer.setLocalDescription(x)).catch(()=>{});
                      }
                    }""",
                    {"udpPort": udp_port},
                )
                context._page.wait_for_timeout(800)  # type: ignore[attr-defined]
                assert results == {"same": True, "cross": False, "private": False, "lan": False}
                downloads = []
                context._page.on("download", lambda item: downloads.append(item))  # type: ignore[attr-defined]
                context._page.evaluate("document.querySelector('#download').click()")  # type: ignore[attr-defined]
                context._page.wait_for_timeout(300)  # type: ignore[attr-defined]
                assert downloads
                assert downloads[0].suggested_filename == "private.bin"
                context._page.evaluate(  # type: ignore[attr-defined]
                    "if (window.testSocket) testSocket.close(); if (window.testPeer) testPeer.close();"
                )
            finally:
                context.close()
        assert resolver_calls == [("example.test", port)]
        assert "/" in target.hits and "/same" in target.hits
        assert "/sw.js" not in target.hits and "/ws" not in target.hits
        assert cross.accepts == 0 and cross.hits == []
        assert not list((tmp_path / "profile").rglob("private.bin"))
        with pytest.raises(TimeoutError):
            udp.recvfrom(1024)
    udp.close()


@pytest.mark.parametrize(
    ("driver_options", "body", "delay", "error"),
    [
        ({"max_body_bytes": 512}, b"<p>" + b"x" * 4096 + b"</p>", 0, "browser_body_too_large"),
        ({"max_network_bytes": 512}, b"<p>" + b"x" * 4096 + b"</p>", 0, "network_cap"),
        ({"operation_timeout": 2.0}, b"<p>slow</p>", 4.0, "deadline"),
    ],
    ids=("body", "network", "deadline"),
)
def test_real_chromium_enforces_body_network_and_deadline_limits(
    tmp_path: Path,
    driver_options: dict[str, object],
    body: bytes,
    delay: float,
    error: str | None,
) -> None:
    with _RunningServer({"/": (body, {"Content-Type": "text/html"}, delay)}) as target:
        url = f"http://example.test:{target.server_address[1]}/"
        context = _launch(tmp_path, url, **driver_options)
        started = time.monotonic()
        try:
            if error == "browser_body_too_large":
                with pytest.raises(ValueError, match=error):
                    context.navigate(url)
            elif error == "network_cap":
                context.navigate(url)
            else:
                with pytest.raises(PlaywrightError):
                    context.navigate(url)
            if "max_network_bytes" in driver_options:
                assert context._proxy.network_bytes <= driver_options["max_network_bytes"]  # type: ignore[attr-defined,operator]
            if "operation_timeout" in driver_options:
                assert time.monotonic() - started < 2
        finally:
            context.close()


def _client_hello_sni(data: bytes) -> str | None:
    if len(data) < 9 or data[0] != 22 or data[5] != 1:
        return None
    pos = 9 + 2 + 32
    if pos >= len(data):
        return None
    pos += 1 + data[pos]
    cipher_length = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2 + cipher_length
    pos += 1 + data[pos]
    extension_length = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2
    end = min(len(data), pos + extension_length)
    while pos + 4 <= end:
        kind = int.from_bytes(data[pos : pos + 2], "big")
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        value = data[pos + 4 : pos + 4 + length]
        if kind == 0 and len(value) >= 5:
            name_length = int.from_bytes(value[3:5], "big")
            return value[5 : 5 + name_length].decode("ascii")
        pos += 4 + length
    return None


def test_real_chromium_tls_uses_locked_hostname_for_sni_without_second_dns_resolution(tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(3)
    port = listener.getsockname()[1]
    observed: list[str | None] = []

    def capture_hello() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(2)
            observed.append(_client_hello_sni(connection.recv(8192)))

    thread = threading.Thread(target=capture_hello, daemon=True)
    thread.start()
    calls = 0

    def resolver(_host: str, _port: int):
        nonlocal calls
        calls += 1
        return ("127.0.0.1",) if calls == 1 else ("10.0.0.1",)

    url = f"https://example.test:{port}/"
    context = _launch(tmp_path, url, resolver=resolver)
    try:
        with pytest.raises(PlaywrightError):
            context.navigate(url)
    finally:
        context.close()
        listener.close()
        thread.join(3)
    assert calls == 1
    assert observed == ["example.test"]


def test_real_chromium_profile_closes_then_corrupt_metadata_is_quarantined_and_cleaned(tmp_path: Path) -> None:
    with _RunningServer({"/": (b"<p>profile</p>", {"Content-Type": "text/html"}, 0)}) as target:
        url = f"http://example.test:{target.server_address[1]}/"
        store = BrowserSessionStore(tmp_path / "sessions")
        with store.acquire("example.test") as lease:
            context = DefaultPlaywrightDriver(operation_timeout=5).launch(
                user_data_dir=lease.profile_path,
                headless=True,
                policy=_guard(url),
            )
            context.navigate(url)
            context.close()
            sentinel = lease.profile_path / "old-private-state"
            sentinel.write_bytes(b"private")
            session_id = lease.info.session_id
        metadata = store._paths("example.test")[2]
        metadata.write_bytes(b"{corrupt")
        with store.acquire("example.test") as replacement:
            assert replacement.info.session_id != session_id
            assert not (replacement.profile_path / "old-private-state").exists()
            replacement_id = replacement.info.session_id
            replacement_path = replacement.profile_path
        assert list((store.root / "quarantine").glob("*.bad"))
        assert store.clear("example.test", replacement_id, confirmation=True)
        assert not replacement_path.exists()
        assert not list((store.root / "trash").iterdir())
        assert not list((store.root / "tombstones").iterdir())
        shutil.rmtree(tmp_path / "profile", ignore_errors=True)


@pytest.mark.skip(
    reason=(
        "the local stdlib fixtures have no HTTP/3 server, so QUIC negotiation is not objectively observable; "
        "tests/browser/test_driver.py enforces the mandatory --disable-quic Chromium launch gate"
    )
)
def test_real_chromium_quic_negotiation_requires_local_http3_fixture() -> None:
    """Document the one protocol check this local-only harness cannot honestly perform."""
