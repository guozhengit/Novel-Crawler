from novel_crawler.cli import build_parser, main


def test_help_contains_readable_chinese(capsys):
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    output = capsys.readouterr().out
    assert "通用小说爬虫系统" in output
    assert "抓取小说" in output
    assert "閫" not in output


def test_parser_exposes_existing_commands():
    parser = build_parser()
    commands = parser._subparsers._group_actions[0].choices
    assert set(commands) == {
        "env", "books", "delete", "crawl", "inspect", "wizard", "resume",
        "progress", "validate", "logs", "report", "retry-failed", "export",
        "decode-font", "web", "fix-titles", "dedup", "export-all", "retry-all",
        "crawl-batch", "preview", "stats", "validate-config",
    }


def test_env_command_returns_success(tmp_path, capsys):
    assert main(["env"], project_dir=tmp_path) == 0
    output = capsys.readouterr().out
    assert "Runtime:" in output
    assert str(tmp_path) in output


def test_data_dir_override_preserves_project_dir(tmp_path, capsys):
    project_dir = tmp_path / "source"
    data_dir = tmp_path / "state"

    assert main(["--data-dir", str(data_dir), "env"], project_dir=project_dir) == 0

    output = capsys.readouterr().out
    assert str(project_dir.resolve()) in output
    assert str(data_dir.resolve() / "cache") in output
    assert (data_dir / "cache").is_dir()
    assert (data_dir / "output").is_dir()
