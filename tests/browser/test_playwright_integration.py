from __future__ import annotations

import os
from pathlib import Path

import pytest

from novel_crawler.acquisition.security import UrlSafetyPolicy
from novel_crawler.browser.driver import BrowserRequestPolicy, DefaultPlaywrightDriver


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_PLAYWRIGHT_INTEGRATION") != "1",
    reason="set RUN_PLAYWRIGHT_INTEGRATION=1 when a Playwright browser is installed",
)
def test_real_playwright_can_launch_persistent_headless_context(tmp_path: Path) -> None:
    policy = BrowserRequestPolicy(UrlSafetyPolicy(resolver=lambda host, port: ("93.184.216.34",)))
    policy.lock("https://example.com/")
    context = DefaultPlaywrightDriver().launch(user_data_dir=tmp_path / "profile", headless=True, policy=policy)
    context.close()

