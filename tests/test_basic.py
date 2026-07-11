"""基础单元测试，不依赖网络请求。"""
import json

import pytest

from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.storage import Storage
from novel_crawler.core.utils import normalize_blank_lines, progress_bar, safe_filename
from novel_crawler.core.validator import Validator
from novel_crawler.sites.detector import inspect_html


class TestUtils:
    def test_safe_filename_basic(self):
        assert safe_filename("hello") == "hello"

    def test_safe_filename_strips_invalid(self):
        result = safe_filename('a/b\\c:d*e?f<g>h|i')
        assert "/" not in result
        assert "\\" not in result
        assert ":" not in result

    def test_safe_filename_empty(self):
        assert safe_filename("") == "untitled"

    def test_normalize_blank_lines(self):
        text = "line1\n\n\n\nline2\n\n\n\n\nline3"
        result = normalize_blank_lines(text)
        assert result == "line1\n\nline2\n\nline3"

    def test_normalize_blank_lines_strips(self):
        text = "  line1  \n  line2  "
        result = normalize_blank_lines(text)
        assert result == "line1\nline2"


class TestStorage:
    @pytest.fixture
    def storage(self, tmp_path):
        return Storage(tmp_path / "test.db", tmp_path / "data")

    def test_upsert_book(self, storage):
        book = Book(title="测试书", url="https://example.com/book/1", site="test")
        book_id = storage.upsert_book(book)
        assert book_id > 0
        assert book.book_id == book_id

    def test_upsert_chapters(self, storage):
        book = Book(title="测试书", url="https://example.com/book/1", site="test")
        book_id = storage.upsert_book(book)
        chapters = [
            Chapter(index=1, title="第1章", url="https://example.com/book/1/1.html"),
            Chapter(index=2, title="第2章", url="https://example.com/book/1/2.html"),
        ]
        storage.upsert_chapters(book_id, chapters)
        pending = storage.pending_chapters(book_id)
        assert len(pending) == 2

    def test_mark_done(self, storage):
        book = Book(title="测试书", url="https://example.com/book/1", site="test")
        book_id = storage.upsert_book(book)
        chapter = Chapter(index=1, title="第1章", url="https://example.com/book/1/1.html")
        storage.upsert_chapters(book_id, [chapter])
        storage.mark_done(book_id, chapter, "第1章\n\n正文内容")
        pending = storage.pending_chapters(book_id)
        assert len(pending) == 0
        progress = storage.progress(book_id)
        assert progress["done"] == 1

    def test_mark_failed(self, storage):
        book = Book(title="测试书", url="https://example.com/book/1", site="test")
        book_id = storage.upsert_book(book)
        chapter = Chapter(index=1, title="第1章", url="https://example.com/book/1/1.html")
        storage.upsert_chapters(book_id, [chapter])
        storage.mark_failed(book_id, 1, "连接超时")
        progress = storage.progress(book_id)
        assert progress["failed"] == 1


class TestValidator:
    @pytest.fixture
    def storage(self, tmp_path):
        return Storage(tmp_path / "test.db", tmp_path / "data")

    def test_validate_empty(self, storage):
        book = Book(title="空书", url="https://example.com/book/1", site="test")
        book_id = storage.upsert_book(book)
        report = Validator(storage).validate(book_id)
        assert not report.ok

    def test_validate_done(self, storage):
        book = Book(title="测试书", url="https://example.com/book/1", site="test")
        book_id = storage.upsert_book(book)
        chapter = Chapter(index=1, title="第1章", url="https://example.com/book/1/1.html")
        storage.upsert_chapters(book_id, [chapter])
        storage.mark_done(book_id, chapter, "第1章\n\n" + "正文内容" * 50)
        report = Validator(storage).validate(book_id)
        assert report.ok


