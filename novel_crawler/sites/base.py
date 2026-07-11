import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from novel_crawler.core.models import Book, Chapter

if TYPE_CHECKING:
    from novel_crawler.core.fetcher import Fetcher

BS_PARSER = "lxml"


class SiteAdapter(ABC):
    name = "base"
    requires_browser = False
    fetcher: "Fetcher | None" = None

    def set_fetcher(self, fetcher: "Fetcher") -> None:
        self.fetcher = fetcher

    @abstractmethod
    def match(self, url: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_book_info(self, html: str, url: str) -> Book:
        raise NotImplementedError

    @abstractmethod
    def get_chapter_list(self, html: str, url: str, *, start: int | None = None, count: int | None = None) -> list[Chapter]:
        raise NotImplementedError

    @abstractmethod
    def parse_chapter(self, html: str, url: str) -> tuple[str, str]:
        raise NotImplementedError

    def _find_chapter_link(self, html: str, url: str, pattern: str) -> str | None:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, BS_PARSER)
        for a in soup.find_all("a"):
            text = a.get_text(strip=True)
            if re.search(pattern, text, re.I):
                href = a.get("href")
                if href:
                    return urljoin(url, href)
        return None

    def find_next_chapter(self, html: str, url: str) -> str | None:
        return self._find_chapter_link(html, url, r"下一[章页节回]|next\s*chapter|下\s*一\s*頁")

    def find_prev_chapter(self, html: str, url: str) -> str | None:
        return self._find_chapter_link(html, url, r"上一[章页节回]|prev\s*chapter|上\s*一\s*頁")


class AdapterRegistry:
    def __init__(self, adapters: list[SiteAdapter]):
        self.adapters = adapters

    def find(self, url: str) -> SiteAdapter:
        for adapter in self.adapters:
            if adapter.match(url):
                return adapter
        raise ValueError(f"没有可用站点适配器: {url}")


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()
