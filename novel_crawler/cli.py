from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from novel_crawler.application import ApplicationError, CrawlOptions, build_application
from novel_crawler.compliance import ALLOW_THIRD_PARTY_ENV, DISCLAIMER, decide_third_party_access
from novel_crawler.easyvoice import EasyVoiceOptions
from novel_crawler.exploration import explore_site, propose_config_from_report, write_report
from novel_crawler.runtime.env import RuntimeContext, create_runtime_context, format_runtime_report
from novel_crawler.task_engine import TaskStatus

LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"
_SELECTOR_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_TERMINAL_EXIT = {"completed": 0, "terminal_failed": 11, "cancelled": 12}
_ERROR_MESSAGES = {
    "task_not_found": "任务不存在",
    "task_queue_full": "任务队列已满，请稍后重试",
    "task_executor_closed": "任务执行器已关闭",
    "task_state_conflict": "任务状态已变化，请刷新后重试",
    "cleanup_required": "需要先完成浏览器资源清理",
    "interaction_unavailable": "当前无法处理交互操作",
    "interaction_failed": "交互操作失败",
    "crawler_unavailable": "书籍服务不可用",
    "crawler_operation_failed": "书籍操作失败",
    "book_id_invalid": "书籍编号无效",
    "export_format_invalid": "导出格式无效",
    "easyvoice_options_invalid": "EasyVoice 参数无效",
    "output_path_invalid": "输出路径无效",
    "third_party_crawl_disabled": "第三方线上站点抓取默认禁用；仅在确认授权、robots、条款和版权许可后再显式开启",
    "service_closing": "服务正在关闭，请稍后重试",
    "service_closed": "服务已关闭",
    "chase_unsupported": "当前版本不支持递推抓取",
    "concurrency_unsupported": "当前版本仅支持单任务顺序抓取",
    "visible_browser_unavailable": "可见浏览器不可用，请确认已安装 Chrome/Playwright",
}


class _SafeView(Protocol):
    def to_safe_dict(self) -> dict[str, object]: ...


class _Application(Protocol):
    def close(self) -> bool: ...


ApplicationFactory = Callable[[RuntimeContext], _Application]


