from dataclasses import dataclass, field
from pathlib import Path


class ChapterStatus:
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Chapter:
    index: int
    title: str
    url: str
    content: str = ""
    status: str = ChapterStatus.PENDING
    content_path: Path | None = None
    error: str | None = None


@dataclass
class Book:
    title: str
    url: str
    site: str
    author: str | None = None
    chapters: list[Chapter] = field(default_factory=list)
    book_id: int | None = None
