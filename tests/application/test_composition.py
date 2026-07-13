from __future__ import annotations

from pathlib import Path

import pytest

from novel_crawler.application.composition import _close_crawler, build_application
from novel_crawler.runtime.env import create_runtime_context


class NeverLaunchDriver:
    def __init__(self) -> None:
        self.calls = 0

    def launch(self, **_kwargs):
        self.calls += 1
        raise AssertionError("browser must be lazy")


class FailingAcquirer:
    def fetch_page(self, _url: str, **_kwargs):
        raise RuntimeError("not used during construction")


def test_composition_uses_private_data_directories_without_browser_state(tmp_path: Path) -> None:
    ctx = create_runtime_context(tmp_path / "project", tmp_path / "private-data")
    driver = NeverLaunchDriver()
    app = build_application(ctx, driver=driver, http_acquirer=FailingAcquirer(), recover_on_start=True)
    assert (ctx.data_dir / "tasks.db").exists()
    assert (ctx.data_dir / "config-registry").is_dir()
    assert not (ctx.data_dir / "browser-sessions").exists()
    assert driver.calls == 0
    assert app.close() is True
    assert app.close() is True


def test_composition_failure_closes_already_created_crawler(monkeypatch, tmp_path: Path) -> None:
    ctx = create_runtime_context(tmp_path / "project", tmp_path / "failure-data")
    closed: list[bool] = []

    class BrokenExecutor:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("construction failed")

    monkeypatch.setattr("novel_crawler.application.composition.BackgroundTaskExecutor", BrokenExecutor)

    def track_close(crawler):
        closed.append(True)
        crawler.storage.close()

    monkeypatch.setattr("novel_crawler.application.composition._close_crawler", track_close)
    try:
        build_application(ctx, driver=NeverLaunchDriver(), http_acquirer=FailingAcquirer())
    except RuntimeError as exc:
        assert str(exc) == "construction failed"
    else:
        raise AssertionError("construction should fail")
    assert closed == [True]


def test_composition_cleans_every_dependency_when_final_service_construction_fails(monkeypatch, tmp_path: Path) -> None:
    ctx = create_runtime_context(tmp_path / "project", tmp_path / "final-failure")
    monkeypatch.setattr(
        "novel_crawler.application.composition.ApplicationService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("final failed")),
    )
    with pytest.raises(RuntimeError, match="final failed"):
        build_application(ctx, driver=NeverLaunchDriver(), http_acquirer=FailingAcquirer())


def test_close_crawler_supports_close_method_and_storage_fallback() -> None:
    calls: list[str] = []

    class WithClose:
        def close(self):
            calls.append("close")

    class StorageOnly:
        class Storage:
            def close(self):
                calls.append("storage")

        storage = Storage()

    _close_crawler(WithClose())  # type: ignore[arg-type]
    _close_crawler(StorageOnly())  # type: ignore[arg-type]
    assert calls == ["close", "storage"]


def test_constructor_failure_never_closes_worker_dependencies_when_shutdown_fails(
    monkeypatch, tmp_path: Path
) -> None:
    from novel_crawler.core.crawler import CrawlerService
    from novel_crawler.task_engine import TaskRepository

    class TrackingRepository(TaskRepository):
        instance = None

        def __init__(self, path):
            super().__init__(path)
            self.close_calls = 0
            TrackingRepository.instance = self

        def close(self):
            self.close_calls += 1
            super().close()

    class TrackingCrawler(CrawlerService):
        instance = None

        def __init__(self, ctx):
            super().__init__(ctx)
            self.close_calls = 0
            TrackingCrawler.instance = self

        def close(self):
            self.close_calls += 1
            self.storage.close()

    class NonStoppingExecutor:
        def __init__(self, *_args, **_kwargs): pass
        def schedule_active(self, _task_id): return False
        def recover_and_schedule(self): return 0
        def shutdown(self, *, wait, timeout): return False

    monkeypatch.setattr("novel_crawler.application.composition.TaskRepository", TrackingRepository)
    monkeypatch.setattr("novel_crawler.application.composition.CrawlerService", TrackingCrawler)
    monkeypatch.setattr("novel_crawler.application.composition.BackgroundTaskExecutor", NonStoppingExecutor)
    monkeypatch.setattr(
        "novel_crawler.application.composition.ApplicationService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("final failed")),
    )
    ctx = create_runtime_context(tmp_path / "project", tmp_path / "non-stopping")
    with pytest.raises(RuntimeError, match="final failed"):
        build_application(ctx, driver=NeverLaunchDriver(), http_acquirer=FailingAcquirer())
    assert TrackingRepository.instance.close_calls == 0
    assert TrackingCrawler.instance.close_calls == 0
    TrackingRepository.instance.close()
    TrackingCrawler.instance.close()
