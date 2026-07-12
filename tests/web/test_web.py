from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler

import pytest

from novel_crawler.application import ApplicationError
from novel_crawler.web import create_web_server
from novel_crawler.web import server as web_server


@dataclass
class View:
    value: dict[str, object]

    def to_safe_dict(self) -> dict[str, object]:
        return dict(self.value)


class FakeApplication:
    def __init__(self) -> None:
        self.closed = 0
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.task = View({
            "task_id": "task-1",
            "status": "created",
            "version": 0,
            "terminal": False,
            "interaction": {
                "verification_required": True,
                "confirmation_required": True,
                "cleanup_required": True,
                "safe_origin": "https://example.test:8443/",
            },
        })

    def _call(self, name: str, *args: object):
        self.calls.append((name, args))
        return self.task

    def create_crawl_task(self, url, options=None): return self._call("create", url, options)
    def list_tasks(self, **kwargs):
        self._call("list_tasks", kwargs)
        return [self.task]
    def get_task(self, task_id): return self._call("get_task", task_id)
    def task_events(self, task_id):
        self._call("events", task_id)
        return [View({"event_id": 1, "task_id": task_id, "to_status": "created"})]
    def pause_task(self, task_id): return self._call("pause", task_id)
    def resume_task(self, task_id): return self._call("resume", task_id)
    def cancel_task(self, task_id): return self._call("cancel", task_id)
    def continue_interaction(self, task_id): return self._call("continue", task_id)
    def confirm_interaction(self, task_id, overrides=None): return self._call("confirm", task_id, overrides)
    def retry_cleanup(self, task_id): return self._call("cleanup", task_id)
    def list_books(self):
        self._call("books")
        return [{"id": 1, "title": "安全书名", "site": "fixture", "total": 2, "done": 1}]
    def book_report(self, book_id):
        self._call("report", book_id)
        return "进度正常"
    def export_book(self, book_id, fmt="txt"):
        self._call("export", book_id, fmt)
        return {"completed": True, "format": fmt}
    def delete_book(self, book_id):
        self._call("delete", book_id)
        return {"state": "completed", "completed": True}
    def close(self):
        self.closed += 1
        return True


