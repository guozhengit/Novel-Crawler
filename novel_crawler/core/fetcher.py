import random
import time
from dataclasses import dataclass
from typing import Protocol

import requests

from novel_crawler.core.proxy_pool import ProxyPool

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
]


class PageAcquirer(Protocol):
    def fetch(self, url: str): ...


@dataclass
class FetchOptions:
    timeout: int = 25
    retries: int = 4
    delay_min: float = 2.0
    delay_max: float = 6.0
    retry_backoff_min: float = 5.0
    retry_backoff_max: float = 10.0
    long_pause_min: float = 8.0
    long_pause_max: float = 20.0
    long_pause_every_min: int = 15
    long_pause_every_max: int = 25


class Fetcher:
    def __init__(
        self,
        proxies: dict[str, str] | None = None,
        options: FetchOptions | None = None,
        enable_playwright: bool = False,
        proxy_pool: ProxyPool | None = None,
        acquirer: PageAcquirer | None = None,
    ):
        self.proxies = proxies or {}
        self.options = options or FetchOptions()
        self.enable_playwright = enable_playwright
        self.proxy_pool = proxy_pool
        self.acquirer = acquirer
        self.session = requests.Session()
        self._next_long_pause_at = random.randint(self.options.long_pause_every_min, self.options.long_pause_every_max)

    def headers(self, referer: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7",
            "Connection": "keep-alive",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def fetch_bytes(self, url: str, referer: str | None = None) -> bytes:
        if self.acquirer is not None:
            snapshot = self.acquirer.fetch(url)
            return snapshot.body
        last_error: Exception | None = None
        for attempt in range(self.options.retries):
            proxy = self._select_proxy()
            try:
                response = self.session.get(
                    url,
                    headers=self.headers(referer),
                    timeout=self.options.timeout,
                    proxies=proxy or None,
                )
                if response.status_code == 200:
                    return response.content
                last_error = RuntimeError(f"HTTP {response.status_code}")
            except Exception as exc:
                last_error = exc
                if self.proxy_pool and proxy:
                    self.proxy_pool.record_fail(proxy)
            time.sleep(random.uniform(self.options.retry_backoff_min, self.options.retry_backoff_max) * (attempt + 1))
        raise RuntimeError(f"抓取失败：{url}：{last_error}")

    def _select_proxy(self) -> dict[str, str] | None:
        if self.proxy_pool:
            return self.proxy_pool.next()
        return self.proxies or None

    def fetch_text(self, url: str, referer: str | None = None) -> str:
        if self.acquirer is not None:
            return self.acquirer.fetch(url).html
        try:
            content = self.fetch_bytes(url, referer)
            text = decode_bytes(content)
            if text.strip():
                return text
        except Exception:
            if not self.enable_playwright:
                raise
        if self.enable_playwright:
            return self.fetch_text_with_browser(url)
        raise RuntimeError(f"抓取内容为空：{url}")

    def fetch_text_with_browser(self, url: str) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("Playwright 不可用，无法使用浏览器渲染 fallback") from exc
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=random.choice(USER_AGENTS), locale="zh-CN")
                page.goto(url, wait_until="networkidle", timeout=self.options.timeout * 1000)
                return page.content()
            finally:
                browser.close()

    def polite_sleep(self, count: int) -> None:
        delay = random.uniform(self.options.delay_min, self.options.delay_max)
        if count >= self._next_long_pause_at:
            delay += random.uniform(self.options.long_pause_min, self.options.long_pause_max)
            self._next_long_pause_at = count + random.randint(self.options.long_pause_every_min, self.options.long_pause_every_max)
        time.sleep(delay)


def decode_bytes(content: bytes) -> str:
    try:
        from charset_normalizer import from_bytes
        result = from_bytes(content).best()
        if result:
            return str(result)
    except Exception:
        pass
    for encoding in ("utf-8", "gb18030", "big5"):
        try:
            return content.decode(encoding)
        except Exception:
            continue
    return content.decode("utf-8", errors="replace")
