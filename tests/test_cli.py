from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import repeat
from pathlib import Path
from types import SimpleNamespace

import pytest

from novel_crawler.application import ApplicationError
from novel_crawler.cli import build_parser, main


@dataclass
class SafeView:
    payload: dict[str, object]

    def to_safe_dict(self) -> dict[str, object]:
        return dict(self.payload)


class FakeApplication:
    def __init__(self, task_states: Iterator[SafeView] | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.closed = False
        self.close_result = True
        self.task_states = task_states

    def close(self) -> bool:
        self.closed = True
        return self.close_result

    def create_crawl_task(self, url: str, options: object) -> SafeView:
        self.calls.append(("create", url, options))
        return SafeView({"task_id": "task-safe", "status": "created", "terminal": False})

    def get_task(self, task_id: str) -> SafeView:
        self.calls.append(("get", task_id))
        if self.task_states is None:
            return SafeView({"task_id": task_id, "status": "completed", "terminal": True})
        return next(self.task_states)

    def list_tasks(self, *, statuses=None, limit: int = 1000) -> list[SafeView]:
        self.calls.append(("tasks", statuses, limit))
        return [SafeView({"task_id": "one", "status": "paused", "terminal": False})]

    def task_events(self, task_id: str) -> list[SafeView]:
        self.calls.append(("events", task_id))
        return [SafeView({"event_id": 1, "task_id": task_id, "to_status": "created"})]

    def pause_task(self, task_id: str) -> SafeView:
        return self._control("pause", task_id)

    def resume_task(self, task_id: str) -> SafeView:
        return self._control("resume", task_id)

    def cancel_task(self, task_id: str) -> SafeView:
        return self._control("cancel", task_id)

    def continue_interaction(self, task_id: str) -> SafeView:
        return self._control("continue", task_id)

    def confirm_interaction(self, task_id: str, overrides: dict[str, str]) -> SafeView:
        self.calls.append(("confirm", task_id, overrides))
        return SafeView({"task_id": task_id, "status": "validating", "terminal": False})

    def retry_cleanup(self, task_id: str) -> SafeView:
        return self._control("cleanup", task_id)

    def list_books(self) -> list[dict[str, object]]:
        self.calls.append(("books",))
        return [{"id": 1, "title": "测试书", "site": "站点", "done": 2, "total": 3}]

    def book_progress(self, book_id: int) -> dict[str, int]:
        self.calls.append(("progress", book_id))
        return {"total": 3, "done": 2, "failed": 0, "pending": 1}

    def delete_book(self, book_id: int):
        return self._book("delete", book_id)

    def validate_book(self, book_id: int):
        return self._book("validate", book_id)

    def book_logs(self, book_id: int | None, limit: int):
        return self._book("logs", book_id, limit)

    def book_report(self, book_id: int):
        return self._book("report", book_id)

    def retry_failed_chapters(self, book_id: int, export: bool, concurrency: int):
        return self._book("retry-failed", book_id, export, concurrency)

    def export_book(self, book_id: int, fmt: str):
        return self._book("export", book_id, fmt)

    def fix_book_titles(self, book_id: int):
        return self._book("fix-titles", book_id)

    def deduplicate_book(self, book_id: int, remove: bool):
        return self._book("dedup", book_id, remove)

    def export_all_books(self, fmt: str):
        return self._book("export-all", fmt)

    def retry_all_failed_chapters(self):
        return self._book("retry-all")

    def create_crawl_tasks_from_file(self, path: Path, concurrency: int, max_chapters: int | None):
        return self._book("crawl-batch", path, concurrency, max_chapters)

    def preview_book_chapter(self, book_id: int, chapter_index: int, length: int):
        return self._book("preview", book_id, chapter_index, length)

    def book_stats(self):
        return self._book("stats")

    def validate_site_config(self, path: Path):
        return self._book("validate-config", path)

    def _book(self, name: str, *values: object):
        self.calls.append((name, *values))
        return {"completed": True, "operation": name}

    def _control(self, action: str, task_id: str) -> SafeView:
        self.calls.append((action, task_id))
        return SafeView({"task_id": task_id, "status": action, "terminal": False})


def app_factory(app: FakeApplication):
    def factory(_ctx):
        return app

    return factory


def output_json(capsys) -> object:
    return json.loads(capsys.readouterr().out)


def test_help_is_utf8_and_parser_preserves_legacy_plus_task_commands(capsys) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["--help"])
    output = capsys.readouterr().out
    assert "通用小说爬虫系统" in output
    assert "抓取小说" in output
    assert "�" not in output

    commands = build_parser()._subparsers._group_actions[0].choices
    legacy = {
        "env", "books", "delete", "crawl", "inspect", "wizard", "resume",
        "progress", "validate", "logs", "report", "retry-failed", "export",
        "decode-font", "web", "fix-titles", "dedup", "export-all", "retry-all",
        "crawl-batch", "preview", "stats", "validate-config",
    }
    task_commands = {
        "tasks", "task", "task-events", "task-pause", "task-resume",
        "task-cancel", "task-continue", "task-confirm", "task-retry-cleanup",
    }
    assert legacy | task_commands <= set(commands)


