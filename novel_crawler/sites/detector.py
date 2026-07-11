import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

TITLE_SELECTORS = [
    "h1",
    ".book-title",
    ".bookname h1",
    ".info h1",
    "#info h1",
    "meta[property='og:novel:book_name']",
    "meta[property='og:title']",
]

CONTENT_SELECTORS = [
    "#content",
    ".content",
    ".chapter-content",
    ".read-content",
    ".entry-content",
    "article",
    ".text",
    "#chaptercontent",
]

CHAPTER_LIST_SELECTORS = [
    ".chapter-list a",
    ".chapters a",
    ".listmain a",
    "#list a",
    ".book-list a",
    ".catalog a",
    "a",
]

CHAPTER_TEXT_RE = re.compile(r"(第\s*[0-9一二三四五六七八九十百千万零〇两]+\s*[章节回卷集]|chapter\s*\d+)", re.I)
CHAPTER_HREF_RE = re.compile(r"(/|^)(chapter|chap|read)?[_/-]?\d+\.(html?|xhtml)$|/\d+/?$", re.I)
CLEAN_TEXT_PHRASES = ("请收藏本站", "最新网址", "手机阅读", "加入书签")
CLEAN_TEXT_PATTERNS = (
    r"请收藏本站[，,。；;、\s]*",
    r"最新网址(?:[：:]\s*\S+)?[，,。；;、\s]*",
    r"手机阅读(?:更方便)?[，,。；;、\s]*",
    r"加入书签[，,。；;、！!\s]*",
)


@dataclass
class SelectorCandidate:
    selector: str
    score: float
    sample: str = ""


@dataclass
class SiteInspection:
    title_selector: str | None = None
    content_selector: str | None = None
    chapter_list_selector: str | None = None
    title_candidates: list[SelectorCandidate] = field(default_factory=list)
    content_candidates: list[SelectorCandidate] = field(default_factory=list)
    chapter_candidates: list[SelectorCandidate] = field(default_factory=list)
    chapter_count: int = 0

    def to_config(self, site: str, domain: str) -> dict:
        return {
            "site": site,
            "domain": [domain],
            "book": {
                "title_selector": self.title_selector or "h1",
                "author_selector": ".author",
                "chapter_list_selector": self.chapter_list_selector or "a",
            },
            "chapter": {
                "title_selector": "h1",
                "content_selector": self.content_selector or "#content",
                "paragraph_selector": "p",
            },
            "clean": {
                "remove_selectors": ["script", "style", ".ad", ".ads", "iframe"],
                "remove_text_contains": list(CLEAN_TEXT_PHRASES),
            },
        }


def inspect_html(html: str, url: str) -> SiteInspection:
    soup = BeautifulSoup(html, "html.parser")
    inspection = SiteInspection()
    inspection.title_candidates = _rank_title_selectors(soup)
    inspection.content_candidates = _rank_content_selectors(soup)
    inspection.chapter_candidates = _rank_chapter_selectors(soup, url)
    if inspection.title_candidates:
        inspection.title_selector = inspection.title_candidates[0].selector
    if inspection.content_candidates:
        inspection.content_selector = inspection.content_candidates[0].selector
    if inspection.chapter_candidates:
        best = inspection.chapter_candidates[0]
        inspection.chapter_list_selector = best.selector
        inspection.chapter_count = int(best.score)
    return inspection


def _rank_title_selectors(soup: BeautifulSoup) -> list[SelectorCandidate]:
    candidates = []
    for selector in TITLE_SELECTORS:
        node = soup.select_one(selector)
        if not node:
            continue
        text = node.get("content", "") if node.name == "meta" else node.get_text(strip=True)
        if not text:
            continue
        score = min(len(text), 80)
        candidates.append(SelectorCandidate(selector, float(score), text[:80]))
    return sorted(candidates, key=lambda x: x.score, reverse=True)


def _rank_content_selectors(soup: BeautifulSoup) -> list[SelectorCandidate]:
    candidates = []
    for selector in CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if not node:
            continue
        text = node.get_text("\n", strip=True)
        if len(text) < 100:
            continue
        paragraphs = len(node.find_all("p"))
        score = len(text) + paragraphs * 100
        candidates.append(SelectorCandidate(selector, float(score), text[:120]))
    return sorted(candidates, key=lambda x: x.score, reverse=True)


def _rank_chapter_selectors(soup: BeautifulSoup, url: str) -> list[SelectorCandidate]:
    candidates = []
    for selector in CHAPTER_LIST_SELECTORS:
        links = soup.select(selector)
        valid = []
        for link in links:
            href = link.get("href") or ""
            text = link.get_text(strip=True)
            full = urljoin(url, href)
            same_site = urlparse(full).netloc == urlparse(url).netloc
            if not same_site:
                continue
            if CHAPTER_TEXT_RE.search(text) or CHAPTER_HREF_RE.search(href):
                valid.append((text, full))
        if len(valid) < 3:
            continue
        sample = " | ".join(x[0] or x[1] for x in valid[:5])
        candidates.append(SelectorCandidate(selector, float(len(valid)), sample[:160]))
    return sorted(candidates, key=lambda x: x.score, reverse=True)
