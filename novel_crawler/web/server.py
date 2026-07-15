from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

from novel_crawler.application import ApplicationError
from novel_crawler.compliance import ALLOW_THIRD_PARTY_ENV, decide_third_party_access, is_local_or_test_url
from novel_crawler.core.domains import canonical_domain
from novel_crawler.easyvoice import EasyVoiceOptions
from novel_crawler.exploration import explore_site

from .assets import HTML

_MAX_BODY = 65_536
_BUSY_BODY = b'{"error":{"code":"server_busy","retryable":true}}'
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SELECTOR_FIELDS = {"title", "author", "chapter_list", "chapter_title", "content"}
_TASK_FIELDS = {
    "task_id", "status", "version", "created_at", "updated_at", "error_code",
    "resume_status", "terminal", "cleanup_required", "checkpoint_count",
    "checkpoint_version_total",
}
_EVENT_FIELDS = {"event_id", "task_id", "from_status", "to_status", "task_version", "created_at", "error_code"}
_INTERACTION_FIELDS = {
    "kind", "attempt", "expires_at", "safe_origin", "verification_required",
    "confirmation_required", "cleanup_required",
}
_PROGRESS_FIELDS = {"total", "done", "failed", "pending"}
_RESULT_FIELDS = {
    "completed", "state", "format", "book_id", "job_id", "cleanup_required",
    "manual_cleanup_required", "error_code", "chapters", "incomplete", "returncode",
    "operation",
}
_EXPLORATION_FIELDS = {"completed", "domain", "sample_count", "requires_dedicated_adapter", "warning_codes", "proposed_config"}
_TTS_PROGRESS_ROOT_ENV = "NOVEL_CRAWLER_TTS_PROGRESS_ROOT"
_DEFAULT_TTS_PROGRESS_ROOT = Path("/Users/admin/docker-data/easyVoice")
_TTS_GROUP = re.compile(r"^[A-Za-z0-9_-]+-\d{4}-\d{4}$")
_UNSAFE_TEXT = re.compile(
    r"https?://|(?:password|passwd|token|secret|cookie|authorization)\s*[:=]|"
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]|(?<![A-Za-z0-9_])/(?:[^/\s]+/)+[^/\s]+",
    re.I,
)


class Application(Protocol):
    def create_crawl_task(self, url: str, options: object = None) -> object: ...
    def list_tasks(self, **kwargs: object) -> list[object]: ...
    def get_task(self, task_id: str) -> object: ...
    def task_events(self, task_id: str) -> list[object]: ...
    def pause_task(self, task_id: str) -> object: ...
    def resume_task(self, task_id: str) -> object: ...
    def cancel_task(self, task_id: str) -> object: ...
    def continue_interaction(self, task_id: str) -> object: ...
    def confirm_interaction(self, task_id: str, selector_overrides: Mapping[str, str] | None = None) -> object: ...
    def retry_cleanup(self, task_id: str) -> object: ...
    def list_books(self) -> list[dict[str, object]]: ...
    def book_report(self, book_id: int) -> str: ...
    def export_book(self, book_id: int, fmt: str = "txt") -> dict[str, object]: ...
    def export_easyvoice_book(self, book_id: int, output: object = None) -> dict[str, object]: ...
    def convert_easyvoice_book(self, book_id: int, options: EasyVoiceOptions, **kwargs: object) -> dict[str, object]: ...
    def delete_book(self, book_id: int) -> dict[str, object]: ...
    def close(self) -> bool: ...


@dataclass(frozen=True)
class _Session:
    csrf: str
    expires: int


