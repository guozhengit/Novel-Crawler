"""Deterministic, explainable page classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

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
        if _ERROR_TITLE.search(title):
            return Classification(PageKind.ERROR, 0.9, ("error.title_marker",))
        if soup.select_one("input[type=password]") is not None:
            return Classification(PageKind.AUTH_OR_CHALLENGE, 0.98, ("auth.password_input",))
        if soup.select_one("[id*=challenge i], [class*=challenge i], [id*=captcha i], [class*=captcha i]") is not None:
            return Classification(PageKind.AUTH_OR_CHALLENGE, 0.97, ("auth.challenge_element",))
        if _CHALLENGE_TEXT.search(title):
            return Classification(PageKind.AUTH_OR_CHALLENGE, 0.95, ("auth.challenge_title",))
        if _AUTH_TITLE.search(title) and soup.find("form") is not None:
            return Classification(PageKind.AUTH_OR_CHALLENGE, 0.9, ("auth.form_title",))

        for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
            comment.extract()
        for node in soup.select(_NOISE_SELECTOR):
            node.decompose()

        content = soup.select_one(_CONTENT_SELECTOR)
        if content is not None and (_CHAPTER_TITLE.search(title) or _CHAPTER_TITLE.search(content.get_text(" ", strip=True))):
            return Classification(PageKind.CHAPTER, 0.95, ("chapter.title_and_content",))
        path = urlsplit(snapshot.final_url).path
        if content is not None and _CHAPTER_HREF.search(path):
            return Classification(PageKind.CHAPTER, 0.85, ("chapter.url_and_content",))

        chapter_links = {
            link.get("href") for link in soup.find_all("a", href=True) if _CHAPTER_HREF.search(str(link.get("href")))
        }
        if len(chapter_links) >= 3:
            return Classification(PageKind.BOOK_INDEX, 0.93, ("book_index.chapter_link_cluster",))

        if _SEARCH_TEXT.search(title) or soup.select_one("form[role=search], input[type=search]") is not None:
            return Classification(PageKind.SEARCH_OR_LIST, 0.88, ("list.search_marker",))
        return Classification(PageKind.UNKNOWN, 0.0, ())