class TestDetector:
    def test_inspect_basic_html(self):
        html = """
        <html>
        <head><title>测试小说</title></head>
        <body>
            <h1>测试小说</h1>
            <div class="chapter-list">
                <a href="/book/1/chapter1.html">第一章 开始</a>
                <a href="/book/1/chapter2.html">第二章 发展</a>
                <a href="/book/1/chapter3.html">第三章 高潮</a>
                <a href="/book/1/chapter4.html">第四章 结局</a>
            </div>
        </body>
        </html>
        """
        result = inspect_html(html, "https://example.com/book/1")
        assert result.title_selector is not None
        assert len(result.chapter_candidates) > 0
        assert result.chapter_count >= 4

    def test_inspect_content_detection(self):
        html = """
        <html><body>
        <h1>第一章</h1>
        <div id="content">
            <p>""" + "这是一段很长的正文内容。" * 20 + """</p>
        </div>
        </body></html>
        """
        result = inspect_html(html, "https://example.com/book/1/chapter1.html")
        assert result.content_selector is not None
        assert len(result.content_candidates) > 0


class TestNextChapter:
    def test_find_next_chapter(self):
        from novel_crawler.sites.auto import AutoAdapter
        html = """
        <html><body>
        <a href="/book/1/1.html">上一章</a>
        <a href="/book/1/3.html">下一章</a>
        </body></html>
        """
        adapter = AutoAdapter()
        next_url = adapter.find_next_chapter(html, "https://example.com/book/1/2.html")
        assert next_url == "https://example.com/book/1/3.html"

    def test_find_prev_chapter(self):
        from novel_crawler.sites.auto import AutoAdapter
        html = """
        <html><body>
        <a href="/book/1/1.html">上一章</a>
        <a href="/book/1/3.html">下一章</a>
        </body></html>
        """
        adapter = AutoAdapter()
        prev_url = adapter.find_prev_chapter(html, "https://example.com/book/1/2.html")
        assert prev_url == "https://example.com/book/1/1.html"

    def test_find_next_chapter_none(self):
        from novel_crawler.sites.auto import AutoAdapter
        html = "<html><body><a href='/'>首页</a></body></html>"
        adapter = AutoAdapter()
        assert adapter.find_next_chapter(html, "https://example.com/book/1/1.html") is None


class TestProgressBar:
    def test_progress_bar_zero(self):
        bar = progress_bar(0, 10)
        assert "0%" in bar
        assert "(0/10)" in bar

    def test_progress_bar_half(self):
        bar = progress_bar(5, 10)
        assert "50%" in bar
        assert "(5/10)" in bar

    def test_progress_bar_full(self):
        bar = progress_bar(10, 10)
        assert "100%" in bar

    def test_progress_bar_zero_total(self):
        bar = progress_bar(0, 0)
        assert "?" in bar


class TestListBooks:
    @pytest.fixture
    def storage(self, tmp_path):
        return Storage(tmp_path / "test.db", tmp_path / "data")

    def test_list_books_empty(self, storage):
        books = storage.list_books()
        assert len(books) == 0

    def test_list_books_with_data(self, storage):
        for i in range(3):
            book = Book(title=f"书{i}", url=f"https://example.com/{i}", site="test")
            book_id = storage.upsert_book(book)
            chapters = [Chapter(index=1, title="第1章", url=f"https://example.com/{i}/1.html")]
            storage.upsert_chapters(book_id, chapters)
        books = storage.list_books()
        assert len(books) == 3
        assert books[0]["title"] == "书0"
        assert books[2]["title"] == "书2"

    def test_delete_book(self, storage):
        book = Book(title="待删除", url="https://example.com/del", site="test")
        book_id = storage.upsert_book(book)
        storage.delete_book(book_id)
        books = storage.list_books()
        assert len(books) == 0


