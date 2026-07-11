import html as ihtml
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.utils import normalize_blank_lines
from novel_crawler.sites.base import SiteAdapter, domain_of
from novel_crawler.sites.detector import CHAPTER_HREF_RE, CHAPTER_TEXT_RE, CLEAN_TEXT_PHRASES, inspect_html


class AutoAdapter(SiteAdapter):
    """未知站点兜底适配器：基于页面结构启发式提取目录和正文。"""

    name = "auto"

    def match(self, url: str) -> bool:
        return True

    def get_book_info(self, html: str, url: str) -> Book:
        soup = BeautifulSoup(html, "html.parser")
        inspection = inspect_html(html, url)
        title = None
        if inspection.title_selector:
            node = soup.select_one(inspection.title_selector)
            if node:
                title = node.get("content", "") if node.name == "meta" else node.get_text(strip=True)
        if not title and soup.title:
            title = soup.title.get_text(strip=True).split("_")[0].split("-")[0]
        return Book(title=title or domain_of(url), url=url, site=self.name)

    def get_chapter_list(self, html: str, url: str, *, start: int | None = None, count: int | None = None) -> list[Chapter]:
        soup = BeautifulSoup(html, "html.parser")
        inspection = inspect_html(html, url)
        selector = inspection.chapter_list_selector or "a"
        chapters: list[Chapter] = []
        seen = set()
        for a in soup.select(selector):
            href = a.get("href") or ""
            text = a.get_text(strip=True)
            if not href:
                continue
            if not (CHAPTER_TEXT_RE.search(text) or CHAPTER_HREF_RE.search(href)):
                continue
            full = urljoin(url, href)
            if full in seen:
                continue
            seen.add(full)
            index = len(chapters) + 1
            if start and index < start:
                continue
            if count and len(chapters) >= count:
                break
            chapters.append(Chapter(index=index, title=text or f"第{index}章", url=full))
        return chapters

    def parse_chapter(self, html: str, url: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        for selector in ("script", "style", ".ad", ".ads", "iframe"):
            for tag in soup.select(selector):
                tag.decompose()
        title_node = soup.find("h1") or soup.select_one(".title, .chapter-title")
        title = title_node.get_text(strip=True) if title_node else ""
        inspection = inspect_html(html, url)
        content_node = soup.select_one(inspection.content_selector) if inspection.content_selector else None
        if content_node is None:
            content_node = self._largest_text_block(soup)
        if content_node is None:
            return title, ""
        nodes = content_node.find_all("p") or [content_node]
        lines = []
        for node in nodes:
            text = ihtml.unescape(node.get_text("", strip=True))
            if not text:
                continue
            for phrase in CLEAN_TEXT_PHRASES:
                text = text.replace(phrase, "")
            if text:
                lines.append(text)
        return title, normalize_blank_lines("\n".join(lines))

    def _largest_text_block(self, soup: BeautifulSoup):
        best = None
        best_len = 0
        for node in soup.find_all(["div", "article", "section", "main"]):
            text_len = len(node.get_text("\n", strip=True))
            if text_len > best_len:
                best = node
                best_len = text_len
        return best
