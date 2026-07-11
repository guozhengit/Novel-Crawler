"""内容去重：检测并标记内容完全相同或高度相似的重复章节。"""
import hashlib
from dataclasses import dataclass, field

from novel_crawler.core.models import ChapterStatus
from novel_crawler.core.storage import Storage
from novel_crawler.core.utils import parse_chapter_content


def content_hash(text: str) -> str:
    """取正文部分（跳过标题行）的 MD5。"""
    _, body = parse_chapter_content(text)
    return hashlib.md5(body.encode("utf-8")).hexdigest()


def similarity(a: str, b: str) -> float:
    """简单的 Jaccard 相似度，基于字符 bigram。"""
    def bigrams(text: str) -> set[str]:
        text = text.strip()
        if len(text) < 2:
            return {text}
        return {text[i:i+2] for i in range(len(text) - 1)}
    ba = bigrams(a)
    bb = bigrams(b)
    if not ba or not bb:
        return 0.0
    intersection = ba & bb
    union = ba | bb
    return len(intersection) / len(union)


@dataclass
class DupPair:
    index_a: int
    index_b: int
    kind: str  # "exact" or "similar"
    score: float = 1.0


@dataclass
class DedupResult:
    total: int = 0
    exact_dupes: int = 0
    similar_dupes: int = 0
    pairs: list[DupPair] = field(default_factory=list)

    @property
    def details(self) -> list[str]:
        lines = []
        for p in self.pairs:
            if p.kind == "exact":
                lines.append(f"exact: #{p.index_a} == #{p.index_b}")
            else:
                lines.append(f"similar: #{p.index_a} ~ #{p.index_b} ({p.score:.1%})")
        return lines


class Deduplicator:
    """检测重复章节：完全相同（MD5）和高度相似（Jaccard > threshold）。"""

    def __init__(self, storage: Storage, similarity_threshold: float = 0.85):
        self.storage = storage
        self.threshold = similarity_threshold

    def scan(self, book_id: int) -> DedupResult:
        chapters = self.storage.all_chapters(book_id)
        result = DedupResult(total=len(chapters))
        done = [c for c in chapters if c.status == ChapterStatus.DONE and c.content_path and c.content_path.exists()]
        hashes: dict[str, int] = {}
        bodies: list[tuple[int, str, str]] = []

        for chapter in done:
            raw = chapter.content_path.read_text(encoding="utf-8")
            _, body = parse_chapter_content(raw)
            h = content_hash(raw)
            if h in hashes:
                result.exact_dupes += 1
                result.pairs.append(DupPair(hashes[h], chapter.index, "exact"))
            else:
                hashes[h] = chapter.index
                bodies.append((chapter.index, h, body))

        for i, (idx_a, _, body_a) in enumerate(bodies):
            for idx_b, _, body_b in bodies[i+1:]:
                sim = similarity(body_a, body_b)
                if sim >= self.threshold:
                    result.similar_dupes += 1
                    result.pairs.append(DupPair(idx_a, idx_b, "similar", sim))

        return result

    def remove_duplicates(self, book_id: int) -> DedupResult:
        """将重复章节标记为 failed（保留第一个出现的）。"""
        result = self.scan(book_id)
        for pair in result.pairs:
            if pair.kind == "exact":
                self.storage.mark_failed(book_id, pair.index_b, "duplicate content")
        return result
