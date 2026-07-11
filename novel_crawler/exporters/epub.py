from pathlib import Path

from novel_crawler.core.models import ChapterStatus
from novel_crawler.core.storage import Storage
from novel_crawler.core.utils import ensure_dir, parse_chapter_content, safe_filename
from novel_crawler.exporters.base import register_exporter


@register_exporter("epub")
class EpubExporter:
    def __init__(self, output_dir: Path):
        self.output_dir = ensure_dir(output_dir)

    def export(self, storage: Storage, book_id: int, output: Path | None = None) -> Path:
        try:
            from ebooklib import epub
        except Exception as exc:
            raise RuntimeError("EPUB 导出需要安装 ebooklib：pip install ebooklib") from exc

        book_info = storage.get_book(book_id)
        chapters = [c for c in storage.all_chapters(book_id) if c.status == ChapterStatus.DONE and c.content_path and c.content_path.exists()]
        output = output or (self.output_dir / f"{safe_filename(book_info.title)}.epub")

        book = epub.EpubBook()
        book.set_identifier(str(book_id))
        book.set_title(book_info.title)
        book.set_language("zh")
        if book_info.author:
            book.add_author(book_info.author)

        epub_chapters = []
        for chapter in chapters:
            raw = chapter.content_path.read_text(encoding="utf-8")
            title, body = parse_chapter_content(raw)
            title = title or chapter.title
            paragraphs = "\n".join(f"<p>{_escape(line)}</p>" for line in body.splitlines() if line.strip())
            item = epub.EpubHtml(title=title, file_name=f"chap_{chapter.index:05d}.xhtml", lang="zh")
            item.content = f"<h1>{_escape(title)}</h1>\n{paragraphs}"
            book.add_item(item)
            epub_chapters.append(item)

        book.toc = tuple(epub_chapters)
        book.spine = ["nav", *epub_chapters]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub.write_epub(str(output), book)
        return output


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
