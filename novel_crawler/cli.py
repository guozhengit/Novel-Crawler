from __future__ import annotations

import argparse
import logging
from pathlib import Path

from novel_crawler.core.crawler import CrawlerService
from novel_crawler.runtime.env import create_runtime_context, format_runtime_report

LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-crawler", description="通用小说爬虫系统")
    parser.add_argument("--data-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("env", help="显示运行环境检测报告")

    sub.add_parser("books", help="列出所有已抓取的小说")
    delete = sub.add_parser("delete", help="删除一本书及其所有数据")
    delete.add_argument("book_id", type=int)

    crawl = sub.add_parser("crawl", help="抓取小说")
    crawl.add_argument("url")
    crawl.add_argument("--start", type=int, default=None, help="起始章节序号")
    crawl.add_argument("--count", type=int, default=None, help="下载章节数量")
    crawl.add_argument("--no-export", action="store_true", help="只下载，不导出TXT")
    crawl.add_argument("--concurrency", type=int, default=1, help="并发抓取数（默认1）")
    crawl.add_argument("--max-chapters", type=int, default=None, help="本次最多下载章节数（暂停控制）")
    crawl.add_argument("--chase", action="store_true", help="递推抓取模式：从首章逐章跟随下一章链接")
    crawl.add_argument("--proxy-file", type=Path, default=None, help="代理列表文件（每行一个代理URL）")

    inspect = sub.add_parser("inspect", help="探测未知小说站点并输出配置草案")
    inspect.add_argument("url")
    inspect.add_argument("--save", type=Path, default=None, help="保存配置草案到 JSON 文件")

    wizard = sub.add_parser("wizard", help="交互式站点配置向导：探测→验证首章→保存")
    wizard.add_argument("url")
    wizard.add_argument("--save", type=Path, default=None, help="保存配置到文件（默认 configs/<domain>.json）")
    wizard.add_argument("--sample-url", type=str, default=None, help="用于验证正文解析的章节URL")

    resume = sub.add_parser("resume", help="继续未完成任务")
    resume.add_argument("book_id", type=int)
    resume.add_argument("--no-export", action="store_true")
    resume.add_argument("--concurrency", type=int, default=1, help="并发抓取数（默认1）")
    resume.add_argument("--max-chapters", type=int, default=None, help="本次最多下载章节数（暂停控制）")

    progress = sub.add_parser("progress", help="查看进度")
    progress.add_argument("book_id", type=int)

    validate = sub.add_parser("validate", help="校验抓取质量")
    validate.add_argument("book_id", type=int)

    logs = sub.add_parser("logs", help="查看最近任务日志")
    logs.add_argument("--book-id", type=int, default=None)
    logs.add_argument("--limit", type=int, default=30)

    report = sub.add_parser("report", help="生成任务报告")
    report.add_argument("book_id", type=int)

    retry_failed = sub.add_parser("retry-failed", help="重试失败章节")
    retry_failed.add_argument("book_id", type=int)
    retry_failed.add_argument("--no-export", action="store_true")
    retry_failed.add_argument("--concurrency", type=int, default=1, help="并发抓取数（默认1）")

    export = sub.add_parser("export", help="导出文件")
    export.add_argument("book_id", type=int)
    export.add_argument("--format", choices=["txt", "epub", "md", "jsonl"], default="txt")
    export.add_argument("--output", type=Path, default=None)

    decode_font = sub.add_parser("decode-font", help="根据系统字体破解混淆字体映射")
    decode_font.add_argument("font", type=Path)
    decode_font.add_argument("--output", type=Path, default=Path("font_decode_map.json"))

    web = sub.add_parser("web", help="启动 Web UI 管理界面")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)

    fix_titles = sub.add_parser("fix-titles", help="自动修正章节标题编号")
    fix_titles.add_argument("book_id", type=int)

    dedup = sub.add_parser("dedup", help="检测/去除重复章节")
    dedup.add_argument("book_id", type=int)
    dedup.add_argument("--remove", action="store_true", help="将重复章节标记为失败")

    export_all = sub.add_parser("export-all", help="批量导出所有书籍")
    export_all.add_argument("--format", choices=["txt", "epub", "md", "jsonl"], default="txt")

    sub.add_parser("retry-all", help="重试所有书籍的失败章节")

    crawl_batch = sub.add_parser("crawl-batch", help="从URL列表文件批量抓取")
    crawl_batch.add_argument("file", type=Path, help="URL列表文件（每行一个URL，#开头为注释）")
    crawl_batch.add_argument("--concurrency", type=int, default=1)
    crawl_batch.add_argument("--max-chapters", type=int, default=None)

    preview = sub.add_parser("preview", help="预览章节内容")
    preview.add_argument("book_id", type=int)
    preview.add_argument("chapter_index", type=int)
    preview.add_argument("--length", type=int, default=500, help="预览字符数")

    sub.add_parser("stats", help="全局下载统计")

    validate_config = sub.add_parser("validate-config", help="校验站点配置文件")
    validate_config.add_argument("config", type=Path)

    return parser


