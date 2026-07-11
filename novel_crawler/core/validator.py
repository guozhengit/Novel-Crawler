from collections import Counter
from dataclasses import dataclass, field

from novel_crawler.core.models import ChapterStatus
from novel_crawler.core.storage import Storage
from novel_crawler.core.utils import parse_chapter_content


@dataclass
class ValidationIssue:
    level: str
    code: str
    message: str


@dataclass
class ValidationReport:
    book_id: int
    total: int
    done: int
    failed: int
    pending: int
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)

    def to_text(self) -> str:
        lines = [
            f"book_id: {self.book_id}",
            f"total: {self.total}",
            f"done: {self.done}",
            f"failed: {self.failed}",
            f"pending: {self.pending}",
            f"ok: {self.ok}",
        ]
        if self.issues:
            lines.append("issues:")
            for issue in self.issues:
                lines.append(f"  [{issue.level}] {issue.code}: {issue.message}")
        return "\n".join(lines)


class Validator:
    def __init__(self, storage: Storage):
        self.storage = storage

    def validate(self, book_id: int) -> ValidationReport:
        chapters = self.storage.all_chapters(book_id)
        progress = self.storage.progress(book_id)
        report = ValidationReport(
            book_id=book_id,
            total=len(chapters),
            done=progress.get("done", 0),
            failed=progress.get("failed", 0),
            pending=progress.get("pending", 0),
        )
        if not chapters:
            report.issues.append(ValidationIssue("error", "NO_CHAPTERS", "没有章节记录"))
            return report
        if report.failed:
            report.issues.append(ValidationIssue("error", "FAILED_CHAPTERS", f"存在 {report.failed} 个失败章节"))
        if report.pending:
            report.issues.append(ValidationIssue("warning", "PENDING_CHAPTERS", f"存在 {report.pending} 个未完成章节"))

        indexes = [c.index for c in chapters]
        urls = [c.url for c in chapters]
        titles = [c.title.strip() for c in chapters if c.title.strip()]
        index_counts = Counter(indexes)
        url_counts = Counter(urls)
        title_counts = Counter(titles)
        duplicates = sorted(i for i, cnt in index_counts.items() if cnt > 1)
        if duplicates:
            report.issues.append(ValidationIssue("error", "DUPLICATE_INDEX", f"重复章节序号: {duplicates[:20]}"))
        duplicate_urls = sorted(u for u, cnt in url_counts.items() if cnt > 1)
        if duplicate_urls:
            report.issues.append(ValidationIssue("error", "DUPLICATE_URL", f"重复章节URL数量: {len(duplicate_urls)}"))
        duplicate_titles = sorted(t for t, cnt in title_counts.items() if cnt > 1)
        if duplicate_titles and len(duplicate_titles) > max(3, len(titles) // 20):
            report.issues.append(ValidationIssue("warning", "MANY_DUPLICATE_TITLES", f"疑似重复标题过多: {duplicate_titles[:20]}"))
        missing = [i for i in range(min(indexes), max(indexes) + 1) if i not in set(indexes)]
        if missing:
            report.issues.append(ValidationIssue("warning", "MISSING_INDEX", f"缺失章节序号: {missing[:30]}"))

        empty = []
        residual_hangul = []
        short = []
        for chapter in chapters:
            if chapter.status != ChapterStatus.DONE:
                continue
            if not chapter.content_path or not chapter.content_path.exists():
                empty.append(chapter.index)
                continue
            text = chapter.content_path.read_text(encoding="utf-8", errors="replace")
            _, body = parse_chapter_content(text)
            if not body:
                empty.append(chapter.index)
            elif len(body) < 100:
                short.append(chapter.index)
            if any(0xAC00 <= ord(ch) <= 0xD7A3 for ch in text):
                residual_hangul.append(chapter.index)
        if empty:
            report.issues.append(ValidationIssue("error", "EMPTY_CONTENT", f"空正文章节: {empty[:30]}"))
        if short:
            report.issues.append(ValidationIssue("warning", "SHORT_CONTENT", f"正文过短章节: {short[:30]}"))
        if residual_hangul:
            report.issues.append(ValidationIssue("warning", "RESIDUAL_OBFUSCATION", f"疑似残留混淆字符章节: {residual_hangul[:30]}"))
        return report
