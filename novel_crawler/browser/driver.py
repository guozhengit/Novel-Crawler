"""Playwright adapter and browser-network safety boundary."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from charset_normalizer import from_bytes

from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.acquisition.security import UrlSafetyError, UrlSafetyPolicy, redact_url
from novel_crawler.core.domains import canonical_domain

MAX_BROWSER_BODY_BYTES = 10 * 1024 * 1024
_RETAINED_HEADERS = frozenset({"content-type", "content-language", "etag", "last-modified"})
_INLINE_URL_LIMIT = 64 * 1024


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

    def __init__(self, policy: UrlSafetyPolicy | None = None, *, allow_public_cdn_subresources: bool = False) -> None:
        self.policy = policy or UrlSafetyPolicy()
        self.allow_public_cdn_subresources = allow_public_cdn_subresources
        self._origin: tuple[str, str, int] | None = None

    def lock(self, url: str, *, validate: bool = True) -> None:
        if validate:
            self.policy.validate(url)
        self._origin = _origin_key(url)

    def decide(self, url: str, *, resource_type: str, is_navigation: bool) -> RequestDecision:
        if self._origin is None:
            raise RuntimeError("origin_not_locked")
        scheme = urlsplit(url).scheme.lower()
        document = is_navigation or resource_type == "document"
        if scheme in {"data", "blob"}:
            allowed_type = resource_type in {"image", "media", "font"}
            return RequestDecision.ALLOW if not document and allowed_type and len(url) <= _INLINE_URL_LIMIT else RequestDecision.BLOCK
        if scheme not in {"http", "https"}:
            return RequestDecision.BLOCK
        try:
            self.policy.validate(url)
            same_origin = _origin_key(url) == self._origin
        except (UrlSafetyError, TypeError, ValueError):
            return RequestDecision.BLOCK
        if document:
            return RequestDecision.ALLOW if same_origin else RequestDecision.BLOCK
        if same_origin or self.allow_public_cdn_subresources:
            return RequestDecision.ALLOW
        return RequestDecision.BLOCK


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
    def __init__(self, playwright: Any, context: Any, policy: BrowserRequestPolicy) -> None:
        self._playwright = playwright
        self._context = context
        self._policy = policy
        self._page = context.pages[0] if context.pages else context.new_page()
        self._requested_url = ""
        self._status = 200
        self._headers: dict[str, str] = {}
        context.route("**/*", self._route)

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

    def navigate(self, url: str) -> BrowserPageSnapshot:
        if self._policy.decide(url, resource_type="document", is_navigation=True) is RequestDecision.BLOCK:
            raise RuntimeError("browser_navigation_blocked")
        self._requested_url = url
        response = self._page.goto(url, wait_until="domcontentloaded")
        if response is not None:
            self._status = response.status
            self._headers = dict(response.headers)
        return self.capture()

    def capture(self) -> BrowserPageSnapshot:
        if self._policy.decide(self._page.url, resource_type="document", is_navigation=True) is RequestDecision.BLOCK:
            raise RuntimeError("browser_navigation_blocked")
        body = self._page.content().encode("utf-8")
        if len(body) > MAX_BROWSER_BODY_BYTES:
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
            self._playwright.stop()


class DefaultPlaywrightDriver:
    """Production driver. Playwright is imported only when a browser is launched."""

    def launch(self, *, user_data_dir: Path, headless: bool, policy: BrowserRequestPolicy) -> BrowserContext:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        context: Any = None
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=headless,
            )
            return _PlaywrightContext(playwright, context, policy)
        except Exception:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            playwright.stop()
            raise