class Client:
    def __init__(self, server) -> None:
        self.server = server
        self.cookie = ""
        self.csrf = ""
        self.host = f"127.0.0.1:{server.server_address[1]}"

    def request(self, method: str, path: str, body: object | None = None, *, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        raw = b"" if body is None else json.dumps(body).encode()
        values = {"Host": self.host, **(headers or {})}
        if self.cookie:
            values["Cookie"] = self.cookie
        if body is not None:
            values.setdefault("Content-Type", "application/json")
            values.setdefault("Content-Length", str(len(raw)))
        connection.request(method, path, body=raw, headers=values)
        response = connection.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        connection.close()
        return result

    def login(self) -> None:
        status, headers, body = self.request("GET", "/")
        assert status == 200
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        match = re.search(rb'<meta name="csrf-token" content="([A-Za-z0-9_-]+)">', body)
        assert match
        self.csrf = match.group(1).decode()

    def post(self, path: str, body: object):
        return self.request(
            "POST",
            path,
            body,
            headers={"Origin": f"http://{self.host}", "X-CSRF-Token": self.csrf},
        )


@pytest.fixture
def web_app():
    app = FakeApplication()
    server = create_web_server(app=app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, server, Client(server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def decoded(result):
    status, headers, body = result
    return status, headers, json.loads(body)


def raw_request(server, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=3) as connection:
        connection.sendall(request)
        chunks = []
        while chunk := connection.recv(4096):
            chunks.append(chunk)
    return b"".join(chunks)


def raw_authenticated(client: Client, headers: str, body: bytes = b"") -> bytes:
    request = (
        "POST /api/tasks HTTP/1.1\r\n"
        f"Host: {client.host}\r\nCookie: {client.cookie}\r\nOrigin: http://{client.host}\r\n"
        f"X-CSRF-Token: {client.csrf}\r\nContent-Type: application/json\r\n{headers}\r\n"
    ).encode() + body
    return raw_request(client.server, request)


def test_non_loopback_binding_requires_explicit_unsafe_opt_in() -> None:
    with pytest.raises(ValueError, match="unsafe_non_loopback"):
        create_web_server(app=FakeApplication(), host="0.0.0.0", port=0)
    server = create_web_server(app=FakeApplication(), host="0.0.0.0", port=0, unsafe_non_loopback=True)
    assert f"127.0.0.1:{server.server_address[1]}" in server.allowed_hosts
    server.server_close()


def test_root_creates_hardened_session_and_safe_dom_ui(web_app) -> None:
    _, _, client = web_app
    status, headers, body = client.request("GET", "/")
    assert status == 200
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert headers["Cache-Control"] == "no-store"
    assert headers["Connection"] == "close"
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert b"innerHTML" not in body
    page = body.decode()
    assert "小说抓取任务" in page
    assert all(label in page for label in ("继续验证", "确认配置", "重试清理", "safe_origin"))
    assert all(label in page for label in ("确认删除", "取消删除", "书籍已删除", "导出已完成"))


def test_host_session_origin_and_csrf_are_strict(web_app) -> None:
    _, _, client = web_app
    assert client.request("GET", "/api/tasks")[0] == 401
    assert client.request("GET", "/", headers={"Host": "evil.test"})[0] == 421
    assert client.request("GET", "/", headers={"Host": f"[::1]:{client.server.server_address[1]}"})[0] == 200
    client.login()
    assert client.request("POST", "/api/tasks", {"url": "https://example.test"})[0] == 403
    assert client.request("POST", "/api/tasks", {"url": "https://example.test"}, headers={"Origin": "http://evil.test", "X-CSRF-Token": client.csrf})[0] == 403
    assert client.request("POST", "/api/tasks", {"url": "https://example.test"}, headers={"Origin": f"http://{client.host}", "X-CSRF-Token": "wrong"})[0] == 403
    valid_cookie = client.cookie
    client.cookie = valid_cookie[:-1] + ("A" if valid_cookie[-1] != "A" else "B")
    assert client.request("GET", "/api/tasks")[0] == 401
    client.login()
    assert client.cookie != valid_cookie


def test_task_endpoints_use_application_service_and_only_post_mutates(web_app) -> None:
    app, _, client = web_app
    client.login()
    status, _, created = decoded(client.post("/api/tasks", {"url": "https://example.test/book", "options": {"export": False}}))
    assert status == 202 and created["task"]["task_id"] == "task-1"
    assert decoded(client.request("GET", "/api/tasks"))[2]["tasks"][0]["status"] == "created"
    assert decoded(client.request("GET", "/api/tasks/task-1"))[2]["task"]["version"] == 0
    assert decoded(client.request("GET", "/api/tasks/task-1/events"))[2]["events"][0]["event_id"] == 1
    for action in ("pause", "resume", "cancel", "continue", "retry-cleanup"):
        assert client.post(f"/api/tasks/task-1/{action}", {})[0] == 200
    assert client.post("/api/tasks/task-1/confirm", {"selector_overrides": {"content": "article"}})[0] == 200
    assert client.request("GET", "/api/tasks/task-1/pause")[0] == 405
    assert ("create", ("https://example.test/book", {"export": False})) in app.calls


def test_json_contract_rejects_type_size_and_unknown_fields(web_app) -> None:
    _, server, client = web_app
    client.login()
    secure = {"Origin": f"http://{client.host}", "X-CSRF-Token": client.csrf}
    assert client.request("POST", "/api/tasks", None, headers={**secure, "Content-Type": "text/plain", "Content-Length": "0"})[0] == 415
    assert client.post("/api/tasks", {"url": "https://example.test", "extra": 1})[0] == 400
    oversized = (
        "POST /api/tasks HTTP/1.1\r\n"
        f"Host: {client.host}\r\nCookie: {client.cookie}\r\nOrigin: http://{client.host}\r\n"
        f"X-CSRF-Token: {client.csrf}\r\nContent-Type: application/json\r\nContent-Length: 999999\r\n\r\n"
    ).encode()
    assert raw_request(server, oversized).startswith(b"HTTP/1.1 413")


def test_book_endpoints_never_return_paths_or_urls(web_app) -> None:
    _, _, client = web_app
    client.login()
    books = decoded(client.request("GET", "/api/books"))[2]
    assert books == {"books": [{"id": 1, "title": "安全书名", "site": "fixture", "total": 2, "done": 1}]}
    assert decoded(client.request("GET", "/api/books/1/report"))[2] == {"report": "进度正常"}
    assert decoded(client.post("/api/books/1/export", {"format": "txt"}))[2] == {"completed": True, "format": "txt"}
    assert decoded(client.post("/api/books/1/delete", {}))[2]["state"] == "completed"


def test_dependency_exceptions_are_redacted_and_concurrent_queries_are_safe(web_app) -> None:
    app, _, client = web_app
    client.login()
    app.list_tasks = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("token=secret C:\\private"))
    status, _, payload = decoded(client.request("GET", "/api/tasks"))
    assert status == 500 and payload == {"error": {"code": "internal_error", "retryable": True}}
    app.list_tasks = lambda **_kwargs: [app.task]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: client.request("GET", "/api/tasks")[0], range(24)))
    assert results == [200] * 24