class TestGenericAdapter:
    def test_load_config_and_match(self, tmp_path):
        config = {
            "site": "test_site",
            "domain": ["test.example.com"],
            "book": {"title_selector": "h1", "chapter_list_selector": ".chapters a"},
            "chapter": {"title_selector": "h1", "content_selector": "#content", "paragraph_selector": "p"},
            "clean": {"remove_selectors": ["script"]},
        }
        config_path = tmp_path / "test_site.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        from novel_crawler.sites.generic import GenericAdapter
        adapter = GenericAdapter(config_path)
        assert adapter.match("https://test.example.com/book/1")
        assert not adapter.match("https://other.com/book/1")

    def test_fetch_options_from_config(self, tmp_path):
        config = {
            "site": "test_rate",
            "domain": ["rate.example.com"],
            "book": {"title_selector": "h1"},
            "chapter": {"content_selector": "#content"},
            "request": {"delay_min": 5, "delay_max": 10, "retries": 3},
        }
        config_path = tmp_path / "test_rate.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        from novel_crawler.sites.generic import GenericAdapter
        adapter = GenericAdapter(config_path)
        opts = adapter.fetch_options
        assert opts is not None
        assert opts.delay_min == 5
        assert opts.delay_max == 10
        assert opts.retries == 3

    def test_fetch_options_none_without_request(self, tmp_path):
        config = {"site": "no_rate", "domain": ["norate.example.com"], "book": {}, "chapter": {}}
        config_path = tmp_path / "no_rate.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        from novel_crawler.sites.generic import GenericAdapter
        adapter = GenericAdapter(config_path)
        assert adapter.fetch_options is None


class TestExporters:
    @pytest.fixture
    def storage_with_book(self, tmp_path):
        storage = Storage(tmp_path / "test.db", tmp_path / "data")
        book = Book(title="导出测试", url="https://example.com/book/1", site="test")
        book_id = storage.upsert_book(book)
        for i in range(1, 4):
            chapter = Chapter(index=i, title=f"第{i}章", url=f"https://example.com/book/1/{i}.html")
            storage.upsert_chapters(book_id, [chapter])
            storage.mark_done(book_id, chapter, f"第{i}章\n\n这是第{i}章的正文内容，足够长用于测试。" * 10)
        return storage, book_id, tmp_path

    def test_txt_export(self, storage_with_book):
        from novel_crawler.exporters.txt import TxtExporter
        storage, book_id, tmp_path = storage_with_book
        exporter = TxtExporter(tmp_path / "output")
        path = exporter.export(storage, book_id)
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "导出测试" in text
        assert "第1章" in text

    def test_markdown_export(self, storage_with_book):
        from novel_crawler.exporters.markdown import MarkdownExporter
        storage, book_id, tmp_path = storage_with_book
        exporter = MarkdownExporter(tmp_path / "output")
        path = exporter.export(storage, book_id)
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "# 导出测试" in text
        assert "## 第1章" in text

    def test_jsonl_export(self, storage_with_book):
        import json as json_mod

        from novel_crawler.exporters.markdown import JsonlExporter
        storage, book_id, tmp_path = storage_with_book
        exporter = JsonlExporter(tmp_path / "output")
        path = exporter.export(storage, book_id)
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        first = json_mod.loads(lines[0])
        assert first["type"] == "book"
        assert first["title"] == "导出测试"
        second = json_mod.loads(lines[1])
        assert second["type"] == "chapter"


class TestProxyPool:
    def test_empty_pool(self):
        from novel_crawler.core.proxy_pool import ProxyPool
        pool = ProxyPool()
        assert pool.next() is None
        assert pool.alive_count() == 0

    def test_round_robin(self):
        from novel_crawler.core.proxy_pool import ProxyPool
        pool = ProxyPool(["http://1.1.1.1:8080", "http://2.2.2.2:8080"])
        p1 = pool.next()
        p2 = pool.next()
        p3 = pool.next()
        assert p1["http"] == "http://1.1.1.1:8080"
        assert p2["http"] == "http://2.2.2.2:8080"
        assert p3["http"] == "http://1.1.1.1:8080"

    def test_fail_and_revive(self):
        from novel_crawler.core.proxy_pool import ProxyPool
        pool = ProxyPool(["http://bad:8080"])
        pool.entries[0].max_fails = 2
        proxy = pool.next()
        pool.record_fail(proxy)
        pool.record_fail(proxy)
        assert pool.alive_count() == 0
        pool.reset_all()
        assert pool.alive_count() == 1

    def test_from_file(self, tmp_path):
        from novel_crawler.core.proxy_pool import ProxyPool
        path = tmp_path / "proxies.txt"
        path.write_text("# comment\nhttp://1.1.1.1:8080\nhttp://2.2.2.2:8080\n", encoding="utf-8")
        pool = ProxyPool.from_file(path)
        assert len(pool.entries) == 2


