"""Visible Chrome acquisition for sites that block non-browser HTTP clients."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.acquisition.security import redact_url


class VisibleBrowserAcquirer:
    """Fetch pages through a user-visible Chrome window.

    This is intentionally not headless.  It is a fallback for sites that require
    a real browser session, Cloudflare clearance, or manual interaction.
    """

    def __init__(
        self,
        user_data_dir: Path,
        *,
        channel: str = "chrome",
        wait_until: str = "domcontentloaded",
        settle_seconds: float = 0.0,
    ) -> None:
        if settle_seconds < 0 or settle_seconds > 30:
            raise ValueError("settle_seconds must be between 0 and 30")
        self.user_data_dir = user_data_dir
        self.channel = channel
        self.wait_until = wait_until
        self.settle_seconds = settle_seconds
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None

    def fetch(self, url: str, **kwargs: Any) -> PageSnapshot:
        return self.fetch_page(url, **kwargs).snapshot

    def fetch_page(
        self,
        url: str,
        *,
        task_key: str | None = None,
        timeout: float | None = None,
        **_kwargs: object,
    ) -> AcquiredPage:
        del task_key
        page = self._ensure_page()
        timeout_ms = max(1, int((timeout or 60) * 1000))
        response = page.goto(url, wait_until=self.wait_until, timeout=timeout_ms)
        if self.settle_seconds:
            time.sleep(self.settle_seconds)
        status = response.status if response is not None else 0
        headers = dict(response.headers) if response is not None else {}
        html = page.content()
        final_url = page.url
        snapshot = PageSnapshot(
            requested_url=redact_url(url),
            final_url=redact_url(final_url),
            status_code=status,
            headers={name.lower(): value for name, value in headers.items()},
            encoding="utf-8",
            html=html,
            body=html.encode("utf-8"),
            method="browser-visible",
            redirects=(),
            retrieved_at=datetime.now(UTC),
        )
        return AcquiredPage(snapshot, final_url)

    def close(self) -> None:
        context = self._context
        playwright = self._playwright
        self._page = None
        self._context = None
        self._playwright = None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass

    def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            channel=self.channel,
            headless=False,
            accept_downloads=False,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page


__all__ = ["VisibleBrowserAcquirer"]
