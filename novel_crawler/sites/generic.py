import html as ihtml
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from novel_crawler.core.config import load_config
from novel_crawler.core.fetcher import FetchOptions
from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.utils import normalize_blank_lines
from novel_crawler.sites.base import SiteAdapter, domain_of


class GenericAdapter(SiteAdapter):
    """配置驱动的通用适配器。当前使用 JSON 配置，避免额外依赖 YAML。"""

    name = "generic"

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.name = self.config.get("site", config_path.stem)

    @property
    def fetch_options(self) -> FetchOptions | None:
        req = self.config.get("request")
        if not req:
            return None
        return FetchOptions(
            delay_min=req.get("delay_min", 2.0),
            delay_max=req.get("delay_max", 6.0),
            long_pause_min=req.get("long_pause_min", 8.0),
            long_pause_max=req.get("long_pause_max", 20.0),
            long_pause_every_min=req.get("long_pause_every_min", 15),
            long_pause_every_max=req.get("long_pause_every_max", 25),
            retries=req.get("retries", 4),
            timeout=req.get("timeout", 25),
        )

    def match(self, url: str) -> bool:
        domains = self.config.get("domain", [])
        host = domain_of(url)
        return any(host.endswith(d) for d in domains)

    def get_book_info(self, html: str, url: str) -> Book:
        soup = BeautifulSoup(html, "html.parser")
        book_cfg = self.config.get("book", {})
        title = self._text(soup, book_cfg.get("title_selector")) or self.config.get("default_title") or "untitled"
        author = self._text(soup, book_cfg.get("author_selector"))
        return Book(title=title, author=author, url=url, site=self.name)

    def get_chapter_list(self, html: str, url: str, *, start: int | None = None, count: int | None = None) -> list[Chapter]:
        soup = BeautifulSoup(html, "html.parser")
        selector = self.config.get("book", {}).get("chapter_list_selector")
        if not selector:
            return []
        chapters = []
        for i, a in enumerate(soup.select(selector), start=1):
            href = a.get("href")
            if not href:
                continue
            if start and i < start:
                continue
            if count and len(chapters) >= count:
                break
            title = a.get_text(strip=True) or f"第{i}章"
            chapters.append(Chapter(index=i, title=title, url=urljoin(url, href)))
        return chapters

    def parse_chapter(self, html: str, url: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        cfg = self.config.get("chapter", {})
        clean = self.config.get("clean", {})
        for selector in clean.get("remove_selectors", ["script", "style"]):
            for tag in soup.select(selector):
                tag.decompose()
        title = self._text(soup, cfg.get("title_selector"))
        content_node = soup.select_one(cfg.get("content_selector", "body"))
        if content_node is None:
            return title, ""
        paragraph_selector = cfg.get("paragraph_selector")
        nodes = content_node.select(paragraph_selector) if paragraph_selector else content_node.find_all("p")
        if not nodes:
            nodes = [content_node]
        remove_contains = clean.get("remove_text_contains", [])
        paras = []
        for node in nodes:
            text = ihtml.unescape(node.get_text("", strip=True))
            if not text or any(x in text for x in remove_contains):
                continue
            paras.append(text)
        return title, normalize_blank_lines("\n".join(paras))

    def _text(self, soup: BeautifulSoup, selector: str | None) -> str | None:
        if not selector:
            return None
        node = soup.select_one(selector)
        return node.get_text(strip=True) if node else None
