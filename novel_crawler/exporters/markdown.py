import json
from pathlib import Path

from novel_crawler.core.models import ChapterStatus
from novel_crawler.core.storage import Storage
from novel_crawler.core.utils import ensure_dir, parse_chapter_content, safe_filename
from novel_crawler.exporters.base import register_exporter


@register_exporter("md")
class MarkdownExporter:
    def __init__(self, output_dir: Path):
        self.output_dir = ensure_dir(output_dir)

    def export(self, storage: Storage, book_id: int, output: Path | None = None) -> Path:
        book = storage.get_book(book_id)
        chapters = storage.all_chapters(book_id)
        output = output or (self.output_dir / f"{safe_filename(book.title)}.md")
        with output.open("w", encoding="utf-8") as f:
            f.write(f"# {book.title}\n\n")
            if book.author:
                f.write(f"**作者**：{book.author}\n\n")
            f.write(f"**站点**：{book.site}\n\n---\n\n")
            for chapter in chapters:
                if chapter.status != ChapterStatus.DONE or not chapter.content_path or not chapter.content_path.exists():
                    continue
                raw = chapter.content_path.read_text(encoding="utf-8").strip()
                title, body = parse_chapter_content(raw)
                f.write(f"## {title or chapter.title}\n\n")
                for line in body.splitlines():
                    line = line.strip()
                    if line:
                        f.write(f"{line}\n\n")
        return output


@register_exporter("jsonl")
class JsonlExporter:
    def __init__(self, output_dir: Path):
        self.output_dir = ensure_dir(output_dir)

    def export(self, storage: Storage, book_id: int, output: Path | None = None) -> Path:
        book = storage.get_book(book_id)
        chapters = storage.all_chapters(book_id)
        output = output or (self.output_dir / f"{safe_filename(book.title)}.jsonl")
        with output.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "book", "title": book.title, "author": book.author, "site": book.site, "url": book.url}, ensure_ascii=False) + "\n")
            for chapter in chapters:
                if chapter.status != ChapterStatus.DONE or not chapter.content_path or not chapter.content_path.exists():
                    continue
                raw = chapter.content_path.read_text(encoding="utf-8").strip()
                title, body = parse_chapter_content(raw)
                f.write(json.dumps({
                    "type": "chapter",
                    "index": chapter.index,
                    "title": title or chapter.title,
                    "url": chapter.url,
                    "content": body.strip(),
                }, ensure_ascii=False) + "\n")
        return output
