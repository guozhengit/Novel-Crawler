from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot


def test_acquired_page_keeps_navigation_url_private_and_immutable() -> None:
    snapshot = PageSnapshot("https://example.test/private?q=x", "https://example.test/private?q=x", 200, {}, "utf-8", "ok", b"ok", "GET", (), datetime.now(UTC))
    page = AcquiredPage(snapshot, "https://example.test/private?q=x")
    assert page.navigation_url.endswith("/private?q=x")
    assert "private" not in repr(page) and "?q=x" not in repr(page)
    with pytest.raises(TypeError):
        asdict(page)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        page.navigation_url = "x"  # type: ignore[misc]


def test_snapshot_and_acquired_page_repr_hide_dom_body_and_headers() -> None:
    snapshot = PageSnapshot(
        "https://example.test/private",
        "https://example.test/private",
        200,
        {"set-cookie": "dom-secret"},
        "utf-8",
        "<html>dom-secret</html>",
        b"dom-secret",
        "GET",
        (),
        datetime.now(UTC),
    )
    assert "dom-secret" not in repr(snapshot)
    assert "dom-secret" not in repr(AcquiredPage(snapshot, "https://example.test/private"))