def _bounded_float(minimum: float, maximum: float) -> Callable[[str], float]:
    def parse(value: str) -> float:
        try:
            result = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("必须是数字") from exc
        if not minimum <= result <= maximum:
            raise argparse.ArgumentTypeError(f"必须在 {minimum:g} 到 {maximum:g} 之间")
        return result

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="novel-crawler", description="通用小说爬虫系统")
    parser.add_argument("--data-dir", type=Path, default=None, help="私有运行数据目录")
    parser.add_argument(
        "--allow-third-party",
        action="store_true",
        help="显式允许本次进程访问第三方线上站点；仅限已获授权且遵守 robots/条款/版权许可的目标",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("env", help="显示运行环境检测报告")
    sub.add_parser("books", help="列出已抓取的小说")

    delete = sub.add_parser("delete", help="删除一本书及其数据")
    delete.add_argument("book_id", type=int)

    crawl = sub.add_parser("crawl", help="抓取小说（创建后台任务）")
    crawl.add_argument("url")
    crawl.add_argument("--start", type=int, default=None, help="起始章节序号")
    crawl.add_argument("--count", type=int, default=None, help="下载章节数量")
    crawl.add_argument("--no-export", action="store_true", help="完成后不导出")
    crawl.add_argument("--concurrency", type=int, default=1, help="并发数（当前仅支持 1）")
    crawl.add_argument("--max-chapters", type=int, default=None, help="本次最多下载章节数")
    crawl.add_argument("--chase", action="store_true", help="递推抓取（当前不支持）")
    crawl.add_argument("--proxy-file", type=Path, default=None, help="兼容参数（当前不支持）")
    crawl.add_argument("--browser", choices=["http", "visible"], default="http", help="抓取后端：默认 http；visible 使用有界面 Chrome")
    crawl.add_argument("--wait", action="store_true", help="等待任务结束或需要人工操作")
    crawl.add_argument("--poll-interval", type=_bounded_float(0.05, 10), default=0.5)
    crawl.add_argument("--timeout", type=_bounded_float(0.1, 86_400), default=300.0)

    tasks = sub.add_parser("tasks", help="列出后台任务")
    tasks.add_argument("--status", action="append", default=None, help="按状态筛选，可重复")
    tasks.add_argument("--limit", type=int, default=100)
    task = sub.add_parser("task", help="查看单个任务")
    task.add_argument("task_id")
    events = sub.add_parser("task-events", help="查看任务事件")
    events.add_argument("task_id")
    for name, help_text in (
        ("task-pause", "暂停任务"),
        ("task-resume", "恢复任务"),
        ("task-cancel", "取消任务"),
        ("task-continue", "人工验证完成后继续任务"),
        ("task-retry-cleanup", "重试任务资源清理"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("task_id")
    confirm = sub.add_parser("task-confirm", help="确认自动适配配置")
    confirm.add_argument("task_id")
    override = confirm.add_mutually_exclusive_group()
    override.add_argument("--selector", action="append", default=[], metavar="名称=CSS")
    override.add_argument("--selectors-json", default=None, metavar="JSON")

    # 保留 0.1 系列命令名，所有书籍操作在分发层映射到 ApplicationService。
    inspect = sub.add_parser("inspect", help="旧版站点探测命令（已停用）")
    inspect.add_argument("url")
    inspect.add_argument("--save", type=Path, default=None)
    wizard = sub.add_parser("wizard", help="旧版站点配置向导（已停用）")
    wizard.add_argument("url")
    wizard.add_argument("--save", type=Path, default=None)
    wizard.add_argument("--sample-url", default=None)
    resume = sub.add_parser("resume", help="旧版书籍续传命令（请使用 task-resume）")
    resume.add_argument("book_id", type=int)
    resume.add_argument("--no-export", action="store_true")
    resume.add_argument("--concurrency", type=int, default=1)
    resume.add_argument("--max-chapters", type=int, default=None)
    progress = sub.add_parser("progress", help="查看书籍进度")
    progress.add_argument("book_id", type=int)
    validate = sub.add_parser("validate", help="校验抓取质量")
    validate.add_argument("book_id", type=int)
    logs = sub.add_parser("logs", help="查看最近任务日志")
    logs.add_argument("--book-id", type=int, default=None)
    logs.add_argument("--limit", type=int, default=30)
    report = sub.add_parser("report", help="生成书籍报告")
    report.add_argument("book_id", type=int)
    retry_failed = sub.add_parser("retry-failed", help="重试失败章节")
    retry_failed.add_argument("book_id", type=int)
    retry_failed.add_argument("--no-export", action="store_true")
    retry_failed.add_argument("--concurrency", type=int, default=1)
    export = sub.add_parser("export", help="导出书籍")
    export.add_argument("book_id", type=int)
    export.add_argument("--format", choices=["txt", "epub", "md", "jsonl"], default="txt")
    export.add_argument("--output", type=Path, default=None, help="兼容参数（输出目录由应用统一管理）")
    tts_export = sub.add_parser("tts-export", help="导出 EasyVoice 交换 JSON")
    tts_export.add_argument("book_id", type=int)
    tts_export.add_argument("--output", type=Path, default=None, help="交换 JSON 输出路径")
    tts_convert = sub.add_parser("tts-convert", help="调用 EasyVoice 转换章节音频")
    tts_convert.add_argument("book_id", type=int)
    tts_convert.add_argument("--export-path", type=Path, default=None, help="交换 JSON 输出路径")
    tts_convert.add_argument("--output-dir", type=Path, default=None, help="EasyVoice 音频输出目录")
    tts_convert.add_argument("--base-url", default="http://localhost:9549", help="EasyVoice 服务地址")
    tts_convert.add_argument("--voice", default="zh-CN-YunxiNeural")
    tts_convert.add_argument("--rate", default="+0%")
    tts_convert.add_argument("--pitch", default="+0Hz")
    tts_convert.add_argument("--volume", default="+0%")
    tts_convert.add_argument("--use-llm", action="store_true")
    tts_convert.add_argument("--poll-interval", type=_bounded_float(0.05, 60), default=3.0)
    tts_convert.add_argument("--task-timeout", type=_bounded_float(1, 86_400), default=3600.0)
    tts_convert.add_argument("--retries", type=int, default=3)
    tts_convert.add_argument("--assemble", action="store_true")
    tts_convert.add_argument("--media-container", default=None)
    tts_convert.add_argument("--media-host-root", type=Path, default=None)
    tts_convert.add_argument("--media-container-root", type=Path, default=None)
    tts_convert.add_argument("--pipeline", type=Path, default=None, help="novel_tts_pipeline.py 路径")
    decode_font = sub.add_parser("decode-font", help="根据系统字体解码混淆字体")
    decode_font.add_argument("font", type=Path)
    decode_font.add_argument("--output", type=Path, default=Path("font_decode_map.json"))
    web = sub.add_parser("web", help="启动 Web UI 管理界面")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--unsafe-remote", action="store_true", help="明确允许非本机地址监听（有安全风险）")
    fix_titles = sub.add_parser("fix-titles", help="修正章节标题编号")
    fix_titles.add_argument("book_id", type=int)
    dedup = sub.add_parser("dedup", help="检测或移除重复章节")
    dedup.add_argument("book_id", type=int)
    dedup.add_argument("--remove", action="store_true")
    export_all = sub.add_parser("export-all", help="批量导出所有书籍")
    export_all.add_argument("--format", choices=["txt", "epub", "md", "jsonl"], default="txt")
    sub.add_parser("retry-all", help="重试所有书籍的失败章节")
    crawl_batch = sub.add_parser("crawl-batch", help="从 URL 列表批量创建任务")
    crawl_batch.add_argument("file", type=Path)
    crawl_batch.add_argument("--concurrency", type=int, default=1)
    crawl_batch.add_argument("--max-chapters", type=int, default=None)
    preview = sub.add_parser("preview", help="预览章节内容")
    preview.add_argument("book_id", type=int)
    preview.add_argument("chapter_index", type=int)
    preview.add_argument("--length", type=int, default=500)
    sub.add_parser("stats", help="显示全局下载统计")
    validate_config = sub.add_parser("validate-config", help="校验站点配置文件")
    validate_config.add_argument("config", type=Path)
    explore_site_cmd = sub.add_parser("explore-site", help="探索新源站并生成候选配置报告")
    explore_site_cmd.add_argument("url")
    explore_site_cmd.add_argument("--sample", type=int, default=3, help="样本章节数量，1 到 5")
    explore_site_cmd.add_argument("--output", type=Path, default=None, help="探索报告 JSON 输出路径")
    propose_config = sub.add_parser("propose-config", help="从探索报告导出通用站点配置")
    propose_config.add_argument("report", type=Path)
    propose_config.add_argument("--output", type=Path, required=True, help="候选配置 JSON 输出路径")
    return parser


def main(
    argv: list[str] | None = None,
    project_dir: Path | None = None,
    *,
    application_factory: ApplicationFactory = build_application,
) -> int:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    args = build_parser().parse_args(argv)
    previous_allow = os.environ.get(ALLOW_THIRD_PARTY_ENV)
    if args.allow_third_party:
        os.environ[ALLOW_THIRD_PARTY_ENV] = "1"
        print(f"合规免责声明：{DISCLAIMER}", file=sys.stderr)
    try:
        ctx = create_runtime_context((project_dir or Path.cwd()).resolve(), args.data_dir)
        if args.command == "env":
            print(format_runtime_report(ctx))
            return 0
        if args.command == "decode-font":
            return _decode_font(ctx, args)
        if args.command == "explore-site":
            try:
                return _explore_site(args)
            except ApplicationError as exc:
                return _application_error(exc)
            except Exception:
                print("探索失败（code=exploration_failed）。", file=sys.stderr)
                return 3
        if args.command == "propose-config":
            try:
                output = propose_config_from_report(args.report, args.output)
            except Exception:
                print("候选配置生成失败（code=propose_config_failed）。", file=sys.stderr)
                return 3
            _print_json({"completed": True, "output": str(output)})
            return 0
        app: _Application | None = None
        result = 3
        try:
            app = application_factory(ctx)
            result = _dispatch(app, args, ctx)
        except ApplicationError as exc:
            result = _application_error(exc)
        except KeyboardInterrupt:
            print("操作已中断。", file=sys.stderr)
            result = 130
        except Exception:
            print("操作失败（code=internal_error）。", file=sys.stderr)
            result = 3
        finally:
            if app is not None:
                try:
                    closed = app.close()
                except Exception:
                    closed = False
                if not closed:
                    print("资源未能安全关闭（code=close_incomplete），请稍后重试。", file=sys.stderr)
                    if result == 0:
                        result = 7
        return result
    finally:
        if args.allow_third_party:
            if previous_allow is None:
                os.environ.pop(ALLOW_THIRD_PARTY_ENV, None)
            else:
                os.environ[ALLOW_THIRD_PARTY_ENV] = previous_allow


def _dispatch(app: _Application, args: argparse.Namespace, ctx: RuntimeContext) -> int:
    command = args.command
    if command == "web":
        from novel_crawler.web import run_web_ui

        if not _is_loopback_host(args.host) and not args.unsafe_remote:
            print("非本机监听必须显式指定 --unsafe-remote（code=remote_bind_forbidden）。", file=sys.stderr)
            return 2
        run_web_ui(
            ctx,
            args.host,
            args.port,
            application=app,
            close_application=False,
            unsafe_remote=args.unsafe_remote,
        )
        return 0
    if command == "crawl":
        if args.chase:
            print("当前版本不支持递推抓取（code=chase_unsupported）。", file=sys.stderr)
            return 2
        if args.concurrency != 1:
            print("当前版本不支持并发抓取（code=concurrency_unsupported）。", file=sys.stderr)
            return 2
        if args.proxy_file is not None:
            print("当前版本不支持命令行代理文件（code=proxy_file_unsupported）。", file=sys.stderr)
            return 2
        options = CrawlOptions(
            start=args.start,
            count=args.count,
            max_chapters=args.max_chapters,
            concurrency=args.concurrency,
            export=not args.no_export,
            chase=args.chase,
            browser=args.browser,
        )
        view = _call(app, "create_crawl_task", args.url, options)
        if not args.wait:
            _print_view(view)
            return 0
        task_id = _view_dict(view).get("task_id")
        if not isinstance(task_id, str):
            raise ApplicationError("task_view_failed", retryable=True)
        return _wait_for_task(app, task_id, args.poll_interval, args.timeout)
    if command == "tasks":
        if not 1 <= args.limit <= 1000:
            print("任务数量必须在 1 到 1000 之间。", file=sys.stderr)
            return 2
        try:
            statuses = {TaskStatus(value) for value in args.status} if args.status else None
        except ValueError:
            print("任务状态无效（code=task_status_invalid）。", file=sys.stderr)
            return 2
        _print_views(_call(app, "list_tasks", statuses=statuses, limit=args.limit))
        return 0
    simple = {
        "task": "get_task",
        "task-events": "task_events",
        "task-pause": "pause_task",
        "task-resume": "resume_task",
        "task-cancel": "cancel_task",
        "task-continue": "continue_interaction",
        "task-retry-cleanup": "retry_cleanup",
    }
    if command in simple:
        value = _call(app, simple[command], args.task_id)
        _print_views(value) if isinstance(value, list) else _print_view(value)
        return 0
    if command == "task-confirm":
        try:
            overrides = _parse_selectors(args.selector, args.selectors_json)
        except ValueError:
            print("选择器参数无效（code=selector_overrides_invalid）。", file=sys.stderr)
            return 2
        _print_view(_call(app, "confirm_interaction", args.task_id, overrides))
        return 0
    if command == "books":
        _print_json(_call(app, "list_books"))
        return 0
    if command == "progress":
        _print_json(_call(app, "book_progress", args.book_id))
        return 0
    if command in {"inspect", "wizard"}:
        print("该旧版命令已停用；请使用 crawl 自动适配（code=legacy_command_disabled）。", file=sys.stderr)
        return 2
    if command == "resume":
        print("书籍续传已迁移为任务恢复；请使用 task-resume（code=legacy_command_disabled）。", file=sys.stderr)
        return 2
    return _dispatch_book_command(app, args)


def _dispatch_book_command(app: _Application, args: argparse.Namespace) -> int:
    command = args.command
    calls: dict[str, tuple[str, tuple[object, ...]]] = {
        "delete": ("delete_book", (args.book_id,)) if command == "delete" else ("", ()),
        "validate": ("validate_book", (args.book_id,)) if command == "validate" else ("", ()),
        "logs": ("book_logs", (args.book_id, args.limit)) if command == "logs" else ("", ()),
        "report": ("book_report", (args.book_id,)) if command == "report" else ("", ()),
        "retry-failed": ("retry_failed_chapters", (args.book_id, not args.no_export, args.concurrency)) if command == "retry-failed" else ("", ()),
        "export": ("export_book", (args.book_id, args.format)) if command == "export" else ("", ()),
        "tts-export": ("export_easyvoice_book", (args.book_id, args.output)) if command == "tts-export" else ("", ()),
        "tts-convert": ("convert_easyvoice_book", (args.book_id, _easyvoice_options(args))) if command == "tts-convert" else ("", ()),
        "fix-titles": ("fix_book_titles", (args.book_id,)) if command == "fix-titles" else ("", ()),
        "dedup": ("deduplicate_book", (args.book_id, args.remove)) if command == "dedup" else ("", ()),
        "export-all": ("export_all_books", (args.format,)) if command == "export-all" else ("", ()),
        "retry-all": ("retry_all_failed_chapters", ()) if command == "retry-all" else ("", ()),
        "crawl-batch": ("create_crawl_tasks_from_file", (args.file, args.concurrency, args.max_chapters)) if command == "crawl-batch" else ("", ()),
        "preview": ("preview_book_chapter", (args.book_id, args.chapter_index, args.length)) if command == "preview" else ("", ()),
        "stats": ("book_stats", ()) if command == "stats" else ("", ()),
        "validate-config": ("validate_site_config", (args.config,)) if command == "validate-config" else ("", ()),
    }
    method, values = calls.get(command, ("", ()))
    if not method:
        raise ApplicationError("command_unsupported")
    if command == "tts-convert":
        result = _call(app, method, *values, export_path=args.export_path, output_dir=args.output_dir)
    else:
        result = _call(app, method, *values)
    _print_json(result)
    if command == "tts-convert" and isinstance(result, Mapping):
        code = result.get("returncode")
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    if command in {"crawl-batch", "export-all", "retry-all"} and isinstance(result, Mapping):
        for field in ("failed", "remaining"):
            value = result.get(field, 0)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return 6
    return 0


def _explore_site(args: argparse.Namespace) -> int:
    decision = decide_third_party_access(args.url)
    if not decision.allowed:
        raise ApplicationError(decision.code)
    if isinstance(args.sample, bool) or not 1 <= args.sample <= 5:
        print("样本数量必须在 1 到 5 之间（code=sample_invalid）。", file=sys.stderr)
        return 2
    output = args.output or Path("exploratory") / "site-report.json"
    report = explore_site(args.url, sample=args.sample)
    path = write_report(report, output)
    _print_json(
        {
            "completed": True,
            "output": str(path),
            "domain": report["domain"],
            "sample_count": report["sample_count"],
            "requires_dedicated_adapter": report["requires_dedicated_adapter"],
            "warning_codes": [item["code"] for item in report["warnings"]],
        }
    )
    return 0


def _easyvoice_options(args: argparse.Namespace) -> EasyVoiceOptions:
    if isinstance(args.retries, bool) or not isinstance(args.retries, int) or not 0 <= args.retries <= 100:
        raise ApplicationError("easyvoice_options_invalid")
    return EasyVoiceOptions(
        base_url=args.base_url,
        voice=args.voice,
        rate=args.rate,
        pitch=args.pitch,
        volume=args.volume,
        use_llm=args.use_llm,
        poll_interval=args.poll_interval,
        task_timeout=args.task_timeout,
        retries=args.retries,
        assemble=args.assemble,
        media_container=args.media_container,
        media_host_root=args.media_host_root,
        media_container_root=args.media_container_root,
        pipeline=args.pipeline,
    )


def _wait_for_task(app: _Application, task_id: str, interval: float, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while True:
        view = _call(app, "get_task", task_id)
        payload = _view_dict(view)
        status = payload.get("status")
        if status == "waiting_for_user":
            _print_json(payload)
            return 10
        if status in {"paused", "recoverable_failed"} or payload.get("cleanup_required") is True:
            _print_json(payload)
            return 13
        if payload.get("terminal") is True or status in _TERMINAL_EXIT:
            _print_json(payload)
            return _TERMINAL_EXIT.get(str(status), 11)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("等待任务超时（code=wait_timeout）。", file=sys.stderr)
            return 9
        time.sleep(min(interval, remaining))


def _parse_selectors(items: list[str], raw_json: str | None) -> dict[str, str]:
    if raw_json is not None:
        try:
            value = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("invalid json") from exc
        if not isinstance(value, dict):
            raise ValueError("mapping required")
        pairs = value.items()
    else:
        parsed: dict[str, str] = {}
        for item in items:
            if not isinstance(item, str) or "=" not in item:
                raise ValueError("key=value required")
            key, value = item.split("=", 1)
            if key in parsed:
                raise ValueError("duplicate key")
            parsed[key] = value
        pairs = parsed.items()
    result: dict[str, str] = {}
    total = 0
    for key, value in pairs:
        if not isinstance(key, str) or not _SELECTOR_NAME.fullmatch(key):
            raise ValueError("invalid key")
        if not isinstance(value, str) or not value or len(value) > 512:
            raise ValueError("invalid selector")
        if any(ord(char) < 32 for char in value):
            raise ValueError("invalid selector")
        total += len(value)
        if key in result or len(result) >= 20 or total > 4096:
            raise ValueError("selector limit")
        result[key] = value
    return result


def _call(app: _Application, name: str, *args: object, **kwargs: object) -> Any:
    method = getattr(app, name, None)
    if not callable(method):
        raise ApplicationError("command_unsupported")
    return method(*args, **kwargs)


def _view_dict(value: object) -> dict[str, object]:
    serializer = getattr(value, "to_safe_dict", None)
    if not callable(serializer):
        raise ApplicationError("unsafe_output_blocked")
    payload = serializer()
    if not isinstance(payload, dict):
        raise ApplicationError("unsafe_output_blocked")
    return payload


def _print_view(value: object) -> None:
    _print_json(_view_dict(value))


def _print_views(values: object) -> None:
    if not isinstance(values, list):
        raise ApplicationError("unsafe_output_blocked")
    _print_json([_view_dict(value) for value in values])


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _application_error(exc: ApplicationError) -> int:
    message = _ERROR_MESSAGES.get(exc.code, "操作未完成")
    print(f"{message}（code={exc.code}）。", file=sys.stderr)
    if exc.code == "task_not_found":
        return 4
    if exc.code == "third_party_crawl_disabled":
        return 2
    if exc.code.endswith("_invalid") or exc.code.endswith("_unsupported"):
        return 2
    return 6 if exc.retryable else 3


def _is_loopback_host(host: object) -> bool:
    if not isinstance(host, str):
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _decode_font(ctx: RuntimeContext, args: argparse.Namespace) -> int:
    from novel_crawler.decoders.font_shape import FontShapeDecoderBuilder

    if not ctx.chinese_fonts:
        print("未找到中文参考字体，无法自动解码字体映射。", file=sys.stderr)
        return 3
    try:
        mapping = FontShapeDecoderBuilder(ctx.chinese_fonts).build_map(args.font, args.output)
    except Exception:
        print("字体解码失败（code=font_decode_failed）。", file=sys.stderr)
        return 3
    _print_json({"completed": True, "mapping_size": len(mapping)})
    return 0