def test_owned_factory_is_closed_but_supplied_application_is_not() -> None:
    supplied = FakeApplication()
    server = create_web_server(app=supplied, host="127.0.0.1", port=0)
    server.server_close()
    assert supplied.closed == 0
    owned = FakeApplication()
    server2 = create_web_server(app_factory=lambda: owned, host="127.0.0.1", port=0)
    server2.server_close()
    server2.server_close()
    assert owned.closed == 1


def test_owned_application_close_failure_is_observable() -> None:
    owned = FakeApplication()
    owned.close = lambda: False
    server = create_web_server(app_factory=lambda: owned, host="127.0.0.1", port=0)
    with pytest.raises(RuntimeError, match="application_close_failed"):
        server.server_close()


def test_application_errors_map_to_stable_safe_status(web_app) -> None:
    app, _, client = web_app
    client.login()
    app.get_task = lambda _task_id: (_ for _ in ()).throw(ApplicationError("task_not_found"))
    status, _, payload = decoded(client.request("GET", "/api/tasks/missing"))
    assert status == 404 and payload == {"error": {"code": "task_not_found", "retryable": False}}


def test_session_table_is_bounded_and_all_unsafe_methods_are_rejected(web_app) -> None:
    _, server, client = web_app
    client.login()
    previous_cookie, previous_csrf = client.cookie, client.csrf
    for _ in range(1024):
        server.session_codec.issue()
    client.login()
    assert (client.cookie, client.csrf) == (previous_cookie, previous_csrf)
    assert Client(server).request("GET", "/")[0] == 200
    assert client.request("GET", "/api/tasks")[0] == 200
    for method in ("OPTIONS", "PUT", "DELETE", "PATCH"):
        assert client.request(method, "/api/tasks", {})[0] == 405
        assert client.request(method, "/api/tasks", {}, headers={"Host": "evil.test"})[0] == 421


def test_paths_nan_and_deep_json_fail_closed(web_app) -> None:
    _, _, client = web_app
    client.login()
    assert client.request("GET", "/api/tasks/")[0] == 404
    assert client.request("GET", "/api/%74asks")[0] == 400
    secure = {"Origin": f"http://{client.host}", "X-CSRF-Token": client.csrf, "Content-Type": "application/json"}
    assert client.request("POST", "/api/tasks", None, headers={**secure, "Content-Length": "-1"})[0] == 413
    connection = http.client.HTTPConnection("127.0.0.1", client.server.server_address[1], timeout=3)
    connection.putrequest("POST", "/api/tasks", skip_host=True)
    connection.putheader("Host", client.host)
    connection.putheader("Cookie", client.cookie)
    connection.putheader("Origin", f"http://{client.host}")
    connection.putheader("X-CSRF-Token", client.csrf)
    connection.putheader("Content-Type", "application/json")
    raw = b'{"url":NaN}'
    connection.putheader("Content-Length", str(len(raw)))
    connection.endheaders(raw)
    response = connection.getresponse()
    assert response.status == 400
    response.read()
    connection.close()
    deep: object = "x"
    for _ in range(10):
        deep = {"x": deep}
    assert client.post("/api/tasks", {"url": "https://example.test", "options": deep})[0] == 400


