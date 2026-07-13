from __future__ import annotations

import re
from urllib.parse import urlsplit

from novel_crawler.core.domains import canonical_domain
from novel_crawler.core.models import Chapter
from novel_crawler.core.title_fixer import cn_to_int
from novel_crawler.core.url_paths import canonical_path

_NUMBERED_TITLE = re.compile(r"^第\s*([0-9零〇一二三四五六七八九十百千万两]+)\s*[章节回]")


class ChapterQualityError(ValueError):
    pass


def validate_parsed_chapter(chapter: Chapter, title: str, body: str) -> None:
    if not body.strip():
        raise ChapterQualityError("chapter_content_empty")
    match = _NUMBERED_TITLE.search(title.strip())
    if match and chapter.title.strip() in {"", f"第{chapter.index}章"}:
        raw = match.group(1)
        number = int(raw) if raw.isdigit() else cn_to_int(raw)
        if number is not None and number != chapter.index:
            raise ChapterQualityError("chapter_title_mismatch")


def validate_final_url(expected: str, final: str) -> None:
    expected_parts = urlsplit(expected)
    final_parts = urlsplit(final)
    if (
        canonical_domain(expected_parts.hostname or "") != canonical_domain(final_parts.hostname or "")
        or canonical_path(expected_parts.path or "/") != canonical_path(final_parts.path or "/")
        or expected_parts.query != final_parts.query
    ):
        raise ChapterQualityError("chapter_redirect_mismatch")


__all__ = ["ChapterQualityError", "validate_final_url", "validate_parsed_chapter"]
