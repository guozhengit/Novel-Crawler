import re
from dataclasses import dataclass, field

from novel_crawler.core.models import ChapterStatus
from novel_crawler.core.storage import Storage
from novel_crawler.core.utils import format_chapter_content, parse_chapter_content

# 匹配 "第N章" / "第N节" / "第N回" 等
TITLE_NUM_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百千万零〇两]+)\s*[章节回卷集]", re.I)
# 纯数字
PURE_NUM_RE = re.compile(r"^\d+$")

CN_NUM_MAP = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000, "万": 10000, "两": 2,
}


def cn_to_int(text: str) -> int:
    """将中文数字转为整数。"""
    if PURE_NUM_RE.match(text):
        return int(text)
    total = 0
    current = 0
    for ch in text:
        if ch not in CN_NUM_MAP:
            return -1
        val = CN_NUM_MAP[ch]
        if val >= 10:
            if current == 0:
                current = 1
            total += current * val
            current = 0
        else:
            current = val
    total += current
    return total


@dataclass
class TitleFixResult:
    total: int = 0
    fixed: int = 0
    details: list[str] = field(default_factory=list)


class TitleFixer:
    """自动修正章节标题编号：确保每章标题中的编号与章节序号一致。"""

    def __init__(self, storage: Storage):
        self.storage = storage

    def fix(self, book_id: int, dry_run: bool = False) -> TitleFixResult:
        chapters = self.storage.all_chapters(book_id)
        result = TitleFixResult(total=len(chapters))
        for chapter in chapters:
            if chapter.status != ChapterStatus.DONE:
                continue
            if not chapter.content_path or not chapter.content_path.exists():
                continue
            raw = chapter.content_path.read_text(encoding="utf-8")
            title, body = parse_chapter_content(raw)
            new_title = self._fix_title(chapter.index, title)
            if new_title and new_title != title:
                result.fixed += 1
                detail = f"#{chapter.index}: '{title}' -> '{new_title}'"
                result.details.append(detail)
                if not dry_run:
                    chapter.title = new_title
                    self.storage.mark_done(book_id, chapter, format_chapter_content(new_title, body))
        return result

    def _fix_title(self, index: int, title: str) -> str:
        if not title:
            return f"第{index}章"
        match = TITLE_NUM_RE.search(title)
        if not match:
            return title
        current_num = cn_to_int(match.group(1))
        if current_num == index:
            return title
        # 替换编号
        return TITLE_NUM_RE.sub(f"第{index}章", title, count=1)
