from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
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
from typing import Protocol, cast
from urllib.parse import urlsplit

from novel_crawler.application import ApplicationError
from novel_crawler.core.domains import canonical_domain

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
    "manual_cleanup_required", "error_code",
}
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
        if re.fullmatch(r"/api/(?:tasks/[^/]+/(?:pause|resume|cancel|continue|confirm|retry-cleanup)|books/\d+/(?:export|delete))", path):
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
        self._error("not_found", HTTPStatus.NOT_FOUND)

    def _post_api(self, path: str, payload: dict[str, object]) -> None:
        app = self.server.application
        if path == "/api/tasks":
            _fields(payload, {"url", "options"}, {"url"})
            url = payload["url"]
            if not isinstance(url, str):
                raise _HttpError("request_invalid")
            options = payload.get("options")
            if options is not None and not isinstance(options, dict):
                raise _HttpError("request_invalid")
            return self._json(HTTPStatus.ACCEPTED, {"task": _view(app.create_crawl_task(url, options))})
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
        book = re.fullmatch(r"/api/books/(\d{1,10})/(export|delete)", path)
        if book:
            book_id, action = _book_id(book.group(1)), book.group(2)
            if action == "export":
                _fields(payload, {"format"})
                fmt = payload.get("format", "txt")
                if fmt not in {"txt", "epub", "md", "jsonl"}:
                    raise _HttpError("export_format_invalid")
                return self._json(HTTPStatus.OK, _allow_mapping(app.export_book(book_id, str(fmt)), _RESULT_FIELDS))
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