class TestTitleFixer:
    def test_cn_to_int(self):
        from novel_crawler.core.title_fixer import cn_to_int
        assert cn_to_int("123") == 123
        assert cn_to_int("一") == 1
        assert cn_to_int("十") == 10
        assert cn_to_int("二十") == 20
        assert cn_to_int("一百二十三") == 123
        assert cn_to_int("两千零一") == 2001

    def test_fix_titles_no_change(self, tmp_path):
        from novel_crawler.core.title_fixer import TitleFixer
        storage = Storage(tmp_path / "test.db", tmp_path / "data")
        book = Book(title="测试", url="https://example.com/1", site="test")
        book_id = storage.upsert_book(book)
        for i in range(1, 4):
            ch = Chapter(index=i, title=f"第{i}章 测试", url=f"https://example.com/1/{i}.html")
            storage.upsert_chapters(book_id, [ch])
            storage.mark_done(book_id, ch, f"第{i}章 测试\n\n正文内容")
        result = TitleFixer(storage).fix(book_id, dry_run=True)
        assert result.fixed == 0

    def test_fix_titles_mismatch(self, tmp_path):
        from novel_crawler.core.title_fixer import TitleFixer
        storage = Storage(tmp_path / "test.db", tmp_path / "data")
        book = Book(title="测试", url="https://example.com/1", site="test")
        book_id = storage.upsert_book(book)
        # 故意写错编号：第1章写成第5章
        ch = Chapter(index=1, title="第5章 错误", url="https://example.com/1/1.html")
        storage.upsert_chapters(book_id, [ch])
        storage.mark_done(book_id, ch, "第5章 错误\n\n正文内容")
        result = TitleFixer(storage).fix(book_id, dry_run=True)
        assert result.fixed == 1
        assert "第1章" in result.details[0]


class TestDedup:
    def test_no_duplicates(self, tmp_path):
        from novel_crawler.core.dedup import Deduplicator
        storage = Storage(tmp_path / "test.db", tmp_path / "data")
        book = Book(title="测试", url="https://example.com/1", site="test")
        book_id = storage.upsert_book(book)
        for i in range(1, 4):
            ch = Chapter(index=i, title=f"第{i}章", url=f"https://example.com/1/{i}.html")
            storage.upsert_chapters(book_id, [ch])
            storage.mark_done(book_id, ch, f"第{i}章\n\n第{i}章的独特内容各不相同")
        result = Deduplicator(storage).scan(book_id)
        assert result.exact_dupes == 0

    def test_exact_duplicates(self, tmp_path):
        from novel_crawler.core.dedup import Deduplicator
        storage = Storage(tmp_path / "test.db", tmp_path / "data")
        book = Book(title="测试", url="https://example.com/1", site="test")
        book_id = storage.upsert_book(book)
        for i in range(1, 4):
            ch = Chapter(index=i, title=f"第{i}章", url=f"https://example.com/1/{i}.html")
            storage.upsert_chapters(book_id, [ch])
            storage.mark_done(book_id, ch, f"第{i}章\n\n完全相同的重复正文内容")
        result = Deduplicator(storage).scan(book_id)
        assert result.exact_dupes == 2

    def test_similarity(self):
        from novel_crawler.core.dedup import similarity
        assert similarity("今天天气真好", "今天天气真好") == 1.0
        assert similarity("完全不同的文字", "毫无关联的内容") < 0.3
        assert 0.0 <= similarity("", "") <= 1.0
