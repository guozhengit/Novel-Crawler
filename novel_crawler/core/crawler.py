import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from novel_crawler.acquisition.http import HttpPageAcquirer
from novel_crawler.core.chapter_quality import validate_parsed_chapter
from novel_crawler.core.dedup import Deduplicator, DedupResult
from novel_crawler.core.fetcher import Fetcher
from novel_crawler.core.models import Book, Chapter
from novel_crawler.core.proxy_pool import ProxyPool
from novel_crawler.core.storage import BookDeletionResult, Storage
from novel_crawler.core.title_fixer import TitleFixer, TitleFixResult
from novel_crawler.core.utils import ensure_dir, progress_bar, safe_filename
from novel_crawler.core.validator import ValidationReport, Validator
from novel_crawler.easyvoice import (
    EasyVoiceConversionResult,
    EasyVoiceExportResult,
    EasyVoiceOptions,
    export_book_for_easyvoice,
    run_easyvoice_pipeline,
)
from novel_crawler.exporters import epub as _epub_mod  # noqa: F401
from novel_crawler.exporters import markdown as _md_mod  # noqa: F401
from novel_crawler.exporters import txt as _txt_mod  # noqa: F401 触发注册
from novel_crawler.exporters.base import get_exporter
from novel_crawler.runtime.env import RuntimeContext
from novel_crawler.sites.auto import AutoAdapter
from novel_crawler.sites.base import AdapterRegistry, SiteAdapter
from novel_crawler.sites.bqg import BqgAdapter
from novel_crawler.sites.generic import GenericAdapter
from novel_crawler.sites.shuyous import ShuyousAdapter
from novel_crawler.sites.twbook import TwbookAdapter

logger = logging.getLogger(__name__)


