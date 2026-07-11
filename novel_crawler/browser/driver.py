"""Playwright adapter and browser-network safety boundary."""

from __future__ import annotations

import queue
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from charset_normalizer import from_bytes

from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.acquisition.security import ResolvedTarget, UrlSafetyPolicy, redact_url
from novel_crawler.core.domains import canonical_domain

from .proxy import PinnedSocksProxy

MAX_BROWSER_BODY_BYTES = 10 * 1024 * 1024
_RETAINED_HEADERS = frozenset({"content-type", "content-language", "etag", "last-modified"})


class RequestDecision(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


def _origin_key(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    raw_host = (parts.hostname or "").lower()
    host = raw_host if ":" in raw_host else canonical_domain(raw_host)
    return scheme, host, parts.port or (443 if scheme == "https" else 80)


class BrowserRequestPolicy:
    """Validate every browser request and enforce the initially approved origin."""

    def __init__(self, policy: UrlSafetyPolicy | None = None) -> None:
        self.policy = policy or UrlSafetyPolicy()
        self._origin: tuple[str, str, int] | None = None
        self._locked_url: str | None = None
        self._resolved_target: ResolvedTarget | None = None

    def lock(self, url: str, *, validate: bool = True) -> None:
        if validate:
            self._resolved_target = self.policy.validate(url)
        self._origin = _origin_key(url)
        self._locked_url = url

    @property
    def locked_url(self) -> str:
        if self._locked_url is None:
            raise RuntimeError("origin_not_locked")
        return self._locked_url

    @property
    def resolved_target(self) -> ResolvedTarget:
        if self._resolved_target is None:
            raise RuntimeError("origin_not_validated")
        return self._resolved_target

    def decide(self, url: str, *, resource_type: str, is_navigation: bool) -> RequestDecision:
        if self._origin is None:
            raise RuntimeError("origin_not_locked")
        if self._resolved_target is None:
            return RequestDecision.BLOCK
        try:
            scheme = urlsplit(url).scheme.lower()
            if scheme not in {"http", "https"}:
                return RequestDecision.BLOCK
            same_origin = _origin_key(url) == self._origin
        except (TypeError, ValueError):
            return RequestDecision.BLOCK
        return RequestDecision.ALLOW if same_origin else RequestDecision.BLOCK


@dataclass(frozen=True)
class BrowserPageSnapshot:
    """Raw browser page state; navigation URLs are private until converted."""

    requested_url: str = field(repr=False)
    final_url: str = field(repr=False)
    status_code: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_page_snapshot(self, *, max_body_bytes: int = MAX_BROWSER_BODY_BYTES) -> PageSnapshot:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        effective_max = min(max_body_bytes, MAX_BROWSER_BODY_BYTES)
        if len(self.body) > effective_max:
            raise ValueError("browser_body_too_large")
        filtered = {name.lower(): value for name, value in self.headers.items() if name.lower() in _RETAINED_HEADERS}
        encoding, html = self._decode(self.body, filtered.get("content-type"))
        return PageSnapshot(
            requested_url=redact_url(self.requested_url),
            final_url=redact_url(self.final_url),
            status_code=self.status_code,
            headers=filtered,
            encoding=encoding,
            html=html,
            body=self.body,
            method="browser",
            redirects=(),
            retrieved_at=self.retrieved_at,
            sample_id=f"sample-{secrets.token_hex(16)}",
        )

    def to_acquired_page(self, *, max_body_bytes: int = MAX_BROWSER_BODY_BYTES) -> AcquiredPage:
        return AcquiredPage(self.to_page_snapshot(max_body_bytes=max_body_bytes), self.final_url)

    @staticmethod
    def _decode(body: bytes, content_type: str | None) -> tuple[str, str]:
        charset = None
        if content_type and "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip(' "\'')
        if charset:
            try:
                return charset, body.decode(charset)
            except (LookupError, UnicodeDecodeError):
                pass
        result = from_bytes(body).best()
        if result is not None and result.encoding:
            encoding = result.encoding.lower().replace("_", "-")
            return encoding, str(result)
        return "utf-8", body.decode("utf-8", errors="replace")


class BrowserContext(Protocol):
    def navigate(self, url: str) -> BrowserPageSnapshot: ...
    def capture(self) -> BrowserPageSnapshot: ...
    def close(self) -> None: ...


class Driver(Protocol):
    def launch(self, *, user_data_dir: Path, headless: bool, policy: BrowserRequestPolicy) -> BrowserContext: ...


class _PlaywrightContext:
    def __init__(
        self,
        playwright: Any,
        context: Any,
        policy: BrowserRequestPolicy,
        *,
        proxy: PinnedSocksProxy | None = None,
        max_body_bytes: int = MAX_BROWSER_BODY_BYTES,
        deadline: float | None = None,
    ) -> None:
        self._playwright = playwright
        self._context = context
        self._policy = policy
        self._proxy = proxy
        self._max_body_bytes = max_body_bytes
        self._deadline = deadline
        self._requested_url = ""
        self._status = 200
        self._headers: dict[str, str] = {}
        self._dead = threading.Event()
        on_event = getattr(context, "on", None)
        if on_event is not None:
            on_event("close", lambda *_: self._dead.set())
        context.route("**/*", self._route)
        route_web_socket = getattr(context, "route_web_socket", None)
        if route_web_socket is None:
            raise RuntimeError("playwright_websocket_routing_required")
        route_web_socket("**/*", self._route_web_socket)
        context.add_init_script(
            "if ('serviceWorker' in navigator) navigator.serviceWorker.getRegistrations().then(r => r.forEach(x => x.unregister()));"
        )
        self._page = context.pages[0] if context.pages else context.new_page()

    def _route(self, route: Any) -> None:
        request = route.request
        decision = self._policy.decide(
            request.url,
            resource_type=request.resource_type,
            is_navigation=request.is_navigation_request(),
        )
        if decision is RequestDecision.ALLOW:
            route.continue_()
        else:
            route.abort("blockedbyclient")

    @staticmethod
    def _route_web_socket(web_socket: Any) -> None:
        web_socket.close(code=1008, reason="blocked")

    def navigate(self, url: str) -> BrowserPageSnapshot:
        if self._policy.decide(url, resource_type="document", is_navigation=True) is RequestDecision.BLOCK:
            raise RuntimeError("browser_navigation_blocked")
        self._set_remaining_timeout()
        self._requested_url = url
        response = self._page.goto(url, wait_until="domcontentloaded")
        if response is not None:
            self._status = response.status
            self._headers = dict(response.headers)
        return self.capture()

    def capture(self) -> BrowserPageSnapshot:
        if self._policy.decide(self._page.url, resource_type="document", is_navigation=True) is RequestDecision.BLOCK:
            raise RuntimeError("browser_navigation_blocked")
        self._set_remaining_timeout()
        outer_length = self._page.evaluate(
            "document.documentElement ? new TextEncoder().encode(document.documentElement.outerHTML).byteLength : 0"
        )
        if not isinstance(outer_length, int) or outer_length > self._max_body_bytes:
            raise ValueError("browser_body_too_large")
        body = self._page.content().encode("utf-8")
        if len(body) > self._max_body_bytes:
            raise ValueError("browser_body_too_large")
        return BrowserPageSnapshot(
            self._requested_url or self._page.url,
            self._page.url,
            self._status,
            self._headers,
            body,
        )

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            try:
                self._playwright.stop()
            finally:
                if self._proxy is not None:
                    self._proxy.close()

    def is_alive(self) -> bool:
        return not self._dead.is_set()

    def _set_remaining_timeout(self) -> None:
        if self._deadline is None:
            return
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("browser_deadline")
        milliseconds = max(1, int(remaining * 1000))
        set_timeout = getattr(self._context, "set_default_timeout", None)
        if set_timeout is not None:
            set_timeout(milliseconds)
        set_navigation_timeout = getattr(self._context, "set_default_navigation_timeout", None)
        if set_navigation_timeout is not None:
            set_navigation_timeout(milliseconds)


class DefaultPlaywrightDriver:
    """Production driver. Playwright is imported only when a browser is launched."""

    def __init__(
        self,
        *,
        max_body_bytes: int = MAX_BROWSER_BODY_BYTES,
        max_network_bytes: int = 64 * 1024 * 1024,
        max_connections: int = 32,
        operation_timeout: float = 30.0,
        renderer_process_limit: int = 4,
        js_heap_mb: int = 256,
    ) -> None:
        if min(max_body_bytes, max_network_bytes, max_connections, renderer_process_limit, js_heap_mb) <= 0:
            raise ValueError("browser limits must be positive")
        if operation_timeout <= 0:
            raise ValueError("browser limits must be positive")
        self.max_body_bytes = min(max_body_bytes, MAX_BROWSER_BODY_BYTES)
        self.max_network_bytes = max_network_bytes
        self.max_connections = max_connections
        self.operation_timeout = operation_timeout
        self.renderer_process_limit = renderer_process_limit
        self.js_heap_mb = js_heap_mb

    def launch(self, *, user_data_dir: Path, headless: bool, policy: BrowserRequestPolicy) -> BrowserContext:
        from playwright.sync_api import sync_playwright

        deadline = time.monotonic() + self.operation_timeout
        proxy = PinnedSocksProxy(
            policy.locked_url,
            policy=policy.policy,
            resolved_target=policy.resolved_target,
            max_network_bytes=self.max_network_bytes,
            max_connections=self.max_connections,
            session_timeout=self.operation_timeout,
        )
        proxy.start()
        playwright: Any = None
        context: Any = None
        try:
            playwright = sync_playwright().start()
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=headless,
                proxy={"server": proxy.proxy_url, "bypass": "<-loopback>"},
                service_workers="block",
                accept_downloads=False,
                timeout=max(1, int((deadline - time.monotonic()) * 1000)),
                args=[
                    f"--renderer-process-limit={self.renderer_process_limit}",
                    f"--js-flags=--max-old-space-size={self.js_heap_mb}",
                    "--disable-features=ServiceWorker",
                    "--proxy-bypass-list=<-loopback>",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--disable-quic",
                    "--disable-background-networking",
                    "--dns-prefetch-disable",
                    "--disable-preconnect",
                ],
            )
            return _PlaywrightContext(
                playwright,
                context,
                policy,
                proxy=proxy,
                max_body_bytes=self.max_body_bytes,
                deadline=deadline,
            )
        except Exception:
            if context is not None:
                try:
                    context.close()
                except Exception:  # pragma: no cover - best-effort cleanup of partial Playwright startup
                    pass  # pragma: no cover
            if playwright is not None:
                playwright.stop()
            proxy.close()
            raise


