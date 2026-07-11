from pathlib import Path
from types import SimpleNamespace

import pytest

import novel_crawler.cli as cli
import novel_crawler.runtime.env as env


class FakeService:
    instances = []

    def __init__(self, ctx, proxy_file=None):
        self.ctx = ctx
        self.proxy_file = proxy_file
        self.calls = []
        self.fetcher = SimpleNamespace(fetch_text=lambda *args, **kwargs: "<html></html>")
        self.instances.append(self)

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            values = {
                "list_books": [{"id": 1, "title": "Book", "site": "test", "done": 1, "total": 2, "url": "u"}],
                "crawl": 7,
                "progress": {"total": 4, "done": 3},
                "validate": SimpleNamespace(to_text=lambda: "valid"),
                "fix_titles": SimpleNamespace(total=2, fixed=1, details=["fixed"]),
                "logs": [{"created_at": "now", "level": "INFO", "book_id": 1, "chapter_index": 2, "message": "ok"}],
                "report": "report",
                "retry_all_failed": 2,
                "dedup": SimpleNamespace(total=3, exact_dupes=1, similar_dupes=1, details=["dupe"]),
                "export": Path("book.txt"),
                "export_all": [Path("a.txt")],
                "crawl_batch": [1, 2],
                "preview_chapter": "preview",
                "stats": {"books": 1, "chapters_total": 2, "chapters_done": 1, "chapters_failed": 0,
                          "chapters_pending": 1, "completion_rate": 50, "sites": {"test": 1}},
                "validate_config": {"valid": True, "site": "test", "domain": "example.com",
                                    "errors": ["e"], "warnings": ["w"]},
            }
            return values.get(name)

        return call


@pytest.fixture
def fake_cli(monkeypatch, tmp_path):
    FakeService.instances.clear()
    monkeypatch.setattr(cli, "CrawlerService", FakeService)
    monkeypatch.setattr(cli, "create_runtime_context", lambda project, data: SimpleNamespace(chinese_fonts=[]))
    return tmp_path


@pytest.mark.parametrize(
    "argv,expected_call,output_fragment",
    [
        (["books"], ("list_books", (), {}), "Book"),
        (["delete", "1"], ("delete_book", (1,), {}), "deleted book_id: 1"),
        (["crawl", "https://example.com", "--count", "2"],
         ("crawl", ("https://example.com",), {"start": None, "count": 2, "export": True, "concurrency": 1,
                                               "max_chapters": None, "chase": False}), "book_id: 7"),
        (["resume", "1"], ("resume", (1,), {"export": True, "concurrency": 1, "max_chapters": None}), None),
        (["progress", "1"], ("progress", (1,), {}), "'percent': 75.0"),
        (["validate", "1"], ("validate", (1,), {}), "valid"),
        (["fix-titles", "1"], ("fix_titles", (1,), {"dry_run": False}), "fixed: 1"),
        (["logs", "--book-id", "1"], ("logs", (1, 30), {}), "[INFO]"),
        (["report", "1"], ("report", (1,), {}), "report"),
        (["retry-failed", "1"], ("retry_failed", (1,), {"export": True, "concurrency": 1}), None),
        (["retry-all"], ("retry_all_failed", (), {}), "retried 2 books"),
        (["dedup", "1", "--remove"], ("dedup", (1,), {"remove": True}), "exact_dupes: 1"),
        (["export", "1", "--format", "txt"], ("export", (1, "txt", None), {}), "book.txt"),
        (["export-all", "--format", "txt"], ("export_all", ("txt",), {}), "exported 1 books"),
        (["crawl-batch", "urls.txt", "--concurrency", "2", "--max-chapters", "3"],
         ("crawl_batch", (Path("urls.txt"),), {"export": False, "concurrency": 2, "max_chapters": 3}),
         "crawled 2 books"),
        (["preview", "1", "2"], ("preview_chapter", (1, 2, 500), {}), "preview"),
        (["stats"], ("stats", (), {}), "50%"),
        (["validate-config", "site.json"], ("validate_config", (Path("site.json"),), {}), "[error] e"),
    ],
)
def test_cli_dispatches_without_network(fake_cli, capsys, argv, expected_call, output_fragment):
    assert cli.main(argv, project_dir=fake_cli) == 0
    assert FakeService.instances[-1].calls == [expected_call]
    output = capsys.readouterr().out
    if output_fragment is None:
        assert output == ""
    else:
        assert output_fragment in output


def test_inspect_command_reports_and_saves_draft(fake_cli, monkeypatch, tmp_path, capsys):
    candidate = SimpleNamespace(selector="h1", sample="sample", score=3)
    inspection = SimpleNamespace(
        title_candidates=[candidate], content_candidates=[candidate], chapter_candidates=[candidate],
        to_config=lambda name, domain: {"site": name, "domain": [domain]},
    )
    import novel_crawler.sites.detector as detector
    monkeypatch.setattr(detector, "inspect_html", lambda html, url: inspection)
    output = tmp_path / "draft.json"

    assert cli.main(["inspect", "https://example.com/book", "--save", str(output)], project_dir=tmp_path) == 0
    assert output.is_file()
    assert "Title candidates:" in capsys.readouterr().out


def test_wizard_saves_detected_config_without_sample(fake_cli, monkeypatch, tmp_path):
    inspection = SimpleNamespace(
        title_selector="h1", content_selector="article", chapter_list_selector="a.chapter",
        chapter_count=0, chapter_candidates=[],
        to_config=lambda name, domain: {"site": name, "domain": [domain]},
    )
    import novel_crawler.sites.detector as detector
    monkeypatch.setattr(detector, "inspect_html", lambda html, url: inspection)
    output = tmp_path / "wizard.json"

    assert cli.main(["wizard", "https://example.com/book", "--save", str(output)], project_dir=tmp_path) == 0
    assert output.is_file()


def test_decode_font_requires_discovered_fonts(fake_cli):
    with pytest.raises(SystemExit):
        cli.main(["decode-font", "font.ttf"], project_dir=fake_cli)


@pytest.mark.parametrize("system,expected", [("Darwin", "macos"), ("Windows", "windows"), ("", "unknown")])
def test_detect_os_normalizes_platform(monkeypatch, system, expected):
    monkeypatch.setattr(env.platform, "system", lambda: system)
    assert env.detect_os() == expected


def test_default_data_dir_honors_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(env, "os", SimpleNamespace(name="posix", environ={"XDG_DATA_HOME": str(tmp_path)}))
    monkeypatch.setattr(env.platform, "system", lambda: "Linux")
    assert env.default_data_dir() == tmp_path / "novel-crawler"


def test_runtime_helpers_detect_features_proxies_fonts_and_format(monkeypatch, tmp_path):
    monkeypatch.setattr(env.importlib.util, "find_spec", lambda name: object() if name == "requests" else None)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    font = tmp_path / env.FONT_PRIORITY[0]
    font.write_bytes(b"font")

    assert env.detect_features()["requests"] is True
    assert env.detect_features()["playwright"] is False
    assert env.detect_proxies() == {"http": "http://proxy"}
    assert env.find_chinese_fonts([tmp_path, tmp_path]) == [font]

    ctx = env.RuntimeContext("linux", "3.13", tmp_path, tmp_path, tmp_path / "cache", tmp_path / "out",
                             tmp_path / "db", [tmp_path], [font], {"requests": True}, {"http": "proxy"})
    report = env.format_runtime_report(ctx)
    assert "requests: yes" in report
    assert str(font) in report
    assert "http: proxy" in report