def test_cli_source_contains_no_known_mojibake_or_replacement_character() -> None:
    source = (Path(__file__).parents[1] / "novel_crawler" / "cli.py").read_text(encoding="utf-8")
    assert "通用小说爬虫系统" in source
    assert "任务不存在" in source
    for broken in ("閫氱敤", "绔犺妭", "鎶撳彇", "璇烽", "鎿嶄綔", "褰撳墠", "�"):
        assert broken not in source


def test_env_does_not_construct_application(tmp_path, capsys) -> None:
    def forbidden(_ctx):
        raise AssertionError("env must not construct the application")

    assert main(["env"], project_dir=tmp_path, application_factory=forbidden) == 0
    assert "Runtime:" in capsys.readouterr().out


def test_crawl_detaches_and_prints_safe_task_json(tmp_path, capsys) -> None:
    app = FakeApplication()
    assert main(
        ["crawl", "https://example.test/book", "--start", "2", "--count", "3", "--browser", "visible"],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 0
    assert output_json(capsys) == {"status": "created", "task_id": "task-safe", "terminal": False}
    assert app.calls[0][0:2] == ("create", "https://example.test/book")
    assert app.calls[0][2].browser == "visible"
    assert app.closed


@pytest.mark.parametrize(("flag", "value"), [("--chase", None), ("--concurrency", "2")])
def test_crawl_rejects_unsupported_modes_before_task_creation(tmp_path, capsys, flag, value) -> None:
    app = FakeApplication()
    argv = ["crawl", "https://example.test/book", flag]
    if value is not None:
        argv.append(value)
    assert main(argv, project_dir=tmp_path, application_factory=app_factory(app)) == 2
    captured = capsys.readouterr()
    assert "不支持" in captured.err
    assert not app.calls
    assert app.closed


def test_wait_stops_at_waiting_for_user_without_leaking_token(tmp_path, capsys) -> None:
    states = iter([
        SafeView({
            "task_id": "task-safe", "status": "waiting_for_user", "terminal": False,
            "interaction": {"verification_required": True, "confirmation_required": False,
                            "cleanup_required": False, "safe_origin": "example.test"},
        })
    ])
    app = FakeApplication(states)
    assert main(
        ["crawl", "https://example.test/book", "--wait", "--poll-interval", "0.05", "--timeout", "1"],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 10
    payload = output_json(capsys)
    assert payload["status"] == "waiting_for_user"
    assert payload["interaction"]["verification_required"] is True
    assert "token" not in json.dumps(payload).lower()
    assert not any(call[0] == "continue" for call in app.calls)


def test_task_commands_use_safe_views_and_strict_selector_parsing(tmp_path, capsys) -> None:
    app = FakeApplication()
    assert main(
        ["task-confirm", "task-safe", "--selector", "title=h1", "--selector", "content=#main"],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 0
    assert app.calls == [("confirm", "task-safe", {"title": "h1", "content": "#main"})]
    assert output_json(capsys)["status"] == "validating"

    app = FakeApplication()
    assert main(
        ["task-confirm", "task-safe", "--selectors-json", '{"title":"h1","bad":1}'],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 2
    assert not app.calls
    assert "选择器参数无效" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["tasks", "--limit", "5"], "tasks"),
        (["task", "abc"], "get"),
        (["task-events", "abc"], "events"),
        (["task-pause", "abc"], "pause"),
        (["task-resume", "abc"], "resume"),
        (["task-cancel", "abc"], "cancel"),
        (["task-continue", "abc"], "continue"),
        (["task-retry-cleanup", "abc"], "cleanup"),
    ],
)
def test_each_task_command_dispatches_to_application(tmp_path, capsys, command, expected) -> None:
    app = FakeApplication()
    assert main(command, project_dir=tmp_path, application_factory=app_factory(app)) == 0
    output_json(capsys)
    assert app.calls[0][0] == expected


def test_tasks_rejects_unknown_status_before_application_call(tmp_path, capsys) -> None:
    app = FakeApplication()

    assert main(
        ["tasks", "--status", "not-a-status"],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 2

    assert app.calls == []
    assert "任务状态无效" in capsys.readouterr().err


def test_application_error_is_stable_chinese_and_redacted(tmp_path, capsys) -> None:
    class FailingApplication(FakeApplication):
        def get_task(self, task_id: str) -> SafeView:
            raise ApplicationError("task_not_found")

    app = FailingApplication()
    assert main(["task", "secret-token"], project_dir=tmp_path, application_factory=app_factory(app)) == 4
    error = capsys.readouterr().err
    assert "任务不存在" in error
    assert "task_not_found" in error
    assert "secret-token" not in error


def test_keyboard_interrupt_and_incomplete_close_have_stable_exit_codes(tmp_path, capsys) -> None:
    class InterruptedApplication(FakeApplication):
        def list_tasks(self, *, statuses=None, limit: int = 1000):
            raise KeyboardInterrupt

    app = InterruptedApplication()
    assert main(["tasks"], project_dir=tmp_path, application_factory=app_factory(app)) == 130
    assert "操作已中断" in capsys.readouterr().err
    assert app.closed

    app = FakeApplication()
    app.close_result = False
    assert main(["books"], project_dir=tmp_path, application_factory=app_factory(app)) == 7
    assert "资源未能安全关闭" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["delete", "1"], "delete"),
        (["validate", "1"], "validate"),
        (["logs", "--book-id", "1", "--limit", "2"], "logs"),
        (["report", "1"], "report"),
        (["retry-failed", "1", "--no-export"], "retry-failed"),
        (["export", "1", "--format", "epub"], "export"),
        (["fix-titles", "1"], "fix-titles"),
        (["dedup", "1", "--remove"], "dedup"),
        (["export-all", "--format", "md"], "export-all"),
        (["retry-all"], "retry-all"),
        (["crawl-batch", "urls.txt", "--max-chapters", "5"], "crawl-batch"),
        (["preview", "1", "2", "--length", "10"], "preview"),
        (["stats"], "stats"),
        (["validate-config", "site.json"], "validate-config"),
    ],
)
def test_legacy_book_commands_are_routed_through_facade(tmp_path, capsys, command, expected) -> None:
    app = FakeApplication()
    assert main(command, project_dir=tmp_path, application_factory=app_factory(app)) == 0
    assert output_json(capsys)["operation"] == expected
    assert app.calls[0][0] == expected


