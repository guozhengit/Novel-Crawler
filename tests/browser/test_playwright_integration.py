from __future__ import annotations

import os
import socket
import threading
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
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen()
    port = upstream.getsockname()[1]

    def serve() -> None:
        connection, _ = upstream.accept()
        with connection:
            connection.recv(4096)
            body = b"<title>Proxy integration</title><p>ok</p>"
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "
                + str(len(body)).encode("ascii")
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    policy = UrlSafetyPolicy(resolver=lambda host, requested_port: ("127.0.0.1",))
    policy._require_public = lambda address, safe_url: None  # type: ignore[method-assign]
    guard = BrowserRequestPolicy(policy)
    guard.lock(f"http://example.test:{port}/")
    context = DefaultPlaywrightDriver().launch(user_data_dir=tmp_path / "profile", headless=True, policy=guard)
    try:
        snapshot = context.navigate(f"http://example.test:{port}/")
        assert b"Proxy integration" in snapshot.body
    finally:
        context.close()
        upstream.close()
        thread.join(2)
