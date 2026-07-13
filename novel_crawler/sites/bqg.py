"""bqg998.cc 专用适配器：SPA 站点，API 获取目录，Playwright 渲染章节页。"""
import re

from bs4 import BeautifulSoup

from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.utils import normalize_blank_lines
from novel_crawler.sites.base import BS_PARSER, SiteAdapter, domain_of

BQG_HOST = "www.bqg998.cc"


class BqgAdapter(SiteAdapter):
    name = "bqg"
    requires_browser = True

    def match(self, url: str) -> bool:
        host = domain_of(url)
        return host == BQG_HOST or host.endswith(".bqg998.cc")˚

    def _fetch_json(self, api_url: str) -> dict:
        html = self.fetcher.fetch_text(api_url)
        import json
        return json.loads(html)

    def get_book_info(self, html: str, url: str) -> Book:
        book_id = self._book_id_from_url(url)
        if not book_id:
            return Book(title="未知", url=url, site=self.name)
        try:
            data = self._fetch_json(f"https://{BQG_HOST}/api/book?id={book_id}")
            return Book(title=data.get("title", "未知"), author=data.get("author"), url=url, site=self.name)
        except Exception:
            return Book(title=f"book_{book_id}", url=url, site=self.name)

    def get_chapter_list(self, html: str, url: str, *, start: int | None = None, count: int | None = None) -> list[Chapter]:
        book_id = self._book_id_from_url(url)
        if not book_id:
            return []
        data = self._fetch_json(f"https://{BQG_HOST}/api/booklist?id={book_id}")
        titles = data.get("list", [])
        chapters = []
        for i, title in enumerate(titles, start=1):
            if start and i < start:
                continue
            if count and len(chapters) >= count:
                break
            title = self._clean_title(title)
            chapter_url = f"https://{BQG_HOST}/#/book/{book_id}/{i}.html"
            chapters.append(Chapter(index=i, title=title, url=chapter_url))
        return chapters

    def parse_chapter(self, html: str, url: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, BS_PARSER)
        title_el = soup.find(id="title")
        title = title_el.get_text(strip=True) if title_el else ""
        content = soup.find(id="content") or soup.find("div", class_="content")
        if content is None:
            return title, ""
        for tag in content.select("script, style, .ad, .ads, iframe"):
            tag.decompose()
        lines = []
        for p in content.find_all("p"):
            text = p.get_text(strip=True)
            if text and text not in ("上一章", "目录", "下一章"):
                lines.append(text)
        if not lines:
            text = content.get_text("\n", strip=True)
            for line in text.split("\n"):
                line = line.strip()
                if line and line not in ("上一章", "目录", "下一章"):
                    lines.append(line)
        return title, normalize_blank_lines("\n".join(lines))

    def _book_id_from_url(self, url: str) -> str | None:
        m = re.search(r"book/(\d+)", url)
        return m.group(1) if m else None

    def _clean_title(self, title: str) -> str:
        return title.replace("?", "").strip()