class BrowserContextWorker:
    """Own a synchronous browser context and all its API calls on one thread."""

    def __init__(
        self,
        driver: Driver,
        *,
        user_data_dir: Path,
        headless: bool,
        policy: BrowserRequestPolicy,
        ttl: float,
        terminal_callback: Callable[[str, bool], None] | None = None,
    ) -> None:
        if ttl <= 0:
            raise ValueError("worker ttl must be positive")
        self._driver = driver
        self._user_data_dir = user_data_dir
        self._headless = headless
        self._policy = policy
        self._deadline = time.monotonic() + ttl
        self._commands: queue.Queue[tuple[str, tuple[object, ...], Future[Any]]] = queue.Queue()
        self._ready: Future[None] = Future()
        self._thread = threading.Thread(target=self._run, name="browser-context-worker", daemon=True)
        self._started = False
        self._closed = False
        self._expired = False
        self._terminal_callback = terminal_callback
        self._terminal_notified = False

    @property
    def deadline(self) -> float:
        return self._deadline

    def start(self) -> None:
        if self._started:
            raise RuntimeError("browser_worker_started")
        self._started = True
        self._thread.start()
        self._ready.result(timeout=max(1.0, self._deadline - time.monotonic()))

    def navigate(self, url: str) -> BrowserPageSnapshot:
        return self._submit("navigate", url)

    def capture(self) -> BrowserPageSnapshot:
        return self._submit("capture")

    def close(self) -> None:
        if self._closed:
            return
        self._submit("close", allow_expired=True)

    def is_alive(self) -> bool:
        return bool(self._submit("is_alive", allow_expired=True))

    def _submit(self, action: str, *args: object, allow_expired: bool = False) -> Any:
        if self._expired and not allow_expired:
            raise RuntimeError("browser_worker_expired")
        if self._closed:
            if action == "close":
                return None
            raise RuntimeError("browser_worker_closed")
        if not self._started:
            raise RuntimeError("browser_worker_not_started")
        future: Future[Any] = Future()
        self._commands.put((action, args, future))
        return future.result(timeout=max(1.0, self._deadline - time.monotonic()) if not allow_expired else 30.0)

    def _run(self) -> None:
        try:
            context = self._driver.launch(
                user_data_dir=self._user_data_dir,
                headless=self._headless,
                policy=self._policy,
            )
        except Exception as exc:
            self._ready.set_exception(exc)
            self._closed = True
            self._notify_terminal("crash", True)
            return
        self._ready.set_result(None)
        while True:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0 and not self._expired:
                self._expired = True
                try:
                    context.close()
                except Exception:  # pragma: no cover - worker remains quarantinable for explicit retry
                    self._notify_terminal("deadline", False)
                else:
                    self._closed = True
                    self._notify_terminal("deadline", True)
                    return
            try:
                action, args, future = self._commands.get(timeout=0.1 if self._expired else min(0.1, remaining))
            except queue.Empty:
                checker = getattr(context, "is_alive", None)
                if checker is not None:
                    try:
                        alive = bool(checker())
                    except Exception:  # pragma: no cover - conservative process liveness fallback
                        alive = True  # pragma: no cover
                    if not alive:
                        self._closed = True
                        self._notify_terminal("crash", True)
                        return
                continue
            if self._expired and action not in {"close", "is_alive"}:  # pragma: no cover - command race at deadline
                future.set_exception(RuntimeError("browser_worker_expired"))
                continue  # pragma: no cover
            try:
                result: Any
                if action == "navigate":
                    result = context.navigate(str(args[0]))
                elif action == "capture":
                    result = context.capture()
                elif action == "close":
                    context.close()
                    self._closed = True
                    future.set_result(None)
                    return
                elif action == "is_alive":
                    checker = getattr(context, "is_alive", None)
                    result = True if checker is None else bool(checker())
                else:  # pragma: no cover - queue is process-private and actions are fixed
                    raise RuntimeError("browser_worker_command")  # pragma: no cover
            except Exception as exc:
                future.set_exception(exc)
                checker = getattr(context, "is_alive", None)
                if checker is not None:
                    try:
                        alive = bool(checker())
                    except Exception:  # pragma: no cover - conservative process liveness fallback
                        alive = True  # pragma: no cover
                    if not alive:
                        self._closed = True
                        self._notify_terminal("crash", True)
                        return
            else:
                if time.monotonic() >= self._deadline and action not in {"close", "is_alive"}:
                    self._expired = True
                    try:
                        context.close()
                    except Exception:  # pragma: no cover - idle deadline failure path covers quarantine callback
                        self._notify_terminal("deadline", False)  # pragma: no cover
                    else:
                        self._closed = True
                        self._notify_terminal("deadline", True)
                    future.set_exception(RuntimeError("browser_worker_expired"))
                    if self._closed:
                        return
                else:
                    future.set_result(result)

    def _notify_terminal(self, reason: str, closed_ok: bool) -> None:
        if self._terminal_notified:
            return
        self._terminal_notified = True
        if self._terminal_callback is not None:
            try:
                self._terminal_callback(reason, closed_ok)
            except Exception:  # pragma: no cover - callback failures cannot compromise worker cleanup
                pass  # pragma: no cover
