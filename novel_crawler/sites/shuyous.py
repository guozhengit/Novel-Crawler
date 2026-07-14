from __future__ import annotations

import html as ihtml
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.utils import normalize_blank_lines
from novel_crawler.sites.base import BS_PARSER, SiteAdapter, domain_of


class ShuyousAdapter(SiteAdapter):
    name = "shuyous"

    def match(self, url: str) -> bool:
        return domain_of(url).split(":", 1)[0] in {"shuyous.com", "www.shuyous.com"}

    def get_book_info(self, html: str, url: str) -> Book:
        soup = BeautifulSoup(html, BS_PARSER)
        title = self._meta(soup, "og:novel:book_name")
        author = self._meta(soup, "og:novel:author")
        if not title:
            breadcrumb = soup.select_one(".position a[href*='/book/'][href$='.html']")
            if breadcrumb:
                title = breadcrumb.get_text(strip=True)
        if not title:
            heading = soup.select_one(".chapterBox .name, h1")
            title = heading.get_text(strip=True) if heading else "书友社小说"
        return Book(title=title, author=author or None, url=url, site=self.name)

    def get_chapter_list(
        self,
        html: str,
        url: str,
        *,
        start: int | None = None,
        count: int | None = None,
    ) -> list[Chapter]:
        book_id = self._book_id(url)
        if not book_id:
            return []
        catalog_url = f"https://www.shuyous.com/book/{book_id}.html"
        catalog_html = html
        if self._chapter_number(url, book_id) is not None and self.fetcher is not None:
            catalog_html = self.fetcher.fetch_text(catalog_url, referer=url)
        soup = BeautifulSoup(catalog_html, BS_PARSER)
        found: dict[int, Chapter] = {}
        for anchor in soup.select(".chapterList .list a[href], a[href]"):
            href = str(anchor.get("href", ""))
            full = urljoin(catalog_url, href)
            number = self._chapter_number(full, book_id)
            if number is None:
                continue
            title = anchor.get_text(" ", strip=True) or f"第{number}章"
            found.setdefault(number, Chapter(index=number, title=title, url=full))
        chapters = [found[number] for number in sorted(found)]
        requested_start = start or 1
        selected = [chapter for chapter in chapters if chapter.index >= requested_start]
        return selected if count is None else selected[:count]

    def parse_chapter(self, html: str, url: str) -> tuple[str, str]:
        book_id = self._book_id(url)
        chapter_number = self._chapter_number(url, book_id) if book_id else None
        title = ""
        pages: list[str] = []
        current_html = html
        current_url = url
        seen = {url}
        for _ in range(20):
            page_title, body = self._parse_single_page(current_html)
            title = title or page_title
            if body:
                pages.append(body)
            next_url = self._next_page_url(current_html, current_url)
            if (
                not next_url
                or next_url in seen
                or self.fetcher is None
                or book_id is None
                or chapter_number is None
                or self._chapter_number(next_url, book_id) != chapter_number
            ):
                break
            seen.add(next_url)
            current_html = self.fetcher.fetch_text(next_url, referer=current_url)
            current_url = next_url
        return title, normalize_blank_lines("\n".join(pages))

    def _parse_single_page(self, html: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, BS_PARSER)
        heading = soup.select_one("h1.title, .readCon h1, h1")
        title = heading.get_text(strip=True) if heading else ""
        content = soup.select_one("#content, .readCon .content")
        if content is None:
            return title, ""
        for tag in content.select("script, style, iframe, .ad, .ads, .spe, .btnError"):
            tag.decompose()
        lines: list[str] = []
        for node in content.find_all("p") or [content]:
            text = ihtml.unescape(node.get_text(" ", strip=True))
            text = self._clean_line(text)
            if text:
                lines.append(text)
        return title, normalize_blank_lines("\n".join(lines))

    def _next_page_url(self, html: str, url: str) -> str | None:
        soup = BeautifulSoup(html, BS_PARSER)
        for selector in ("a.pageDown[href]", ".btnW a[href]"):
            for anchor in soup.select(selector):
                text = anchor.get_text(strip=True)
                if text != "下一页":
                    continue
                href = str(anchor.get("href", ""))
                if href and not href.startswith("javascript:"):
                    return urljoin(url, href)
        return None

    @staticmethod
    def _meta(soup: BeautifulSoup, property_name: str) -> str:
        node = soup.find("meta", attrs={"property": property_name})
        content = node.get("content", "") if node else ""
        return str(content).strip()

    @staticmethod
    def _clean_line(text: str) -> str:
        text = text.replace("\r", "").strip()
        if not text:
            return ""
        blocked = (
            "章节错误",
            "请记住本书首发域名",
            "http://www.shuyous.com/book/",
        )
        if any(phrase in text for phrase in blocked):
            return ""
        return text

    @staticmethod
    def _book_id(url: str) -> str | None:
        match = re.search(r"/book/(\d+)(?:[-.]|$)", url)
        return match.group(1) if match else None

    @staticmethod
    def _chapter_number(url: str | None, book_id: str) -> int | None:
        if not url:
            return None
        parts = urlsplit(url)
        if parts.hostname not in {"shuyous.com", "www.shuyous.com"}:
            return None
        match = re.fullmatch(rf"/book/{re.escape(book_id)}-(\d+)(?:-\d+)?\.html", parts.path)
        return int(match.group(1)) if match else None