def test_ambiguous_headers_and_json_are_rejected(web_app) -> None:
    _, server, client = web_app
    client.login()
    assert raw_authenticated(client, "", b"").startswith(b"HTTP/1.1 411")
    assert raw_authenticated(client, "Content-Length: 2\r\nContent-Length: 2\r\n", b"{}").startswith(b"HTTP/1.1 411")
    assert raw_authenticated(client, "Transfer-Encoding: chunked\r\n", b"0\r\n\r\n").startswith(b"HTTP/1.1 400")
    assert raw_authenticated(client, f"Origin: http://{client.host}\r\nContent-Length: 2\r\n", b"{}").startswith(b"HTTP/1.1 403")
    assert raw_authenticated(client, f"X-CSRF-Token: {client.csrf}\r\nContent-Length: 2\r\n", b"{}").startswith(b"HTTP/1.1 403")
    assert raw_authenticated(client, "Content-Type: application/json\r\nContent-Length: 2\r\n", b"{}").startswith(b"HTTP/1.1 415")
    duplicate = b'{"url":"https://example.test","url":"https://other.test"}'
    assert raw_authenticated(client, f"Content-Length: {len(duplicate)}\r\n", duplicate).startswith(b"HTTP/1.1 400")
    for host in (f"user@{client.host}", "[::1:1234"):
        response = raw_request(server, f"GET / HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
        assert response.startswith(b"HTTP/1.1 421")
    duplicate_host = raw_request(
        server,
        f"GET / HTTP/1.1\r\nHost: {client.host}\r\nHost: {client.host}\r\n\r\n".encode(),
    )
    assert duplicate_host.startswith(b"HTTP/1.1 421")


def test_invalid_route_fields_and_application_failures_are_stable(web_app) -> None:
    app, _, client = web_app
    client.login()
    assert client.request("GET", "/missing")[0] == 404
    assert client.request("POST", "/api/%74asks", {}, headers={
        "Origin": f"http://{client.host}", "X-CSRF-Token": client.csrf,
    })[0] == 400
    assert client.post("/api/tasks", {"url": 4})[0] == 400
    assert client.post("/api/tasks", {"url": "https://example.test", "options": []})[0] == 400
    assert client.post("/api/tasks/task-1/confirm", {"selector_overrides": {"content": 4}})[0] == 400
    assert client.post("/api/tasks/task-1/confirm", {"selector_overrides": {"unknown": "article"}})[0] == 400
    assert client.post("/api/tasks/task-1/confirm", {"selector_overrides": {"content": "a\n"}})[0] == 400
    assert client.post("/api/tasks/task-1/confirm", {"selector_overrides": {"content": "a" * 1001}})[0] == 400
    assert client.post("/api/books/1/export", {"format": "exe"})[0] == 400
    assert client.post("/api/books/0/delete", {})[0] == 400
    assert client.post("/api/unknown", {})[0] == 404
    app.pause_task = lambda _task_id: (_ for _ in ()).throw(ApplicationError("busy", retryable=True))
    assert client.post("/api/tasks/task-1/pause", {})[0] == 503
    app.pause_task = lambda _task_id: (_ for _ in ()).throw(RuntimeError("secret=hidden"))
    assert decoded(client.post("/api/tasks/task-1/pause", {}))[2]["error"]["code"] == "internal_error"


def test_sessions_expire_and_safe_serialization_redacts_unknown_values() -> None:
    now = [1000.0]
    codec = web_server._SessionCodec(secret=b"x" * 32, ttl=2, clock=lambda: now[0])
    token, csrf = codec.issue()
    assert codec.verify(token).csrf == csrf
    assert codec.verify(token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    now[0] = 1003.0
    assert codec.verify(token) is None
    assert web_server._safe_value(float("nan")) == 0
    assert web_server._safe_value([object()]) == ["[redacted]"]
    assert web_server._safe_text("token=secret", 100) == "[redacted]"


def test_task_nested_views_are_schema_allowlisted(web_app) -> None:
    app, _, client = web_app
    client.login()
    app.task = View({
        "task_id": "task-1",
        "status": "running",
        "private": "plain-secret",
        "progress": {"done": 1, "private": 99},
        "interaction": {
            "kind": "config",
            "attempt": 1,
            "safe_origin": "https://example.test/",
            "confirmation_required": True,
            "token": "plain-secret",
        },
    })
    task = decoded(client.request("GET", "/api/tasks/task-1"))[2]["task"]
    assert task["progress"] == {"done": 1}
    assert task["interaction"] == {
        "kind": "config",
        "attempt": 1,
        "expires_at": None,
        "safe_origin": "https://example.test/",
        "confirmation_required": True,
    }
    assert "plain-secret" not in json.dumps(task)
    interaction = app.task.value["interaction"]
    assert isinstance(interaction, dict)
    for unsafe in ("https://user@example.test/", "https://example.test:0/", "https://[fe80::1%25eth0]/", "http://2130706433/"):
        interaction["safe_origin"] = unsafe
        task = decoded(client.request("GET", "/api/tasks/task-1"))[2]["task"]
        assert task["interaction"]["safe_origin"] is None


def test_csp_uses_nonce_without_unsafe_inline(web_app) -> None:
    _, _, client = web_app
    _, headers, body = client.request("GET", "/")
    csp = headers["Content-Security-Policy"]
    assert "nonce-" in csp and "unsafe-inline" not in csp
    nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)  # type: ignore[union-attr]
    assert f'<script nonce="{nonce}">'.encode() in body


def test_rejected_request_body_cannot_be_reinterpreted_on_same_connection(web_app) -> None:
    _, server, client = web_app
    injected = f"GET / HTTP/1.1\r\nHost: {client.host}\r\n\r\n".encode()
    request = (
        f"POST /api/tasks HTTP/1.1\r\nHost: evil.test\r\nContent-Length: {len(injected)}\r\n\r\n".encode()
        + injected
    )
    response = raw_request(server, request)
    assert response.count(b"HTTP/1.1") == 1
    assert b"Connection: close" in response


def test_slow_clients_are_bounded_and_capacity_recovers(capsys) -> None:
    app = FakeApplication()
    server = create_web_server(
        app=app,
        host="127.0.0.1",
        port=0,
        max_connections=4,
        read_timeout=0.2,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sockets: list[socket.socket] = []
    try:
        for _ in range(32):
            connection = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2)
            connection.settimeout(2)
            connection.sendall(b"GET / HTTP/1.1\r\nHost:")
            sockets.append(connection)
            if len(sockets) == 4:
                time.sleep(0.05)
                try:
                    busy = raw_request(
                        server,
                        f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{server.server_address[1]}\r\n\r\n".encode(),
                    )
                except OSError:
                    busy = b""
                assert not busy or busy.startswith(b"HTTP/1.1 503")
        started = time.monotonic()
        responses = []
        for connection in sockets:
            chunks = []
            try:
                while chunk := connection.recv(4096):
                    chunks.append(chunk)
            except OSError:
                pass
            responses.append(b"".join(chunks))
        assert time.monotonic() - started < 3
        assert all(not response or response.startswith(b"HTTP/1.1 503") for response in responses)
        assert Client(server).request("GET", "/")[0] == 200
    finally:
        for connection in sockets:
            connection.close()
        server.shutdown()
        server.server_close()
        thread.join(3)
    assert "Traceback" not in capsys.readouterr().err


def test_factory_and_bind_failures_have_bounded_safe_cleanup(monkeypatch) -> None:
    def broken_factory():
        raise RuntimeError("token=private")

    with pytest.raises(RuntimeError, match="^application_factory_failed$") as factory_error:
        create_web_server(app_factory=broken_factory, host="127.0.0.1", port=0)
    assert "private" not in str(factory_error.value)

    owned = FakeApplication()
    owned.close = lambda: False

    def broken_server(*_args, **_kwargs):
        raise OSError("C:\\private\\token.txt")

    monkeypatch.setattr(web_server, "WebServer", broken_server)
    with pytest.raises(RuntimeError, match="^application_close_failed$") as close_error:
        create_web_server(app_factory=lambda: owned, host="127.0.0.1", port=0)
    assert "private" not in str(close_error.value)


def test_factory_ownership_and_server_limits_fail_closed() -> None:
    called = False

    def factory():
        nonlocal called
        called = True
        return FakeApplication()

    with pytest.raises(ValueError, match="factory application must be owned"):
        create_web_server(app_factory=factory, close_application=False, host="127.0.0.1", port=0)
    assert called is False
    for value in (True, 0, 257):
        with pytest.raises(ValueError, match="invalid web server limits"):
            create_web_server(app=FakeApplication(), max_connections=value, host="127.0.0.1", port=0)
    for value in (True, float("nan"), float("inf"), 0.01):
        with pytest.raises(ValueError, match="invalid web server limits"):
            create_web_server(app=FakeApplication(), read_timeout=value, host="127.0.0.1", port=0)


def test_close_drains_handlers_before_closing_owned_application() -> None:
    app = FakeApplication()
    entered = threading.Event()
    release = threading.Event()

    def blocked_tasks(**_kwargs):
        entered.set()
        release.wait(3)
        return [app.task]

    app.list_tasks = blocked_tasks
    server = create_web_server(
        app_factory=lambda: app,
        host="127.0.0.1",
        port=0,
        handler_drain_timeout=0.05,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(server)
    client.login()
    request_thread = threading.Thread(target=lambda: client.request("GET", "/api/tasks"), daemon=True)
    request_thread.start()
    assert entered.wait(1)
    server.shutdown()
    with pytest.raises(RuntimeError, match="^close_incomplete$"):
        server.server_close()
    assert app.closed == 0
    release.set()
    request_thread.join(2)
    server.server_close()
    thread.join(2)
    assert app.closed == 1


def test_request_thread_errors_never_print_tracebacks(web_app, capsys) -> None:
    _, server, _ = web_app
    for error in (
        TimeoutError("private"),
        BrokenPipeError("private"),
        ConnectionResetError("private"),
        ConnectionAbortedError("private"),
        RuntimeError("token=private"),
    ):
        try:
            raise error
        except Exception:
            server.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == ""
    assert server.error_counts == {"socket_error": 4, "request_error": 1}


def test_handler_finish_swallows_and_counts_safe_error_codes(web_app, monkeypatch, capsys) -> None:
    _, server, _ = web_app
    handler = object.__new__(web_server.WebHandler)
    handler.server = server

    def broken_pipe(_handler):
        raise BrokenPipeError("C:\\private")

    monkeypatch.setattr(BaseHTTPRequestHandler, "finish", broken_pipe)
    handler.finish()

    def unknown(_handler):
        raise RuntimeError("token=private")

    monkeypatch.setattr(BaseHTTPRequestHandler, "finish", unknown)
    handler.finish()
    assert capsys.readouterr().err == ""
    assert server.error_counts == {"socket_error": 1, "request_error": 1}


def test_run_web_ui_drains_borrowed_handlers_before_returning_to_cli(monkeypatch) -> None:
    app = FakeApplication()

    class DrainingServer:
        def __init__(self) -> None:
            self.close_calls = 0

        def serve_forever(self) -> None:
            return None

        def server_close(self) -> None:
            self.close_calls += 1
            assert app.closed == 0
            if self.close_calls == 1:
                raise RuntimeError("close_incomplete")

    server = DrainingServer()
    monkeypatch.setattr(web_server, "create_web_server", lambda **_kwargs: server)
    web_server.run_web_ui(application=app, close_application=False)
    assert server.close_calls == 2
    assert app.closed == 0
    assert app.close() is True
