from types import SimpleNamespace

import pytest

import novel_crawler.cli as cli
import novel_crawler.runtime.env as env


class CloseOnlyApplication:
    def close(self) -> bool:
        return True


def test_cli_never_exposes_or_constructs_legacy_crawler_service() -> None:
    assert not hasattr(cli, "CrawlerService")


@pytest.mark.parametrize("command", ["inspect", "wizard"])
def test_legacy_network_configuration_commands_are_disabled(command, tmp_path, capsys) -> None:
    assert cli.main(
        [command, "https://example.com/book"],
        project_dir=tmp_path,
        application_factory=lambda _ctx: CloseOnlyApplication(),
    ) == 2
    assert "legacy_command_disabled" in capsys.readouterr().err


def test_decode_font_without_discovered_fonts_returns_safe_failure(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cli, "create_runtime_context", lambda *_args: SimpleNamespace(chinese_fonts=[]))
    assert cli.main(["decode-font", "font.ttf"], project_dir=tmp_path) == 3
    assert "参考字体" in capsys.readouterr().err


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
    assert "playwright" not in env.detect_features()
    assert env.detect_proxies() == {"http": "http://proxy"}
    assert env.find_chinese_fonts([tmp_path, tmp_path]) == [font]

    ctx = env.RuntimeContext("linux", "3.13", tmp_path, tmp_path, tmp_path / "cache", tmp_path / "out",
                             tmp_path / "db", [tmp_path], [font], {"requests": True}, {"http": "proxy"})
    report = env.format_runtime_report(ctx)
    assert "requests: yes" in report
    assert str(font) in report
    assert "http: proxy" in report
