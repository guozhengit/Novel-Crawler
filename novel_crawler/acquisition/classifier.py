"""Deterministic, explainable page classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Comment

from .models import PageSnapshot


class PageKind(Enum):
    BOOK_INDEX = "book_index"
    CHAPTER = "chapter"
    SEARCH_OR_LIST = "search_or_list"
    AUTH_OR_CHALLENGE = "auth_or_challenge"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Classification:
    kind: PageKind
    confidence: float
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


_CHAPTER_TITLE = re.compile(r"(?:第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章节回卷]|chapter\s+\d+)", re.I)
_CHAPTER_HREF = re.compile(r"/(?:chapter|chapters?|read)(?:/|[-_])", re.I)
_NUMERIC_CHAPTER_PATH = re.compile(r"/(?:\d+|[^/]*\d+\.html?)$", re.I)
_AUTH_TITLE = re.compile(r"(?:登录|登陆|sign[ -]?in|log[ -]?in)", re.I)
_CHALLENGE_TEXT = re.compile(r"(?:verify (?:you are )?human|just a moment|captcha|安全验证|人机验证)", re.I)
_ERROR_TITLE = re.compile(r"(?:404|not found|server error|页面不存在|访问出错)", re.I)
_SEARCH_TEXT = re.compile(r"(?:搜索结果|search results?|书库|小说列表)", re.I)
_NOISE_SELECTOR = "comments, .comments, #comments, .comment, #comment, recommendations, .recommendations, #recommendations, aside"
_CONTENT_SELECTOR = "article, [id*=content i], [class*=chapter-content i], [class*=chapter_content i], [class*=read-content i]"


class PageClassifier:
    """Classify a snapshot using stable rules in security-first precedence order."""

    def classify(self, snapshot: PageSnapshot) -> Classification:
        if snapshot.status_code >= 400:
            return Classification(PageKind.ERROR, 1.0, ("error.http_status",))

        soup = BeautifulSoup(snapshot.html, "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""

        for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
            comment.extract()
        for node in soup.select(_NOISE_SELECTOR):
            node.decompose()

        content = soup.select_one(_CONTENT_SELECTOR)
        content_text = content.get_text(" ", strip=True) if content is not None else ""
        chapter_by_title = content is not None and bool(
            _CHAPTER_TITLE.search(title) or _CHAPTER_TITLE.search(content_text)
        )
        chapter_by_url = content is not None and len(content_text) >= 20 and self._chapter_url(snapshot.final_url)

        chapter_links = {
            str(link.get("href"))
            for link in soup.find_all("a", href=True)
            if _CHAPTER_TITLE.search(link.get_text(" ", strip=True)) or self._chapter_url(str(link.get("href")))
        }
        book_index = len(chapter_links) >= 3
        substantial_content = chapter_by_title or chapter_by_url or book_index

        if self._challenge_score(soup, title) >= 2:
            return Classification(PageKind.AUTH_OR_CHALLENGE, 0.96, ("auth.challenge_signals",))
        if not substantial_content and self._login_form(soup, title):
            return Classification(PageKind.AUTH_OR_CHALLENGE, 0.98, ("auth.password_input",))
        if _ERROR_TITLE.search(title):
            return Classification(PageKind.ERROR, 0.9, ("error.title_marker",))
        if chapter_by_title:
            return Classification(PageKind.CHAPTER, 0.95, ("chapter.title_and_content",))
        if chapter_by_url:
            return Classification(PageKind.CHAPTER, 0.85, ("chapter.url_and_content",))
        if book_index:
            return Classification(PageKind.BOOK_INDEX, 0.93, ("book_index.chapter_link_cluster",))

        if _SEARCH_TEXT.search(title) or soup.select_one("form[role=search], input[type=search]") is not None:
            return Classification(PageKind.SEARCH_OR_LIST, 0.88, ("list.search_marker",))
        return Classification(PageKind.UNKNOWN, 0.0, ())

    @staticmethod
    def _chapter_url(url: str) -> bool:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        return bool(
            _CHAPTER_HREF.search(parts.path)
            or _NUMERIC_CHAPTER_PATH.search(parts.path)
            or any(key.lower() in {"cid", "chapter_id", "chapterid"} for key in query)
        )

    @staticmethod
    def _login_form(soup: BeautifulSoup, title: str) -> bool:
        for password in soup.select("form input[type=password]"):
            form = password.find_parent("form")
            if form is None or form.has_attr("hidden") or "display:none" in str(form.get("style", "")).replace(" ", ""):
                continue
            context = " ".join(
                (title, str(form.get("action", "")), str(form.get("id", "")), " ".join(form.get("class", [])))
            )
            if _AUTH_TITLE.search(context):
                return True
        return False

    @staticmethod
    def _challenge_score(soup: BeautifulSoup, title: str) -> int:
        page_text = soup.get_text(" ", strip=True)
        signals = (
            bool(_CHALLENGE_TEXT.search(f"{title} {page_text}")),
            soup.select_one("input[name*=captcha i], input[id*=captcha i]") is not None,
            soup.select_one("img[src*=captcha i], img[alt*=captcha i]") is not None,
            soup.select_one("form[action*=captcha i], form[action*=challenge i]") is not None,
        )
        return sum(signals)