class _SessionCodec:
    def __init__(
        self,
        *,
        secret: bytes | None = None,
        ttl: int = 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._secret = secret or secrets.token_bytes(32)
        self._ttl = ttl
        self._clock = clock

    def issue(self) -> tuple[str, str]:
        issued = int(self._clock())
        csrf = secrets.token_urlsafe(32)
        payload = json.dumps(
            {"csrf": csrf, "issued": issued, "expiry": issued + self._ttl, "nonce": secrets.token_urlsafe(16)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        encoded = _b64encode(payload)
        signature = _b64encode(hmac.digest(self._secret, encoded.encode("ascii"), hashlib.sha256))
        return f"{encoded}.{signature}", csrf

    def verify(self, token: str) -> _Session | None:
        if not 100 <= len(token) <= 1024 or token.count(".") != 1:
            return None
        encoded, supplied = token.split(".", 1)
        expected = _b64encode(hmac.digest(self._secret, encoded.encode("ascii", "ignore"), hashlib.sha256))
        if not hmac.compare_digest(supplied, expected):
            return None
        try:
            payload = json.loads(_b64decode(encoded), object_pairs_hook=_unique_object)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or set(payload) != {"csrf", "issued", "expiry", "nonce"}:
            return None
        csrf, nonce = payload["csrf"], payload["nonce"]
        issued, expiry = payload["issued"], payload["expiry"]
        now = int(self._clock())
        if (
            not isinstance(csrf, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{40,64}", csrf) is None
            or not isinstance(nonce, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{20,32}", nonce) is None
            or isinstance(issued, bool)
            or not isinstance(issued, int)
            or isinstance(expiry, bool)
            or not isinstance(expiry, int)
            or expiry - issued != self._ttl
            or issued > now + 5
            or expiry < now
        ):
            return None
        return _Session(csrf, expiry)


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(
        self,
        address: tuple[str, int],
        app: Application,
        *,
        owns_app: bool,
        max_connections: int,
        read_timeout: float,
        handler_drain_timeout: float,
    ) -> None:
        self.application = app
        self.owns_application = owns_app
        self.session_codec = _SessionCodec()
        self._app_closed = False
        self._capacity = threading.BoundedSemaphore(max_connections)
        self._read_timeout = read_timeout
        self._handler_drain_timeout = handler_drain_timeout
        self._handlers = threading.Condition()
        self._active_handlers = 0
        self._error_lock = threading.Lock()
        self.error_counts = {"socket_error": 0, "request_error": 0}
        super().__init__(address, WebHandler)
        port = int(self.server_address[1])
        bound = str(self.server_address[0]).casefold()
        self.allowed_hosts = _allowed_hosts(str(address[0]), bound, port)
        if _is_loopback(bound):
            self.allowed_hosts.update({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})

    def server_close(self) -> None:
        super().server_close()
        if not self._drain_handlers(self._handler_drain_timeout):
            raise RuntimeError("close_incomplete")
        if self.owns_application and not self._app_closed:
            self._app_closed = _bounded_close(self.application)
            if not self._app_closed:
                raise RuntimeError("application_close_failed")

    def get_request(self) -> tuple[socket.socket, object]:
        request, address = super().get_request()
        request.settimeout(self._read_timeout)
        return request, address

    def process_request(self, request: socket.socket, client_address: object) -> None:
        if not self._capacity.acquire(blocking=False):
            try:
                request.setblocking(False)
                try:
                    while request.recv(4096):
                        pass
                except OSError:
                    pass
                request.setblocking(True)
                request.settimeout(self._read_timeout)
                headers = (
                    "HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(_BUSY_BODY)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
                request.sendall(headers + _BUSY_BODY)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        with self._handlers:
            self._active_handlers += 1
        try:
            super().process_request(request, client_address)
        except Exception:
            self._handler_finished()
            raise

    def process_request_thread(self, request: socket.socket, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_finished()

    def handle_error(self, request: object, client_address: object) -> None:
        del request, client_address
        error = sys.exc_info()[1]
        code = "socket_error" if isinstance(
            error,
            (TimeoutError, BrokenPipeError, ConnectionResetError, ConnectionAbortedError),
        ) else "request_error"
        self._record_error(code)

    def _record_error(self, code: str) -> None:
        with self._error_lock:
            self.error_counts[code] += 1

    def _handler_finished(self) -> None:
        self._capacity.release()
        with self._handlers:
            self._active_handlers -= 1
            self._handlers.notify_all()

    def _drain_handlers(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._handlers:
            while self._active_handlers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._handlers.wait(remaining)
            return True


class WebHandler(BaseHTTPRequestHandler):
    server: WebServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if not self._valid_host():
            return self._error("host_invalid", HTTPStatus.MISDIRECTED_REQUEST)
        path = self._path()
        if path is None:
            return self._error("path_invalid", HTTPStatus.BAD_REQUEST)
        if path == "/":
            current = self._session_data()
            if current is None:
                session_id, csrf = self.server.session_codec.issue()
            else:
                session_id, session = current
                csrf = session.csrf
            nonce = secrets.token_urlsafe(24)
            body = HTML.format(csrf=csrf, nonce=nonce).encode("utf-8")
            self._send(
                HTTPStatus.OK,
                body,
                "text/html; charset=utf-8",
                extra={
                    "Set-Cookie": f"nc_session={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age=3600",
                    "Content-Security-Policy": (
                        "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                        f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'"
                    ),
                },
            )
            return
        if not path.startswith("/api/"):
            return self._error("not_found", HTTPStatus.NOT_FOUND)
        if self._session() is None:
            return self._error("session_required", HTTPStatus.UNAUTHORIZED)
        if re.fullmatch(r"/api/(?:tasks/[^/]+/(?:pause|resume|cancel|continue|confirm|retry-cleanup)|books/\d+/(?:export|delete|tts-export|tts-convert)|explorations)", path):
            return self._error("method_not_allowed", HTTPStatus.METHOD_NOT_ALLOWED)
        try:
            self._get_api(path)
        except ApplicationError as exc:
            self._application_error(exc)
        except Exception:
            self._error("internal_error", HTTPStatus.INTERNAL_SERVER_ERROR, retryable=True)

    def do_POST(self) -> None:
        if not self._valid_host():
            return self._error("host_invalid", HTTPStatus.MISDIRECTED_REQUEST)
        path = self._path()
        if path is None:
            return self._error("path_invalid", HTTPStatus.BAD_REQUEST)
        session = self._session()
        if session is None:
            return self._error("session_required", HTTPStatus.UNAUTHORIZED)
        host = self.headers.get("Host", "").casefold()
        origins = self.headers.get_all("Origin") or []
        csrf_tokens = self.headers.get_all("X-CSRF-Token") or []
        if (
            origins != [f"http://{host}"]
            or len(csrf_tokens) != 1
            or not secrets.compare_digest(csrf_tokens[0], session.csrf)
        ):
            return self._error("csrf_invalid", HTTPStatus.FORBIDDEN)
        try:
            payload = self._json_body()
            self._post_api(path, payload)
        except _HttpError as exc:
            self._error(exc.code, exc.status)
        except ApplicationError as exc:
            self._application_error(exc)
        except Exception:
            self._error("internal_error", HTTPStatus.INTERNAL_SERVER_ERROR, retryable=True)

    def do_OPTIONS(self) -> None: self._method_not_allowed()
    def do_PUT(self) -> None: self._method_not_allowed()
    def do_DELETE(self) -> None: self._method_not_allowed()
    def do_PATCH(self) -> None: self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        if not self._valid_host():
            self._error("host_invalid", HTTPStatus.MISDIRECTED_REQUEST)
            return
        self._error("method_not_allowed", HTTPStatus.METHOD_NOT_ALLOWED)

    def _get_api(self, path: str) -> None:
        app = self.server.application
        if path == "/api/tasks":
            return self._json(HTTPStatus.OK, {"tasks": [_view(item) for item in app.list_tasks()]})
        match = re.fullmatch(r"/api/tasks/([A-Za-z0-9_-]{1,128})(/events)?", path)
        if match:
            task_id = match.group(1)
            if match.group(2):
                return self._json(HTTPStatus.OK, {"events": [_view(item) for item in app.task_events(task_id)]})
            return self._json(HTTPStatus.OK, {"task": _view(app.get_task(task_id))})
        if path == "/api/books":
            return self._json(HTTPStatus.OK, {"books": [_book(item) for item in app.list_books()]})
        match = re.fullmatch(r"/api/books/(\d{1,10})/report", path)
        if match:
            return self._json(HTTPStatus.OK, {"report": _safe_text(app.book_report(_book_id(match.group(1))), 32_768)})
        if path == "/api/tts/progress":
            return self._json(HTTPStatus.OK, {"conversions": _tts_progress()})
        self._error("not_found", HTTPStatus.NOT_FOUND)

    def _post_api(self, path: str, payload: dict[str, object]) -> None:
        app = self.server.application
        if path == "/api/tasks":
            _fields(payload, {"url", "options", "allow_third_party"}, {"url"})
            url = payload["url"]
            if not isinstance(url, str):
                raise _HttpError("request_invalid")
            options = payload.get("options")
            if options is not None and not isinstance(options, dict):
                raise _HttpError("request_invalid")
            allow_third_party = _bool(payload.get("allow_third_party", False))
            _require_authorized(url, allow_third_party)
            if allow_third_party:
                with _temporary_third_party_access():
                    task = app.create_crawl_task(url, options)
            else:
                task = app.create_crawl_task(url, options)
            return self._json(HTTPStatus.ACCEPTED, {"task": _view(task)})
        if path == "/api/explorations":
            _fields(payload, {"url", "sample", "allow_third_party"}, {"url", "allow_third_party"})
            url = payload["url"]
            if not isinstance(url, str):
                raise _HttpError("request_invalid")
            sample = payload.get("sample", 3)
            if isinstance(sample, bool) or not isinstance(sample, int) or not 1 <= sample <= 5:
                raise _HttpError("sample_invalid")
            _require_authorized(url, _bool(payload.get("allow_third_party")))
            report = explore_site(url, sample=sample)
            safe_report = {
                "completed": True,
                "domain": report["domain"],
                "sample_count": report["sample_count"],
                "requires_dedicated_adapter": report["requires_dedicated_adapter"],
                "warning_codes": [item["code"] for item in report["warnings"]],
                "proposed_config": report["proposed_config"],
            }
            return self._json(HTTPStatus.OK, _allow_mapping(safe_report, _EXPLORATION_FIELDS))
        task = re.fullmatch(r"/api/tasks/([A-Za-z0-9_-]{1,128})/(pause|resume|cancel|continue|confirm|retry-cleanup)", path)
        if task:
            task_id, action = task.groups()
            if action == "confirm":
                _fields(payload, {"selector_overrides"})
                overrides = payload.get("selector_overrides")
                if overrides is not None:
                    overrides = _selector_overrides(overrides)
                result = app.confirm_interaction(task_id, overrides)
            else:
                _fields(payload, set())
                operation = {
                    "pause": app.pause_task,
                    "resume": app.resume_task,
                    "cancel": app.cancel_task,
                    "continue": app.continue_interaction,
                    "retry-cleanup": app.retry_cleanup,
                }[action]
                result = operation(task_id)
            return self._json(HTTPStatus.OK, {"task": _view(result)})
        book = re.fullmatch(r"/api/books/(\d{1,10})/(export|delete|tts-export|tts-convert)", path)
        if book:
            book_id, action = _book_id(book.group(1)), book.group(2)
            if action == "export":
                _fields(payload, {"format"})
                fmt = payload.get("format", "txt")
                if fmt not in {"txt", "epub", "md", "jsonl"}:
                    raise _HttpError("export_format_invalid")
                return self._json(HTTPStatus.OK, _allow_mapping(app.export_book(book_id, str(fmt)), _RESULT_FIELDS))
            if action == "tts-export":
                _fields(payload, {"allow_third_party"})
                if _bool(payload.get("allow_third_party", False)):
                    with _temporary_third_party_access():
                        result = app.export_easyvoice_book(book_id)
                else:
                    result = app.export_easyvoice_book(book_id)
                return self._json(HTTPStatus.OK, _allow_mapping(result, _RESULT_FIELDS))
            if action == "tts-convert":
                _fields(payload, {"allow_third_party", "base_url", "voice", "rate", "pitch", "volume", "use_llm"})
                options = _easyvoice_options(payload)
                if _bool(payload.get("allow_third_party", False)):
                    with _temporary_third_party_access():
                        result = app.convert_easyvoice_book(book_id, options)
                else:
                    result = app.convert_easyvoice_book(book_id, options)
                return self._json(HTTPStatus.OK, _allow_mapping({**result, "operation": "tts-convert"}, _RESULT_FIELDS))
            _fields(payload, set())
            return self._json(HTTPStatus.OK, _allow_mapping(app.delete_book(book_id), _RESULT_FIELDS))
        raise _HttpError("not_found", HTTPStatus.NOT_FOUND)

    def _json_body(self) -> dict[str, object]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise _HttpError("request_invalid")
        content_types = self.headers.get_all("Content-Type") or []
        if len(content_types) != 1 or content_types[0].casefold() not in {
            "application/json", "application/json; charset=utf-8",
        }:
            raise _HttpError("content_type_invalid", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1:
            raise _HttpError("length_required", HTTPStatus.LENGTH_REQUIRED)
        try:
            length = int(lengths[0])
        except ValueError:
            raise _HttpError("request_invalid") from None
        if length < 0 or length > _MAX_BODY:
            raise _HttpError("body_too_large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise _HttpError("request_invalid")
        try:
            value = json.loads(
                raw,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
                object_pairs_hook=_unique_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise _HttpError("json_invalid") from None
        if not isinstance(value, dict) or _depth(value) > 8:
            raise _HttpError("json_invalid")
        return value

    def _session(self) -> _Session | None:
        current = self._session_data()
        return current[1] if current is not None else None

    def _session_data(self) -> tuple[str, _Session] | None:
        values = self.headers.get_all("Cookie") or []
        if len(values) != 1 or not values[0] or len(values[0]) > 4096:
            return None
        raw = values[0]
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        item = cookie.get("nc_session")
        if item is None:
            return None
        session = self.server.session_codec.verify(item.value)
        return (item.value, session) if session is not None else None

    def finish(self) -> None:
        try:
            super().finish()
        except (TimeoutError, BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.server._record_error("socket_error")
        except Exception:
            self.server._record_error("request_error")

    def _valid_host(self) -> bool:
        values = self.headers.get_all("Host") or []
        if len(values) != 1:
            return False
        value = values[0].casefold()
        if "@" in value:
            return False
        if value.startswith("["):
            match = re.fullmatch(r"\[([^]]+)\]:(\d{1,5})", value)
            if match is None:
                return False
            try:
                if ipaddress.ip_address(match.group(1)).version != 6:
                    return False
            except ValueError:
                return False
        elif re.fullmatch(r"[^\s/:]+:\d{1,5}", value) is None:
            return False
        return value in self.server.allowed_hosts

    def _path(self) -> str | None:
        if len(self.path) > 2048 or "%" in self.path or "?" in self.path or "#" in self.path:
            return None
        return self.path

    def _application_error(self, exc: ApplicationError) -> None:
        status = HTTPStatus.NOT_FOUND if exc.code in {"task_not_found"} else (
            HTTPStatus.SERVICE_UNAVAILABLE if exc.retryable else HTTPStatus.BAD_REQUEST
        )
        self._error(exc.code, status, retryable=exc.retryable)

    def _json(self, status: int, payload: Mapping[str, object]) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode(), "application/json; charset=utf-8")

    def _error(self, code: str, status: int, *, retryable: bool = False) -> None:
        self._json(status, {"error": {"code": code, "retryable": retryable}})

    def _send(self, status: int, body: bytes, content_type: str, *, extra: Mapping[str, str] | None = None) -> None:
        # Every response closes the HTTP/1.1 connection.  In particular, an
        # authentication rejection must never leave an unread request body that
        # a persistent connection could reinterpret as a second request.
        self.close_connection = True
        self.send_response(status)
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "Connection": "close",
        }
        headers.update(extra or {})
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        del args


class _HttpError(Exception):
    def __init__(self, code: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self.code, self.status = code, status
        super().__init__(code)


def create_web_server(
    *,
    app: Application | None = None,
    app_factory: Callable[[], Application] | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    unsafe_non_loopback: bool = False,
    close_application: bool | None = None,
    max_connections: int = 16,
    read_timeout: float = 5.0,
    handler_drain_timeout: float = 10.0,
) -> WebServer:
    if not _is_loopback(host) and not unsafe_non_loopback:
        raise ValueError("non-loopback binding requires unsafe_non_loopback=True")
    if (app is None) == (app_factory is None):
        raise ValueError("provide exactly one application source")
    if app_factory is not None and close_application is False:
        raise ValueError("factory application must be owned")
    if (
        isinstance(max_connections, bool)
        or not isinstance(max_connections, int)
        or not 1 <= max_connections <= 256
        or isinstance(read_timeout, bool)
        or not isinstance(read_timeout, int | float)
        or not math.isfinite(read_timeout)
        or not 0.1 <= read_timeout <= 60
        or isinstance(handler_drain_timeout, bool)
        or not isinstance(handler_drain_timeout, int | float)
        or not math.isfinite(handler_drain_timeout)
        or not 0.01 <= handler_drain_timeout <= 300
    ):
        raise ValueError("invalid web server limits")
    if app is not None:
        application = app
    else:
        try:
            application = app_factory()
        except Exception:
            raise RuntimeError("application_factory_failed") from None
        if application is None:
            raise RuntimeError("application_factory_failed")
    owns = app_factory is not None or close_application is True
    try:
        return WebServer(
            (host, port),
            application,
            owns_app=owns,
            max_connections=max_connections,
            read_timeout=read_timeout,
            handler_drain_timeout=handler_drain_timeout,
        )
    except Exception:
        if owns and not _bounded_close(application):
            raise RuntimeError("application_close_failed") from None
        raise RuntimeError("web_server_start_failed") from None


def run_web_ui(
    ctx: object = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    application: object | None = None,
    application_factory: Callable[[], Application] | None = None,
    close_application: bool = False,
    unsafe_remote: bool = False,
) -> None:
    del ctx
    server = create_web_server(
        app=cast(Application | None, application),
        app_factory=application_factory,
        host=host,
        port=port,
        unsafe_non_loopback=unsafe_remote,
        close_application=close_application or application_factory is not None,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        while True:
            try:
                server.server_close()
                break
            except RuntimeError as exc:
                if str(exc) != "close_incomplete":
                    raise


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _bounded_close(application: Application) -> bool:
    for _ in range(3):
        try:
            if application.close():
                return True
        except Exception:
            continue
    return False


def _allowed_hosts(configured: str, bound: str, port: int) -> set[str]:
    addresses = {bound}
    configured_folded = configured.casefold()
    if configured_folded in {"0.0.0.0", "::"}:
        # Wildcard listeners still use a strict finite allowlist.  Enumerating
        # the machine's own interface addresses makes explicit unsafe remote
        # mode usable without accepting attacker-selected Host names.
        hostname = socket.gethostname().casefold().rstrip(".")
        if hostname:
            addresses.add(hostname)
        try:
            addresses.update(info[4][0].casefold() for info in socket.getaddrinfo(hostname, None))
        except socket.gaierror:
            pass
        addresses.update({"127.0.0.1", "::1", "localhost"})
    result: set[str] = set()
    for address in addresses:
        rendered = f"[{address}]" if ":" in address and not address.startswith("[") else address
        result.add(f"{rendered}:{port}")
    return result


def _view(value: object) -> dict[str, object]:
    method = getattr(value, "to_safe_dict", None)
    if not callable(method):
        raise ValueError("unsafe application result")
    raw = method()
    if not isinstance(raw, Mapping):
        raise ValueError("unsafe application result")
    if "event_id" in raw:
        return _allow_mapping(raw, _EVENT_FIELDS)
    result = _allow_mapping(raw, _TASK_FIELDS)
    progress = raw.get("progress")
    if isinstance(progress, Mapping):
        result["progress"] = {
            key: count
            for key, count in progress.items()
            if key in _PROGRESS_FIELDS and isinstance(count, int) and not isinstance(count, bool) and count >= 0
        }
    interaction = raw.get("interaction")
    if interaction is None:
        result["interaction"] = None
    elif isinstance(interaction, Mapping):
        result["interaction"] = _interaction(interaction)
    return result


def _interaction(value: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    kind = value.get("kind")
    if isinstance(kind, str) and 0 < len(kind) <= 64:
        result["kind"] = _safe_text(kind, 64)
    attempt = value.get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 0:
        result["attempt"] = attempt
    expires_at = value.get("expires_at")
    if expires_at is None or isinstance(expires_at, str) and len(expires_at) <= 64:
        result["expires_at"] = _safe_value(expires_at)
    for key in ("verification_required", "confirmation_required", "cleanup_required"):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    origin = value.get("safe_origin")
    result["safe_origin"] = origin if isinstance(origin, str) and _safe_origin(origin) else None
    return result


def _safe_origin(value: str) -> bool:
    if not value or len(value) > 300 or not value.isascii():
        return False
    if "://" not in value:
        display = _canonical_host(value)
        return display is not None and display.strip("[]") == value
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65_535
    ):
        return False
    display = _canonical_host(parsed.hostname)
    if display is None:
        return False
    default_port = 443 if parsed.scheme == "https" else 80
    authority = display if port in {None, default_port} else f"{display}:{port}"
    return value == f"{parsed.scheme}://{authority}/"


def _canonical_host(value: str) -> str | None:
    if not value or not value.isascii() or "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if value.isdigit() or re.fullmatch(r"[0-9.]+", value):
            return None
        try:
            canonical = canonical_domain(value)
        except (TypeError, ValueError):
            return None
        return canonical if canonical == value.lower().rstrip(".") else None
    rendered = str(address)
    return f"[{rendered}]" if address.version == 6 else rendered


def _allow_mapping(value: Mapping[str, object], allowed: set[str]) -> dict[str, object]:
    return _safe_mapping({key: item for key, item in value.items() if key in allowed})


def _safe_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {key: _safe_value(item) for key, item in value.items() if isinstance(key, str)}


def _safe_value(value: object) -> object:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else 0
    if isinstance(value, str):
        return _safe_text(value, 32_768)
    if isinstance(value, Mapping):
        return _safe_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value[:1000]]
    return "[redacted]"


def _safe_text(value: str, maximum: int) -> str:
    return "[redacted]" if _UNSAFE_TEXT.search(value) or any(ord(char) < 32 and char not in "\n\t" for char in value) else value[:maximum]


def _book(value: Mapping[str, object]) -> dict[str, object]:
    allowed = {"id", "title", "author", "site", "total", "done", "failed", "pending"}
    return _safe_mapping({key: item for key, item in value.items() if key in allowed})


def _tts_progress() -> list[dict[str, object]]:
    root = _tts_progress_root()
    if root is None or not root.is_dir():
        return []
    output_roots = _tts_output_roots(root)
    return [_tts_output_progress(output) for output in output_roots[:20]]


def _tts_progress_root() -> Path | None:
    configured = os.environ.get(_TTS_PROGRESS_ROOT_ENV)
    root = Path(configured).expanduser() if configured else _DEFAULT_TTS_PROGRESS_ROOT
    try:
        resolved = root.resolve()
    except OSError:
        return None
    if len(str(resolved)) > 512:
        return None
    return resolved


def _tts_output_roots(root: Path) -> list[Path]:
    candidates: set[Path] = set()
    if _looks_like_tts_output(root):
        candidates.add(root)
    try:
        for child in root.iterdir():
            if child.is_dir():
                if _looks_like_tts_output(child):
                    candidates.add(child)
                for grandchild in list(child.iterdir())[:200]:
                    if grandchild.is_dir() and _looks_like_tts_output(grandchild):
                        candidates.add(grandchild)
    except OSError:
        pass
    return sorted(candidates, key=lambda path: _latest_mtime(path), reverse=True)


def _looks_like_tts_output(path: Path) -> bool:
    if (path / "tts-jobs.sqlite3").is_file():
        return True
    try:
        return any(child.is_dir() and _TTS_GROUP.fullmatch(child.name) for child in list(path.iterdir())[:200])
    except OSError:
        return False


def _tts_output_progress(output: Path) -> dict[str, object]:
    groups = [_tts_group_progress(group) for group in _tts_group_dirs(output)]
    completed = sum(1 for group in groups if group["status"] == "completed")
    failed = sum(1 for group in groups if group["status"] == "failed")
    active = next((group for group in groups if group["status"] == "running"), None)
    current = active or next((group for group in groups if group["status"] not in {"completed", "empty"}), None)
    return {
        "id": _safe_tts_name(output.name),
        "total_groups": len(groups),
        "completed_groups": completed,
        "failed_groups": failed,
        "running": active is not None,
        "current_group": current["group"] if current else None,
        "current_phase": current["phase"] if current else "idle",
        "groups": groups[:50],
    }


def _tts_group_dirs(output: Path) -> list[Path]:
    try:
        groups = [child for child in output.iterdir() if child.is_dir() and _TTS_GROUP.fullmatch(child.name)]
    except OSError:
        return []
    return sorted(groups, key=lambda path: path.name)


def _tts_group_progress(group: Path) -> dict[str, object]:
    manifest = _read_json(group / "manifest.json")
    chapters = group / "chapters"
    assembled = group / "assembled"
    chapter_mp3 = _count_files(chapters, "*.mp3")
    chapter_srt = _count_files(chapters, "*.srt")
    assembled_mp3 = assembled / f"{group.name}.mp3"
    assembled_srt = assembled / f"{group.name}.srt"
    assembled_m4b = assembled / f"{group.name}.m4b"
    has_mp3 = assembled_mp3.is_file()
    has_srt = assembled_srt.is_file()
    has_m4b = assembled_m4b.is_file()
    manifest_completed = isinstance(manifest, Mapping) and manifest.get("status") == "COMPLETED"
    manifest_assembled = isinstance(manifest, Mapping) and isinstance(manifest.get("assembled"), Mapping)
    recent_m4b = has_m4b and time.time() - _safe_mtime(assembled_m4b) < 180
    if manifest_assembled:
        status, phase = "completed", "completed"
    elif recent_m4b:
        status, phase = "running", "m4b_transcoding"
    elif has_mp3 and has_srt and has_m4b:
        status, phase = "completed", "completed"
    elif has_mp3 and has_srt and not has_m4b:
        status, phase = "running", "m4b_pending"
    elif has_mp3 and chapter_mp3 >= 1:
        status, phase = "running", "assembling"
    elif chapter_mp3 >= 1:
        status, phase = "running", "chapters"
    elif manifest_completed and has_mp3 and has_m4b:
        status, phase = "completed", "completed"
    else:
        status, phase = "empty", "pending"
    if _group_has_error(group):
        status, phase = "failed", "failed"
    return {
        "group": _safe_tts_name(group.name),
        "status": status,
        "phase": phase,
        "chapter_mp3": chapter_mp3,
        "chapter_srt": chapter_srt,
        "assembled": {"mp3": has_mp3, "srt": has_srt, "m4b": has_m4b, "m4b_size": _safe_size(assembled_m4b)},
        "updated_at": int(_latest_mtime(group)),
    }


def _read_json(path: Path) -> object | None:
    try:
        if not path.is_file() or path.stat().st_size > 2_000_000:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _count_files(path: Path, pattern: str) -> int:
    try:
        return sum(1 for item in path.glob(pattern) if item.is_file())
    except OSError:
        return 0


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _latest_mtime(path: Path) -> float:
    latest = _safe_mtime(path)
    try:
        for child in path.iterdir():
            latest = max(latest, _safe_mtime(child))
    except OSError:
        pass
    return latest


def _group_has_error(group: Path) -> bool:
    manifest = _read_json(group / "manifest.json")
    if isinstance(manifest, Mapping) and manifest.get("status") == "INCOMPLETE":
        return True
    return False


def _safe_tts_name(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value) else "redacted"


def _fields(value: Mapping[str, object], allowed: set[str], required: set[str] | None = None) -> None:
    if set(value) - allowed or not (required or set()) <= set(value):
        raise _HttpError("request_invalid")


def _selector_overrides(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - _SELECTOR_FIELDS:
        raise _HttpError("request_invalid")
    result: dict[str, str] = {}
    for key, selector in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(selector, str)
            or not selector
            or len(selector) > 1000
            or any(ord(char) < 32 for char in selector)
        ):
            raise _HttpError("request_invalid")
        result[key] = selector
    return result


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise _HttpError("request_invalid")
    return value


def _require_authorized(url: str, allowed: bool) -> None:
    if is_local_or_test_url(url):
        return
    if not allowed:
        raise _HttpError("third_party_confirmation_required")
    decision = decide_third_party_access(url)
    if decision.code != "third_party_crawl_disabled":
        raise _HttpError(decision.code)


class _temporary_third_party_access:
    def __enter__(self) -> None:
        self.previous = os.environ.get(ALLOW_THIRD_PARTY_ENV)
        os.environ[ALLOW_THIRD_PARTY_ENV] = "1"

    def __exit__(self, *_args: object) -> None:
        previous = getattr(self, "previous", None)
        if previous is None:
            os.environ.pop(ALLOW_THIRD_PARTY_ENV, None)
        else:
            os.environ[ALLOW_THIRD_PARTY_ENV] = previous


def _easyvoice_options(payload: Mapping[str, object]) -> EasyVoiceOptions:
    values: dict[str, object] = {}
    for key in ("base_url", "voice", "rate", "pitch", "volume"):
        value = payload.get(key)
        if value is not None:
            if not isinstance(value, str) or not value or len(value) > 300:
                raise _HttpError("request_invalid")
            values[key] = value
    use_llm = payload.get("use_llm")
    if use_llm is not None:
        values["use_llm"] = _bool(use_llm)
    return EasyVoiceOptions(**values)


def _book_id(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 2_147_483_647:
        raise _HttpError("book_id_invalid")
    return parsed


def _depth(value: object, level: int = 0) -> int:
    if level > 8:
        return level
    if isinstance(value, Mapping):
        return max((level, *(_depth(item, level + 1) for item in value.values())))
    if isinstance(value, list):
        return max((level, *(_depth(item, level + 1) for item in value)))
    return level


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


__all__ = ["WebServer", "create_web_server", "run_web_ui"]
