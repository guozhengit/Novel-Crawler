"""Runtime adapter for an already validated :class:`SiteConfig`."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from novel_crawler.adaptation.config_schema import SiteConfig
from novel_crawler.core.domains import canonical_domain
from novel_crawler.core.fetcher import FetchOptions
from novel_crawler.core.models import Book, Chapter
from novel_crawler.sites.base import SiteAdapter

_AUTHOR_PREFIX = re.compile(r"^(?:by|author|written\s+by|作者)\s*[：:]?\s*", re.I)


class SiteConfigAdapter(SiteAdapter):
    """Parse pages directly from an in-memory, validated site configuration."""

    def __init__(self, config: SiteConfig) -> None:
        if not isinstance(config, SiteConfig):
            raise TypeError("config must be a SiteConfig")
        self._config = config
        self.name = config.site
        policy = config.request_policy
        timeout = float(policy["timeout_seconds"])
        retries = int(policy["max_retries"]) + 1
        rate = float(policy["rate_limit_seconds"])
        self.fetch_options = FetchOptions(
            timeout=timeout,
            retries=retries,
            delay_min=rate,
            delay_max=rate,
            retry_backoff_min=rate,
            retry_backoff_max=rate,
            long_pause_min=rate,
            long_pause_max=rate,
            long_pause_every_min=1,
            long_pause_every_max=1,
        )

    def __repr__(self) -> str:
        return "SiteConfigAdapter(config_present=True)"

    def match(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or canonical_domain(parts.hostname or "") != self._config.domain:
                return False
        except (TypeError, ValueError):
            return False
        relative = parts.path or "/"
        absolute = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
        return any(pattern.matches(relative) or pattern.matches(absolute) for pattern in self._config.url_patterns)

    def get_book_info(self, html: str, url: str) -> Book:
        soup = BeautifulSoup(html, "lxml")
        selectors = self._selector_group("book")
        title = self._selected_text(soup, selectors.get("title"))
        if not title:
            raise ValueError("book_title_missing")
        author_text = self._selected_text(soup, selectors.get("author"))
        author = _AUTHOR_PREFIX.sub("", author_text).strip() or None
        return Book(title=title, author=author, url=url, site=self.name)

    def get_chapter_list(
        self,
        html: str,
        url: str,
        *,
        start: int | None = None,
        count: int | None = None,
    ) -> list[Chapter]:
        soup = BeautifulSoup(html, "lxml")
        selector = self._selector_group("book").get("chapter_list")
        if not selector:
            raise ValueError("chapter_list_selector_missing")
        roots = soup.select(selector)
        anchors: list[Tag] = []
        for root in roots:
            if root.name == "a":
                anchors.append(root)
            else:
                anchors.extend(node for node in root.select("a[href]") if isinstance(node, Tag))
        chapters: list[Chapter] = []
        seen: set[str] = set()
        for anchor in anchors:
            href = anchor.get("href")
            target = self._safe_join(url, href if isinstance(href, str) else "")
            title = anchor.get_text(" ", strip=True)
            if not target or not title or target in seen:
                continue
            seen.add(target)
            chapters.append(Chapter(index=len(chapters) + 1, title=title, url=target))
        begin = 0 if start is None else max(0, start - 1)
        end = None if count is None else begin + count
        selected = chapters[begin:end]
        return [
            Chapter(index=begin + offset, title=item.title, url=item.url) for offset, item in enumerate(selected, 1)
        ]

    def parse_chapter(self, html: str, url: str) -> tuple[str, str]:
        del url
        soup = BeautifulSoup(html, "lxml")
        selectors = self._selector_group("chapter")
        title = self._selected_text(soup, selectors.get("chapter_title") or selectors.get("title"))
        content_selector = selectors.get("content")
        node = soup.select_one(content_selector) if content_selector else None
        if node is None:
            raise ValueError("chapter_content_missing")
        for selector in ("script", "style", "noscript", "iframe", "form", "object", "embed", *self._clean_selectors()):
            for unwanted in node.select(selector):
                unwanted.decompose()
        content = "\n".join(line.strip() for line in node.get_text("\n").splitlines() if line.strip())
        if not content:
            raise ValueError("chapter_content_missing")
        return title, content

    def _safe_join(self, base: str, href: str) -> str | None:
        if not href:
            return None
        try:
            source = urlsplit(base)
            target = urlsplit(urljoin(base, href))
            if self._origin(source) != self._origin(target):
                return None
            if target.username or target.password:
                return None
            normalized = urlunsplit((target.scheme, target.netloc, target.path or "/", target.query, ""))
            return normalized if self.match(normalized) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _origin(parts: SplitResult) -> tuple[str, str, int]:
        scheme = parts.scheme.casefold()
        if scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("invalid URL")
        port = parts.port or (443 if scheme == "https" else 80)
        return scheme, canonical_domain(parts.hostname), port

    def _selector_group(self, name: str) -> Mapping[str, str]:
        value = self._config.selectors[name]
        assert isinstance(value, Mapping)
        return value

    def _clean_selectors(self) -> tuple[str, ...]:
        value = self._config.selectors["clean"]
        assert isinstance(value, tuple)
        return value

    @staticmethod
    def _selected_text(soup: BeautifulSoup, selector: str | None) -> str:
        node = soup.select_one(selector) if selector else None
        return node.get_text(" ", strip=True) if node is not None else ""


__all__ = ["SiteConfigAdapter"]
