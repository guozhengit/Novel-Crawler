from pathlib import Path

import pytest

from novel_crawler.runtime import env
from novel_crawler.runtime.env import create_runtime_context


def test_default_data_dir_uses_platform_base(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    if env.os.name == "nt" or env.platform.system().lower() != "darwin":
        expected_base = tmp_path
    else:
        expected_base = Path.home() / "Library" / "Application Support"
    assert env.default_data_dir("test-app") == expected_base / "test-app"


def test_explicit_data_dir_is_used(tmp_path):
    data_dir = tmp_path / "state"
    ctx = create_runtime_context(project_dir=tmp_path / "source", data_dir=data_dir)
    assert ctx.data_dir == data_dir.resolve()
    assert ctx.cache_dir == data_dir.resolve() / "cache"
    assert ctx.output_dir == data_dir.resolve() / "output"
    assert ctx.db_path == data_dir.resolve() / "crawler.db"


@pytest.mark.parametrize("legacy_entry", ["crawler.db", "cache", "contents", "output"])
def test_existing_legacy_project_data_is_reused(monkeypatch, tmp_path, legacy_entry):
    project_dir = tmp_path / "source"
    legacy_data_dir = project_dir / "data"
    legacy_data_dir.mkdir(parents=True)
    entry = legacy_data_dir / legacy_entry
    if legacy_entry == "crawler.db":
        entry.touch()
    else:
        entry.mkdir()
    platform_data_dir = tmp_path / "platform-data"
    monkeypatch.setattr(env, "default_data_dir", lambda: platform_data_dir)

    ctx = create_runtime_context(project_dir=project_dir)

    assert ctx.data_dir == legacy_data_dir.resolve()


def test_empty_legacy_project_data_uses_platform_default(monkeypatch, tmp_path):
    project_dir = tmp_path / "source"
    (project_dir / "data").mkdir(parents=True)
    platform_data_dir = tmp_path / "platform-data"
    monkeypatch.setattr(env, "default_data_dir", lambda: platform_data_dir)

    ctx = create_runtime_context(project_dir=project_dir)

    assert ctx.data_dir == platform_data_dir.resolve()


def test_explicit_data_dir_overrides_existing_legacy_data(monkeypatch, tmp_path):
    project_dir = tmp_path / "source"
    legacy_data_dir = project_dir / "data"
    (legacy_data_dir / "cache").mkdir(parents=True)
    explicit_data_dir = tmp_path / "explicit-data"
    monkeypatch.setattr(env, "default_data_dir", lambda: tmp_path / "platform-data")

    ctx = create_runtime_context(project_dir=project_dir, data_dir=explicit_data_dir)

    assert ctx.data_dir == explicit_data_dir.resolve()


def test_runtime_creates_required_directories(tmp_path):
    ctx = create_runtime_context(data_dir=tmp_path / "state")
    assert ctx.data_dir.is_dir()
    assert ctx.cache_dir.is_dir()
    assert ctx.output_dir.is_dir()
