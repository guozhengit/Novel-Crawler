from pathlib import Path

from novel_crawler.core.models import ChapterStatus
from novel_crawler.core.storage import Storage
from novel_crawler.core.utils import ensure_dir, parse_chapter_content, safe_filename
from novel_crawler.exporters.base import register_exporter


@register_exporter("txt")
class TxtExporter:
    def __init__(self, output_dir: Path):
        self.output_dir = ensure_dir(output_dir)

    def export(self, storage: Storage, book_id: int, output: Path | None = None) -> Path:
        book = storage.get_book(book_id)
        chapters = storage.all_chapters(book_id)
        output = output or (self.output_dir / f"{safe_filename(book.title)}.txt")
        with output.open("w", encoding="utf-8") as f:
            f.write(book.title + "\n")
            if book.author:
                f.write(f"作者：{book.author}\n")
            f.write("\n")
            for chapter in chapters:
                if chapter.status != ChapterStatus.DONE or not chapter.content_path or not chapter.content_path.exists():
                    continue
                content = chapter.content_path.read_text(encoding="utf-8").strip()
                title, body = parse_chapter_content(content)
                f.write((title or chapter.title).strip() + "\n\n")
                f.write((body or content).strip() + "\n\n\n")
        return output
