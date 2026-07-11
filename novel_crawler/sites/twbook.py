import html as ihtml
import json
import re
from pathlib import Path
from urllib.parse import urljoin

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
        return domain_of(url).endswith("twbook.cc")

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
        # 优先解析目录链接；如果用户指定了 start/count，或页面只解析出少量导航链接，按URL规律生成。
        soup = BeautifulSoup(html, "html.parser")
        links: list[Chapter] = []
        if start is None and count is None:
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                if re.search(r"/\d+/\d+\.html$", href):
                    title = self._decode(a.get_text(strip=True)) or f"第{len(links)+1}章"
                    links.append(Chapter(index=len(links) + 1, title=title, url=urljoin(url, href)))
            # 少量链接通常只是“开始阅读/导航”，不是真正目录
            if len(links) >= 20:
                return links

        book_id = self._book_id_from_url(url)
        if not book_id:
            raise ValueError("无法从URL识别twbook书籍ID")
        start = start or 1
        count = count or self._chapter_count_from_text(html) or 1
        return [
            Chapter(index=i, title=f"第{i}章", url=f"https://www.twbook.cc/{book_id}/{i}.html")
            for i in range(start, start + count)
        ]

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
