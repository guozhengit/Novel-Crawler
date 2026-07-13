import json
from pathlib import Path

import pytest

from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.storage import Storage
from novel_crawler.easyvoice import (
    EasyVoiceOptions,
    export_book_for_easyvoice,
    normalize_easyvoice_base_url,
    run_easyvoice_pipeline,
)


def test_export_book_for_easyvoice_writes_stable_exchange_json(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "crawler.db", tmp_path / "data")
    try:
        book_id = storage.upsert_book(Book("测试书", "https://example.test/book", "fixture", author="作者"))
        first = Chapter(1, "第一章", "https://example.test/1")
        second = Chapter(2, "第二章", "https://example.test/2")
        storage.upsert_chapters(book_id, [first, second])
        storage.mark_done(book_id, first, "第一章\n\n正文内容足够长。")

        result = export_book_for_easyvoice(storage, book_id, tmp_path / "exports" / "book.json")
    finally:
        storage.close()

    payload = json.loads(result.export_path.read_text(encoding="utf-8"))
    assert result.chapter_count == 1
    assert payload == {
        "book": {"id": f"book-{book_id}", "title": "测试书", "author": "作者"},
        "chapters": [
            {
                "id": f"book-{book_id}-chapter-1",
                "number": 1,
                "title": "第一章",
                "content": "正文内容足够长。",
            }
        ],
    }


def test_export_book_for_easyvoice_rejects_book_without_completed_chapters(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "crawler.db", tmp_path / "data")
    try:
        book_id = storage.upsert_book(Book("测试书", "https://example.test/book", "fixture"))
        storage.upsert_chapters(book_id, [Chapter(1, "第一章", "https://example.test/1")])
        with pytest.raises(ValueError, match="easyvoice_no_completed_chapters"):
            export_book_for_easyvoice(storage, book_id, tmp_path / "book.json")
    finally:
        storage.close()


def test_run_easyvoice_pipeline_requires_local_script(tmp_path: Path) -> None:
    exchange = tmp_path / "book.json"
    exchange.write_text('{"book":{"id":"book-1","title":"测试"},"chapters":[]}', encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="easyvoice pipeline not found"):
        run_easyvoice_pipeline(
            input_path=exchange,
            output_dir=tmp_path / "audio",
            project_dir=tmp_path,
            options=EasyVoiceOptions(),
        )


def test_normalize_easyvoice_base_url_accepts_generate_page_url() -> None:
    assert normalize_easyvoice_base_url("http://localhost:9549/generate") == "http://localhost:9549"
    assert normalize_easyvoice_base_url("http://localhost:9549") == "http://localhost:9549"