class CrawlerService:
    def __init__(self, ctx: RuntimeContext, proxy_file: Path | None = None):
        self.ctx = ctx
        self._closeables: list[object] = []
        pool = ProxyPool.from_file(proxy_file) if proxy_file and proxy_file.exists() else None
        self.fetcher = Fetcher(proxies=ctx.proxies, proxy_pool=pool, acquirer=HttpPageAcquirer())
        self.storage = Storage(ctx.db_path, ctx.data_dir)
        self.registry = AdapterRegistry(self._load_adapters())

    def add_closeable(self, resource: object) -> None:
        self._closeables.append(resource)

    def close(self) -> None:
        failure: Exception | None = None
        for resource in reversed(self._closeables):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    failure = failure or exc
        try:
            self.storage.close()
        except Exception as exc:
            failure = failure or exc
        if failure is not None:
            raise RuntimeError("crawler_close_failed") from None

    def crawl(self, url: str, *, start: int | None = None, count: int | None = None, export: bool = True, concurrency: int = 1, max_chapters: int | None = None, chase: bool = False) -> int:
        adapter = self.registry.find(url)
        self._apply_fetch_options(adapter)
        html = self.fetcher.fetch_text(url)
        book = adapter.get_book_info(html, url)
        book_id = self.storage.upsert_book(book)
        if chase:
            chapters = self._chase_chapters(adapter, html, url, book_id, max_chapters or count)
        else:
            chapters = adapter.get_chapter_list(html, url, start=start, count=count)
            self.storage.upsert_chapters(book_id, chapters)
        pending = self.storage.pending_chapters(book_id, start=start)
        if max_chapters and not chase:
            pending = pending[:max_chapters]
        self._download_batch(book_id, book, adapter, pending, concurrency)
        if export:
            path = self.export(book_id, "txt")
            print(f"exported: {path}")
        return book_id

    def resume(self, book_id: int, *, export: bool = True, concurrency: int = 1, max_chapters: int | None = None) -> None:
        book = self.storage.get_book(book_id)
        adapter = self.registry.find(book.url)
        self._apply_fetch_options(adapter)
        pending = self.storage.pending_chapters(book_id)
        if max_chapters:
            pending = pending[:max_chapters]
        self._download_batch(book_id, book, adapter, pending, concurrency)
        if export:
            path = self.export(book_id, "txt")
            print(f"exported: {path}")

    def export_txt(self, book_id: int, output: Path | None = None) -> Path:
        return get_exporter("txt", self.ctx.output_dir).export(self.storage, book_id, output)

    def export_epub(self, book_id: int, output: Path | None = None) -> Path:
        return get_exporter("epub", self.ctx.output_dir).export(self.storage, book_id, output)

    def export_markdown(self, book_id: int, output: Path | None = None) -> Path:
        return get_exporter("md", self.ctx.output_dir).export(self.storage, book_id, output)

    def export_jsonl(self, book_id: int, output: Path | None = None) -> Path:
        return get_exporter("jsonl", self.ctx.output_dir).export(self.storage, book_id, output)

    def export(self, book_id: int, fmt: str = "txt", output: Path | None = None) -> Path:
        return get_exporter(fmt, self.ctx.output_dir).export(self.storage, book_id, output)

    def export_easyvoice(self, book_id: int, output: Path | None = None) -> EasyVoiceExportResult:
        destination = output or self.ctx.data_dir / "crawler-exports" / f"book-{book_id}.json"
        return export_book_for_easyvoice(self.storage, book_id, destination)

    def convert_easyvoice(
        self,
        book_id: int,
        *,
        export_path: Path | None = None,
        output_dir: Path | None = None,
        options: EasyVoiceOptions | None = None,
    ) -> EasyVoiceConversionResult:
        opts = options or EasyVoiceOptions()
        export_result = self.export_easyvoice(book_id, export_path)
        result = run_easyvoice_pipeline(
            input_path=export_result.export_path,
            output_dir=output_dir or self.ctx.data_dir / "novel-audio",
            project_dir=self.ctx.project_dir,
            options=opts,
        )
        if result.returncode not in {0, 2}:
            raise RuntimeError(result.stderr or result.stdout or "easyvoice_conversion_failed")
        return result

    def progress(self, book_id: int) -> dict[str, int]:
        return self.storage.progress(book_id)

    def list_books(self) -> list[dict[str, object]]:
        return self.storage.list_books()

    def delete_book(self, book_id: int) -> BookDeletionResult:
        return self.storage.delete_book(book_id)

    def validate(self, book_id: int) -> ValidationReport:
        return Validator(self.storage).validate(book_id)

    def fix_titles(self, book_id: int, dry_run: bool = False) -> TitleFixResult:
        return TitleFixer(self.storage).fix(book_id, dry_run)

    def dedup(self, book_id: int, remove: bool = False) -> DedupResult:
        dedup = Deduplicator(self.storage)
        if remove:
            return dedup.remove_duplicates(book_id)
        return dedup.scan(book_id)

    def export_all(self, fmt: str = "txt") -> list[Path]:
        books = self.list_books()
        paths = []
        for book in books:
            try:
                path = self.export(int(book["id"]), fmt)
                paths.append(path)
            except Exception as exc:
                logger.error("export failed book %s: %s", book['id'], exc)
        return paths

    def retry_all_failed(self) -> int:
        books = self.list_books()
        total_fixed = 0
        for book in books:
            failed = book.get("failed") or 0
            if failed > 0:
                logger.info("retrying book %s (%s failed)", book['id'], failed)
                self.retry_failed(int(book["id"]), export=False)
                total_fixed += 1
        return total_fixed

    def crawl_batch(self, file_path: Path, **kwargs) -> list[int]:
        """从 URL 列表文件批量抓取。每行一个 URL，# 开头为注释。"""
        urls = []
        for line in file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
        book_ids = []
        for i, url in enumerate(urls, 1):
            print(f"\n=== [{i}/{len(urls)}] {url} ===")
            try:
                book_id = self.crawl(url, **kwargs)
                book_ids.append(book_id)
            except Exception as exc:
                logger.error("crawl failed: %s", exc)
        return book_ids

    def preview_chapter(self, book_id: int, chapter_index: int, length: int = 500) -> str:
        """预览指定章节内容。"""
        chapters = self.storage.all_chapters(book_id)
        book = self.storage.get_book(book_id)
        for ch in chapters:
            if ch.index == chapter_index:
                lines = [f"书名: {book.title}", f"章节: #{ch.index} {ch.title}", f"状态: {ch.status}", f"URL: {ch.url}"]
                if ch.error:
                    lines.append(f"错误: {ch.error}")
                if ch.content_path and ch.content_path.exists():
                    raw = ch.content_path.read_text(encoding="utf-8")
                    preview = raw[:length]
                    if len(raw) > length:
                        preview += "\n\n..."
                    lines.append(f"\n--- 预览 ({len(raw)} 字符) ---\n{preview}")
                else:
                    lines.append("\n（无内容文件）")
                return "\n".join(lines)
        return f"未找到章节 #{chapter_index}"

    def stats(self) -> dict[str, object]:
        """全局下载统计。"""
        books = self.list_books()
        total_books = len(books)
        total_chapters = sum(b.get("total") or 0 for b in books)
        total_done = sum(b.get("done") or 0 for b in books)
        total_failed = sum(b.get("failed") or 0 for b in books)
        total_pending = sum(b.get("pending") or 0 for b in books)
        sites = {}
        for b in books:
            site = b.get("site", "unknown")
            sites[site] = sites.get(site, 0) + 1
        return {
            "books": total_books,
            "chapters_total": total_chapters,
            "chapters_done": total_done,
            "chapters_failed": total_failed,
            "chapters_pending": total_pending,
            "completion_rate": round(total_done / total_chapters * 100, 1) if total_chapters else 0,
            "sites": sites,
        }

    def validate_config(self, config_path: Path) -> dict[str, object]:
        """校验站点配置文件。"""
        from novel_crawler.core.config import load_config
        try:
            config = load_config(config_path)
        except Exception as exc:
            return {"valid": False, "errors": [f"配置加载失败: {exc}"]}
        errors = []
        warnings = []
        if not config.get("site"):
            errors.append("缺少 site 字段")
        if not config.get("domain"):
            errors.append("缺少 domain 字段")
        book = config.get("book", {})
        if not book.get("title_selector"):
            warnings.append("book.title_selector 未设置")
        if not book.get("chapter_list_selector"):
            warnings.append("book.chapter_list_selector 未设置")
        chapter = config.get("chapter", {})
        if not chapter.get("content_selector"):
            errors.append("chapter.content_selector 未设置")
        if not chapter.get("paragraph_selector"):
            warnings.append("chapter.paragraph_selector 未设置")
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "site": config.get("site"),
            "domain": config.get("domain"),
        }

    def logs(self, book_id: int | None = None, limit: int = 50) -> list[dict[str, object]]:
        return self.storage.recent_logs(book_id, limit)

    def report(self, book_id: int) -> str:
        book = self.storage.get_book(book_id)
        progress = self.progress(book_id)
        validation = self.validate(book_id)
        recent = self.logs(book_id, 10)
        lines = [
            f"书名: {book.title}",
            f"作者: {book.author or '-'}",
            f"站点: {book.site}",
            f"URL: {book.url}",
            f"进度: {progress}",
            "",
            validation.to_text(),
            "",
            "最近日志:",
        ]
        for row in recent:
            lines.append(f"  {row['created_at']} [{row['level']}] #{row['chapter_index']}: {row['message']}")
        return "\n".join(lines)

    def retry_failed(self, book_id: int, *, export: bool = True, concurrency: int = 1) -> None:
        self.storage.reset_failed(book_id)
        self.resume(book_id, export=export, concurrency=concurrency)

    def _apply_fetch_options(self, adapter: SiteAdapter) -> None:
        opts = getattr(adapter, "fetch_options", None)
        if opts is not None:
            self.fetcher.options = opts

    def _chase_chapters(self, adapter: SiteAdapter, html: str, url: str, book_id: int, max_chapters: int | None) -> list[Chapter]:
        """递推抓取：从当前页面开始，逐章解析并跟随"下一章"链接。"""
        discovered: list[Chapter] = []
        current_html = html
        current_url = url
        index = 1
        limit = max_chapters or 99999
        seen_urls: set[str] = {url}
        while index <= limit:
            title, body = adapter.parse_chapter(current_html, current_url)
            if not title:
                title = f"第{index}章"
            chapter = Chapter(index=index, title=title, url=current_url)
            self.storage.upsert_chapters(book_id, [chapter])
            if body.strip():
                self.storage.mark_done(
                    book_id,
                    chapter,
                    chapter.title + "\n\n" + body,
                    reject_duplicate_content=True,
                )
                self.storage.add_log(book_id, index, "info", f"chase done {index}: {title}")
                print(f"[chase {index}] done: {title}")
            else:
                self.storage.mark_failed(book_id, index, "正文为空")
                print(f"[chase {index}] failed: 正文为空")
            discovered.append(chapter)
            next_url = adapter.find_next_chapter(current_html, current_url)
            if not next_url or next_url in seen_urls:
                print(f"[chase] 到达末尾或循环，共抓取 {len(discovered)} 章")
                break
            seen_urls.add(next_url)
            index += 1
            self.fetcher.polite_sleep(index)
            current_html = self.fetcher.fetch_text(next_url, referer=current_url)
            current_url = next_url
        return discovered

    def _download_batch(self, book_id: int, book: Book, adapter: SiteAdapter, pending: list[Chapter], concurrency: int) -> None:
        total = len(pending)
        if total == 0:
            return
        workers = max(1, min(concurrency, total))
        pos = 0
        batch_size = workers
        for batch_start in range(0, total, batch_size):
            batch = pending[batch_start:batch_start + batch_size]
            results: dict[int, tuple[str | None, str, str]] = {}
            if workers == 1:
                for chapter in batch:
                    pos += 1
                    html, source = self._fetch_chapter_html(book, chapter, adapter)
                    results[chapter.index] = (html, source, "")
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    future_map = {pool.submit(self._fetch_chapter_html, book, ch, adapter): ch for ch in batch}
                    for future in as_completed(future_map):
                        chapter = future_map[future]
                        try:
                            html, source = future.result()
                        except Exception as exc:
                            html, source = None, "error"
                            results[chapter.index] = (None, "error", str(exc))
                            continue
                        results[chapter.index] = (html, source, "")
                for _chapter in batch:
                    pos += 1
            for i, chapter in enumerate(batch):
                pos_in_batch = batch_start + i + 1
                entry = results.get(chapter.index)
                if entry is None:
                    continue
                html, source, err = entry
                if err:
                    self.storage.mark_failed(book_id, chapter.index, err)
                    self.storage.add_log(book_id, chapter.index, "error", f"failed {chapter.index}: {err}")
                    print(progress_bar(pos_in_batch, total, prefix="  ") + f" failed {chapter.index}: {err}")
                else:
                    self._process_chapter(book_id, adapter, chapter, html, source, pos_in_batch, total)
            self.fetcher.polite_sleep(pos)

    def _fetch_chapter_html(self, book: Book, chapter: Chapter, adapter: SiteAdapter | None = None) -> tuple[str | None, str]:
        cached = self._load_cached_html(book, chapter.index)
        if cached is not None:
            return cached, "cache"
        html = self.fetcher.fetch_text(chapter.url, referer=book.url)
        self._cache_html(book, chapter.index, html)
        return html, "net"

    def _process_chapter(self, book_id: int, adapter: SiteAdapter, chapter: Chapter, html: str | None, source: str, pos: int, total: int) -> None:
        try:
            if html is None:
                raise RuntimeError("抓取失败")
            title, body = adapter.parse_chapter(html, chapter.url)
            validate_parsed_chapter(chapter, title, body)
            if title:
                chapter.title = title
            self.storage.mark_done(
                book_id,
                chapter,
                chapter.title + "\n\n" + body,
                reject_duplicate_content=True,
            )
            message = f"done {chapter.index}: {chapter.title} ({source})"
            self.storage.add_log(book_id, chapter.index, "info", message)
            print(progress_bar(pos, total, prefix="  ") + f" {message}")
        except Exception as exc:
            self.storage.mark_failed(book_id, chapter.index, str(exc))
            self.storage.add_log(book_id, chapter.index, "error", f"failed {chapter.index}: {exc}")
            print(progress_bar(pos, total, prefix="  ") + f" failed {chapter.index}: {exc}")

    def _cache_path(self, book: Book, index: int) -> Path:
        return self.ctx.cache_dir / book.site / safe_filename(book.title) / f"{index:05d}.html"

    def _load_cached_html(self, book: Book, index: int) -> str | None:
        path = self._cache_path(book, index)
        if path.exists() and path.stat().st_size > 0:
            import time
            age = time.time() - path.stat().st_mtime
            if age > 86400 * 7:  # 7天过期
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        return None

    def _cache_html(self, book: Book, index: int, html: str) -> Path:
        path = self._cache_path(book, index)
        ensure_dir(path.parent)
        path.write_text(html, encoding="utf-8")
        return path

    def _load_adapters(self) -> list[SiteAdapter]:
        adapters: list[SiteAdapter] = [TwbookAdapter(self.ctx.project_dir), BqgAdapter(), ShuyousAdapter()]
        for a in adapters:
            a.set_fetcher(self.fetcher)
        config_dir = self.ctx.project_dir / "novel_crawler" / "configs"
        if config_dir.exists():
            for pattern in ("*.json", "*.yaml", "*.yml"):
                for path in config_dir.glob(pattern):
                    try:
                        adapters.append(GenericAdapter(path))
                    except Exception as exc:
                        logger.warning("skip config %s: %s", path, exc)
        adapters.append(AutoAdapter())
        return adapters