@pytest.mark.parametrize("command", [["inspect", "https://example.test"], ["wizard", "https://example.test"], ["resume", "1"]])
def test_network_legacy_commands_are_explicitly_disabled(tmp_path, capsys, command) -> None:
    app = FakeApplication()
    assert main(command, project_dir=tmp_path, application_factory=app_factory(app)) == 2
    error = capsys.readouterr().err
    assert "旧版" in error or "任务恢复" in error
    assert app.calls == []


def test_wait_terminal_and_timeout_exit_codes(tmp_path, capsys) -> None:
    app = FakeApplication(iter([SafeView({"task_id": "task-safe", "status": "completed", "terminal": True})]))
    assert main(
        ["crawl", "https://example.test", "--wait"],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 0
    assert output_json(capsys)["status"] == "completed"

    app = FakeApplication(repeat(SafeView({"task_id": "task-safe", "status": "crawling", "terminal": False})))
    assert main(
        ["crawl", "https://example.test", "--wait", "--poll-interval", "0.05", "--timeout", "0.1"],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 9
    assert "wait_timeout" in capsys.readouterr().err


def test_selector_json_success_and_malformed_key_value_are_bounded(tmp_path, capsys) -> None:
    app = FakeApplication()
    assert main(
        ["task-confirm", "one", "--selectors-json", '{"title":"h1"}'],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 0
    output_json(capsys)
    assert app.calls == [("confirm", "one", {"title": "h1"})]

    for selector in ("missing-equals", "title=h1\nscript", "1bad=h1", "title="):
        app = FakeApplication()
        assert main(
            ["task-confirm", "one", "--selector", selector],
            project_dir=tmp_path,
            application_factory=app_factory(app),
        ) == 2
        capsys.readouterr()
        assert app.calls == []


def test_invalid_limits_factory_failure_and_close_exception_are_safe(tmp_path, capsys) -> None:
    app = FakeApplication()
    assert main(["tasks", "--limit", "0"], project_dir=tmp_path, application_factory=app_factory(app)) == 2
    assert "1 到 1000" in capsys.readouterr().err

    def failed_factory(_ctx):
        raise RuntimeError("https://private.test token=secret")

    assert main(["books"], project_dir=tmp_path, application_factory=failed_factory) == 3
    assert "private" not in capsys.readouterr().err

    class BadClose(FakeApplication):
        def close(self) -> bool:
            raise RuntimeError("private")

    assert main(["books"], project_dir=tmp_path, application_factory=app_factory(BadClose())) == 7
    assert "close_incomplete" in capsys.readouterr().err


def test_parser_rejects_non_numeric_and_out_of_range_wait_values(capsys) -> None:
    for value in ("abc", "0.001", "90000"):
        with pytest.raises(SystemExit, match="2"):
            main(["crawl", "https://example.test", "--wait", "--timeout", value])
        capsys.readouterr()


def test_web_dispatch_and_decode_font_paths_are_safe(tmp_path, capsys, monkeypatch) -> None:
    web_calls = []
    app = FakeApplication()
    monkeypatch.setattr(
        "novel_crawler.web.run_web_ui",
        lambda ctx, host, port, **kwargs: web_calls.append((host, port, kwargs)),
    )
    assert main(
        ["web", "--port", "9000"],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 0
    assert web_calls == [(
        "127.0.0.1",
        9000,
        {"application": app, "close_application": False, "unsafe_remote": False},
    )]
    assert app.closed

    web_calls.clear()
    app = FakeApplication()
    assert main(
        ["web", "--host", "0.0.0.0"],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 2
    assert "remote_bind_forbidden" in capsys.readouterr().err
    assert web_calls == []
    assert app.closed

    app = FakeApplication()
    assert main(
        ["web", "--host", "0.0.0.0", "--unsafe-remote"],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 0
    assert web_calls[-1][2]["unsafe_remote"] is True
    assert app.closed

    def fail_web(*_args, **_kwargs):
        raise RuntimeError("https://private.test token=secret")

    app = FakeApplication()
    monkeypatch.setattr("novel_crawler.web.run_web_ui", fail_web)
    assert main(["web"], project_dir=tmp_path, application_factory=app_factory(app)) == 3
    assert "private" not in capsys.readouterr().err
    assert app.closed

    empty_ctx = SimpleNamespace(chinese_fonts=[])
    monkeypatch.setattr("novel_crawler.cli.create_runtime_context", lambda *_args: empty_ctx)
    assert main(["decode-font", "font.ttf"], project_dir=tmp_path) == 3
    assert "参考字体" in capsys.readouterr().err

    font_ctx = SimpleNamespace(chinese_fonts=[Path("reference.ttf")])
    monkeypatch.setattr("novel_crawler.cli.create_runtime_context", lambda *_args: font_ctx)
    monkeypatch.setattr("novel_crawler.decoders.font_shape.FontShapeDecoderBuilder.build_map", lambda *_args: {"a": "中"})
    assert main(["decode-font", "font.ttf"], project_dir=tmp_path) == 0
    assert output_json(capsys)["mapping_size"] == 1

    def fail_decode(*_args):
        raise RuntimeError("private")

    monkeypatch.setattr("novel_crawler.decoders.font_shape.FontShapeDecoderBuilder.build_map", fail_decode)
    assert main(["decode-font", "font.ttf"], project_dir=tmp_path) == 3
    assert "font_decode_failed" in capsys.readouterr().err


def test_proxy_progress_valid_status_and_unsafe_views_are_handled(tmp_path, capsys) -> None:
    app = FakeApplication()
    assert main(
        ["crawl", "https://example.test", "--proxy-file", "proxy.txt"],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 2
    assert "proxy_file_unsupported" in capsys.readouterr().err

    app = FakeApplication()
    assert main(["progress", "1"], project_dir=tmp_path, application_factory=app_factory(app)) == 0
    assert output_json(capsys)["done"] == 2
    app = FakeApplication()
    assert main(["tasks", "--status", "paused"], project_dir=tmp_path, application_factory=app_factory(app)) == 0
    output_json(capsys)
    assert app.calls[0][1]

    class UnsafeApp(FakeApplication):
        def get_task(self, task_id: str):
            return {"token": "private"}

    assert main(["task", "one"], project_dir=tmp_path, application_factory=app_factory(UnsafeApp())) == 3
    assert "unsafe_output_blocked" in capsys.readouterr().err


def test_selector_parser_rejects_json_shapes_duplicates_and_limits(tmp_path, capsys) -> None:
    invalid_args = [
        ["--selectors-json", "not-json"],
        ["--selectors-json", "[]"],
        ["--selector", "title=h1", "--selector", "title=h2"],
        ["--selectors-json", json.dumps({f"field{i}": "x" for i in range(21)})],
        ["--selectors-json", json.dumps({"title": "x" * 513})],
    ]
    for values in invalid_args:
        app = FakeApplication()
        assert main(
            ["task-confirm", "one", *values],
            project_dir=tmp_path,
            application_factory=app_factory(app),
        ) == 2
        assert "selector_overrides_invalid" in capsys.readouterr().err


def test_retryable_application_error_uses_stable_exit_code(tmp_path, capsys) -> None:
    class RetryApp(FakeApplication):
        def list_tasks(self, *, statuses=None, limit: int = 1000):
            raise ApplicationError("task_queue_full", retryable=True)

    assert main(["tasks"], project_dir=tmp_path, application_factory=app_factory(RetryApp())) == 6
    error = capsys.readouterr().err
    assert "task_queue_full" in error
    assert "队列已满" in error


@pytest.mark.parametrize(
    "payload",
    [
        {"task_id": "task-safe", "status": "paused", "terminal": False},
        {"task_id": "task-safe", "status": "recoverable_failed", "terminal": False},
        {"task_id": "task-safe", "status": "crawling", "terminal": False, "cleanup_required": True},
    ],
)
def test_wait_returns_immediately_for_non_progressing_task_states(tmp_path, capsys, payload) -> None:
    app = FakeApplication(iter([SafeView(payload)]))
    assert main(
        ["crawl", "https://example.test", "--wait", "--timeout", "10"],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 13
    assert output_json(capsys) == payload
    assert len([call for call in app.calls if call[0] == "get"]) == 1


def test_batch_partial_success_is_visible_and_nonzero(tmp_path, capsys) -> None:
    class PartialBatchApplication(FakeApplication):
        def create_crawl_tasks_from_file(self, path, concurrency, max_chapters):
            self.calls.append(("crawl-batch", path, concurrency, max_chapters))
            return {
                "requested": 3,
                "created": 2,
                "submitted": 1,
                "failed": 1,
                "not_started": 1,
                "error_code": "task_queue_full",
                "tasks": [{"task_id": "safe-one", "status": "created"}, {"task_id": "safe-two", "status": "paused"}],
            }

    app = PartialBatchApplication()
    assert main(
        ["crawl-batch", "urls.txt"],
        project_dir=tmp_path,
        application_factory=app_factory(app),
    ) == 6
    payload = output_json(capsys)
    assert payload["failed"] == 1
    assert [task["task_id"] for task in payload["tasks"]] == ["safe-one", "safe-two"]
    assert "url" not in json.dumps(payload).lower()


@pytest.mark.parametrize(("command", "operation"), [(["export-all"], "export-all"), (["retry-all"], "retry-all")])
def test_best_effort_bulk_failure_is_visible_and_nonzero(tmp_path, capsys, command, operation) -> None:
    class PartialApplication(FakeApplication):
        def _book(self, name: str, *values: object):
            self.calls.append((name, *values))
            return {
                "best_effort": True,
                "requested": 2,
                "succeeded": 1,
                "failed": 1,
                "error_codes": ["operation_failed"],
            }

    app = PartialApplication()
    assert main(command, project_dir=tmp_path, application_factory=app_factory(app)) == 6
    assert output_json(capsys)["failed"] == 1
    assert app.calls[0][0] == operation


def test_real_composition_smoke_for_tasks(tmp_path, capsys) -> None:
    # Windows may canonicalize pytest's user-profile temp directory through an
    # alias; use a private temp directory below the real workspace for the
    # registry's handle-anchored path check.
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        root = Path(directory)
        assert main(["--data-dir", str(root / "state"), "tasks"], project_dir=root) == 0
        assert output_json(capsys) == []