def main(argv: list[str] | None = None, project_dir: Path | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    args = build_parser().parse_args(argv)
    ctx = create_runtime_context((project_dir or Path.cwd()).resolve(), args.data_dir)
    if args.command == "env":
        print(format_runtime_report(ctx))
        return 0

    service = CrawlerService(ctx, proxy_file=getattr(args, "proxy_file", None))
    if args.command == "books":
        rows = service.list_books()
        if not rows:
            print("暂无书籍记录")
        else:
            print(f"{'ID':>4}  {'标题':<24}  {'站点':<10}  {'进度':>12}  URL")
            print("-" * 90)
            for row in rows:
                done = row.get("done") or 0
                total = row.get("total") or 0
                pct = f"{done}/{total}" if total else "-"
                print(f"{row['id']:>4}  {str(row['title'])[:24]:<24}  {str(row['site']):<10}  {pct:>12}  {row['url']}")
    elif args.command == "delete":
        service.delete_book(args.book_id)
        print(f"deleted book_id: {args.book_id}")
    elif args.command == "crawl":
        book_id = service.crawl(args.url, start=args.start, count=args.count, export=not args.no_export, concurrency=args.concurrency, max_chapters=args.max_chapters, chase=args.chase)
        print(f"book_id: {book_id}")
    elif args.command == "inspect":
        import json
        from urllib.parse import urlparse

        from novel_crawler.sites.detector import inspect_html
        html = service.fetcher.fetch_text(args.url)
        result = inspect_html(html, args.url)
        domain = urlparse(args.url).netloc
        config = result.to_config(domain.replace(".", "_"), domain)
        print("Title candidates:")
        for item in result.title_candidates[:5]:
            print(f"  {item.selector}: {item.sample}")
        print("Content candidates:")
        for item in result.content_candidates[:5]:
            print(f"  {item.selector}: score={int(item.score)} sample={item.sample}")
        print("Chapter candidates:")
        for item in result.chapter_candidates[:5]:
            print(f"  {item.selector}: count={int(item.score)} sample={item.sample}")
        text = json.dumps(config, ensure_ascii=False, indent=2)
        print("Config draft:")
        print(text)
        if args.save:
            args.save.write_text(text + "\n", encoding="utf-8")
            print(f"saved: {args.save}")
    elif args.command == "wizard":
        import json
        from urllib.parse import urlparse

        from novel_crawler.sites.detector import inspect_html
        from novel_crawler.sites.generic import GenericAdapter
        html = service.fetcher.fetch_text(args.url)
        result = inspect_html(html, args.url)
        domain = urlparse(args.url).netloc
        site_name = domain.replace(".", "_")
        config = result.to_config(site_name, domain)
        print("=== 站点探测结果 ===")
        print(f"书名选择器: {result.title_selector or '未检测到'}")
        print(f"正文选择器: {result.content_selector or '未检测到'}")
        print(f"章节列表选择器: {result.chapter_list_selector or '未检测到'}")
        print(f"章节链接数: {result.chapter_count}")
        sample_url = args.sample_url
        if not sample_url and result.chapter_candidates:
            from urllib.parse import urljoin

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            links = soup.select(result.chapter_list_selector)
            for link in links:
                href = link.get("href")
                if href:
                    sample_url = urljoin(args.url, href)
                    break
        if sample_url:
            print(f"\n=== 验证章节页: {sample_url} ===")
            try:
                chapter_html = service.fetcher.fetch_text(sample_url, referer=args.url)
                import tempfile
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
                    json.dump(config, tmp, ensure_ascii=False)
                    tmp_path = Path(tmp.name)
                adapter = GenericAdapter(tmp_path)
                title, body = adapter.parse_chapter(chapter_html, sample_url)
                print(f"标题: {title}")
                print(f"正文字数: {len(body)}")
                print(f"正文预览: {body[:200]}...")
                if len(body) < 100:
                    print("\n警告: 正文过短，配置可能需要调整")
                else:
                    print("\n验证通过!")
                tmp_path.unlink(missing_ok=True)
            except Exception as exc:
                print(f"验证失败: {exc}")
        else:
            print("\n未找到章节链接，跳过验证")
        save_path = args.save or (Path("novel_crawler/configs") / f"{site_name}.json")
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n配置已保存: {save_path}")
        print(f"可使用以下命令测试: python main.py crawl {args.url}")
    elif args.command == "resume":
        service.resume(args.book_id, export=not args.no_export, concurrency=args.concurrency, max_chapters=args.max_chapters)
    elif args.command == "progress":
        progress = service.progress(args.book_id)
        total = progress.get("total", 0)
        done = progress.get("done", 0)
        percent = (done / total * 100) if total else 0
        print({**progress, "percent": round(percent, 2)})
    elif args.command == "validate":
        print(service.validate(args.book_id).to_text())
    elif args.command == "fix-titles":
        title_fix = service.fix_titles(args.book_id, dry_run=False)
        print(f"total: {title_fix.total}, fixed: {title_fix.fixed}")
        for detail in title_fix.details[:20]:
            print(f"  {detail}")
    elif args.command == "logs":
        for row in service.logs(args.book_id, args.limit):
            print(f"{row['created_at']} [{row['level']}] book={row['book_id']} chapter={row['chapter_index']} {row['message']}")
    elif args.command == "report":
        print(service.report(args.book_id))
    elif args.command == "retry-failed":
        service.retry_failed(args.book_id, export=not args.no_export, concurrency=args.concurrency)
    elif args.command == "retry-all":
        count = service.retry_all_failed()
        print(f"retried {count} books")
    elif args.command == "dedup":
        dedup = service.dedup(args.book_id, remove=args.remove)
        print(f"total: {dedup.total}, exact_dupes: {dedup.exact_dupes}, similar_dupes: {dedup.similar_dupes}")
        for detail in dedup.details[:20]:
            print(f"  {detail}")
    elif args.command == "export":
        path = service.export(args.book_id, args.format, args.output)
        print(path)
    elif args.command == "export-all":
        paths = service.export_all(args.format)
        print(f"exported {len(paths)} books")
        for p in paths:
            print(f"  {p}")
    elif args.command == "crawl-batch":
        kwargs = {"export": False}
        if args.concurrency:
            kwargs["concurrency"] = args.concurrency
        if args.max_chapters:
            kwargs["max_chapters"] = args.max_chapters
        book_ids = service.crawl_batch(args.file, **kwargs)
        print(f"\ncrawled {len(book_ids)} books: {book_ids}")
    elif args.command == "preview":
        print(service.preview_chapter(args.book_id, args.chapter_index, args.length))
    elif args.command == "stats":
        s = service.stats()
        print(f"书籍总数: {s['books']}")
        print(f"章节总数: {s['chapters_total']}")
        print(f"已完成: {s['chapters_done']}")
        print(f"失败: {s['chapters_failed']}")
        print(f"待下载: {s['chapters_pending']}")
        print(f"完成率: {s['completion_rate']}%")
        print(f"站点分布: {s['sites']}")
    elif args.command == "validate-config":
        validation = service.validate_config(args.config)
        print(f"配置文件: {args.config}")
        print(f"有效: {validation['valid']}")
        if validation.get("site"):
            print(f"站点: {validation['site']}")
        if validation.get("domain"):
            print(f"域名: {validation['domain']}")
        errors = validation.get("errors", [])
        if isinstance(errors, list):
            for err in errors:
                print(f"  [error] {err}")
        warnings = validation.get("warnings", [])
        if isinstance(warnings, list):
            for warn in warnings:
                print(f"  [warn] {warn}")
    elif args.command == "decode-font":
        from novel_crawler.decoders.font_shape import FontShapeDecoderBuilder
        if not ctx.chinese_fonts:
            raise SystemExit("未找到中文参考字体，无法自动破解字体映射")
        mapping = FontShapeDecoderBuilder(ctx.chinese_fonts).build_map(args.font, args.output)
        print(f"mapping size: {len(mapping)} -> {args.output}")
    elif args.command == "web":
        from novel_crawler.web import run_web_ui
        run_web_ui(ctx, args.host, args.port)
    return 0
