import html as ihtml
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.utils import normalize_blank_lines
from novel_crawler.sites.base import SiteAdapter, domain_of


class MapDecoder:
    def __init__(self, map_path: Path | None):
        self.mapping: dict[str, str] = {}
        if map_path and map_path.exists():
            self.mapping = json.loads(map_path.read_text(encoding="utf-8"))

    def decode(self, text: str) -> str:
        if not self.mapping:
            return text
        return "".join(self.mapping.get(ch, ch) for ch in text)


class TwbookAdapter(SiteAdapter):
    name = "twbook"

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.decoder = MapDecoder(project_dir / "font_decode_map.json")

    def match(self, url: str) -> bool:
        return domain_of(url).split(":", 1)[0] in {"twbook.cc", "www.twbook.cc"}

    def get_book_info(self, html: str, url: str) -> Book:
        soup = BeautifulSoup(html, "html.parser")
        title = "twbook小说"
        author = None
        h1 = soup.find(["h1", "h2"])
        if h1:
            title = self._decode(h1.get_text(strip=True)) or title
        text = self._decode(soup.get_text("\n"))
        m = re.search(r"作者\s*/\s*([^\n]+)", text)
        if m:
            author = m.group(1).strip()
        # 兼容当前书：首页文本里没有h1时，用目录标题或路径兜底
        if title == "twbook小说":
            m = re.search(r"##\s*([^\n]+)\s*章節列表", html)
            if m:
                title = self._decode(m.group(1).strip())
        return Book(title=title, author=author, url=url, site=self.name)

    def get_chapter_list(self, html: str, url: str, *, start: int | None = None, count: int | None = None) -> list[Chapter]:
        book_id = self._book_id_from_url(url)
        if not book_id:
            raise ValueError("无法从URL识别twbook书籍ID")
        requested_start = start or 1
        requested_count = count or self._chapter_count_from_text(html)
        catalog = self._catalog_links(html, url, book_id)
        if self._chapter_number(url, book_id) is None or len(catalog) >= 20:
            return self._select_range(catalog, requested_start, requested_count)
        discovery_limit = (
            requested_start + requested_count - 1 if requested_count is not None else None
        )
        discovered = self._follow_next_links(html, url, book_id, discovery_limit)
        if requested_count is not None and requested_count >= 50 and len(discovered) < requested_count:
            return self._sequential_chapters(book_id, requested_start, requested_count, url)
        return self._select_range(discovered, requested_start, requested_count)

    def parse_chapter(self, html: str, url: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.select_one("h1.imgtext") or soup.find("h1")
        title = self._decode(h1.get_text(strip=True)) if h1 else ""
        content = soup.select_one("div.content")
        if content is None:
            content = soup.select_one("#content, .chapter-content, article")
        if content is None:
            return title, ""
        for tag in content.select(".adBlock, script, style, ins, iframe"):
            tag.decompose()
        paras = []
        nodes = content.find_all("p") or [content]
        for node in nodes:
            text = ihtml.unescape(node.get_text("", strip=True))
            text = self._decode(text)
            if not text:
                continue
            if "網站即將改版" in text or "网站即将改版" in text:
                continue
            paras.append(text)
        return title, normalize_blank_lines("\n".join(paras))

    def _decode(self, text: str) -> str:
        return self.decoder.decode(text)

    def _book_id_from_url(self, url: str) -> str | None:
        m = re.search(r"twbook\.cc/(\d+)", url)
        return m.group(1) if m else None

    def _chapter_count_from_text(self, html: str) -> int | None:
        text = self._decode(BeautifulSoup(html, "html.parser").get_text("\n"))
        m = re.search(r"章節[:：]\s*(\d+)", text)
        return int(m.group(1)) if m else None

    def _catalog_links(self, html: str, base_url: str, book_id: str) -> list[Chapter]:
        soup = BeautifulSoup(html, "html.parser")
        found: dict[int, Chapter] = {}
        for anchor in soup.select("a[href]"):
            target = urljoin(base_url, str(anchor.get("href", "")))
            number = self._chapter_number(target, book_id)
            if number is None:
                continue
            title = self._decode(anchor.get_text(strip=True)) or f"第{number}章"
            found.setdefault(number, Chapter(index=number, title=title, url=target))
        return [found[number] for number in sorted(found)]

    def _follow_next_links(
        self,
        first_html: str,
        first_url: str,
        book_id: str,
        discovery_limit: int | None,
    ) -> list[Chapter]:
        current_number = self._chapter_number(first_url, book_id)
        if current_number is None:
            return []
        limit = min(discovery_limit or 10_000, 10_000)
        chapters: list[Chapter] = []
        html = first_html
        url = first_url
        seen = {url}
        while len(chapters) < limit:
            title, _body = self.parse_chapter(html, url)
            chapters.append(Chapter(current_number, title or f"第{current_number}章", url))
            next_url = self.find_next_chapter(html, url)
            next_number = self._chapter_number(next_url, book_id) if next_url else None
            if next_url is None or next_url in seen or next_number is None or next_number <= current_number:
                break
            if self.fetcher is None:
                break
            seen.add(next_url)
            html = self.fetcher.fetch_text(next_url, referer=url)
            url = next_url
            current_number = next_number
        return chapters

    @staticmethod
    def _select_range(chapters: list[Chapter], start: int, count: int | None) -> list[Chapter]:
        selected = [chapter for chapter in chapters if chapter.index >= start]
        return selected if count is None else selected[:count]

    @staticmethod
    def _sequential_chapters(book_id: str, start: int, count: int, url: str) -> list[Chapter]:
        parts = urlsplit(url)
        host = parts.netloc or "www.twbook.cc"
        scheme = parts.scheme or "https"
        return [
            Chapter(
                index=number,
                title=f"第{number}章",
                url=f"{scheme}://{host}/{book_id}/{number}.html",
            )
            for number in range(start, start + count)
        ]

    @staticmethod
    def _chapter_number(url: str | None, book_id: str) -> int | None:
        if not url:
            return None
        parts = urlsplit(url)
        if parts.hostname not in {"twbook.cc", "www.twbook.cc"}:
            return None
        match = re.fullmatch(rf"/{re.escape(book_id)}/(\d+)\.html", parts.path)
        return int(match.group(1)) if match else None
