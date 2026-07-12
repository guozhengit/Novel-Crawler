from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from novel_crawler.acquisition.models import AcquiredPage, PageSnapshot
from novel_crawler.adaptation.service import ProbeService
from novel_crawler.browser import BrowserPageSnapshot

pytestmark = pytest.mark.release

FIXTURES = Path(__file__).parent / "fixtures"


class FixtureAcquirer:
    def __init__(self, pages: dict[str, str], *, browser_rendered: bool) -> None:
        self.pages = pages
        self.browser_rendered = browser_rendered
        self.calls: list[str] = []

    def fetch_page(
        self,
        url: str,
        *,
        max_body_bytes: int | None = None,
        locked_origin: str | None = None,
    ) -> AcquiredPage:
        del locked_origin
        self.calls.append(url)
        body = self.pages[url].encode("utf-8")
        assert max_body_bytes is None or len(body) <= max_body_bytes
        if self.browser_rendered:
            snapshot = BrowserPageSnapshot(url, url, 200, {"content-type": "text/html; charset=utf-8"}, body).to_page_snapshot()
        else:
            snapshot = PageSnapshot(
                url,
                url,
                200,
                {"content-type": "text/html; charset=utf-8"},
                "utf-8",
                self.pages[url],
                body,
                "GET",
                (),
                datetime.now(UTC),
            )
        return AcquiredPage(snapshot, url)


def _pages(case: dict[str, object]) -> tuple[str, dict[str, str]]:
    case_id = str(case["id"])
    wrapper = str(case["wrapper"])
    content = str(case["content"])
    paragraph_count = int(case["paragraphs"])
    base = f"https://fixture.test/{case_id}"
    book_name = f"Fixture Book {case_id}"
    index = (
        f'<meta name="book_name" content="{book_name}"><h1 class="book-title">{book_name}</h1>'
        f'<div id="list" class="{wrapper}">'
        '<a href="chapter-1">Chapter 1</a><a href="chapter-2">Chapter 2</a>'
        '<a href="chapter-3">Chapter 3</a></div>'
    )
    pages = {f"{base}/book": index}
    for number in (1, 2):
        paragraphs = "".join(
            f"<p>{case_id} chapter {number} paragraph {part} " + ("content " * 18) + "</p>"
            for part in range(1, paragraph_count + 1)
        )
        next_link = f'<a rel="next" href="chapter-{number + 1}">Next</a>' if number == 1 else ""
        pages[f"{base}/chapter-{number}"] = (
            f'<meta name="book_name" content="{book_name}"><body class="{wrapper}">'
            f"<h1>Chapter {number}</h1><{content} class=\"chapter-content\">{paragraphs}</{content}>"
            f"{next_link}</body>"
        )
    return f"{base}/book", pages


def _run_benchmark() -> dict[str, list[tuple[bool, tuple[str, ...]]]]:
    cases = json.loads((FIXTURES / "adaptation_cases.json").read_text(encoding="utf-8"))
    results: dict[str, list[tuple[bool, tuple[str, ...]]]] = defaultdict(list)
    for case in cases:
        start, pages = _pages(case)
        is_js = case["kind"] == "javascript"
        outcome = ProbeService(acquirer=FixtureAcquirer(pages, browser_rendered=is_js)).probe(start)
        results[str(case["kind"])].append((outcome.ok, outcome.reason_ids or ("ok",)))
    return results


def test_redistributable_adaptation_benchmark_meets_release_thresholds() -> None:
    manifest = json.loads((FIXTURES / "benchmark_manifest.json").read_text(encoding="utf-8"))
    assert manifest["license"] == "CC0-1.0"
    assert "third-party" in manifest["source"]
    assert manifest["execution"]["javascript"] == "synthetic-browser-page-snapshot"

    first = _run_benchmark()
    second = _run_benchmark()

    assert len(first["static"]) >= 10
    assert len(first["javascript"]) >= 10
    for kind in ("static", "javascript"):
        rate = sum(ok for ok, _reasons in first[kind]) / len(first[kind])
        assert rate >= manifest["thresholds"][kind]
        assert first[kind] == second[kind], "outcomes and reason codes must be stable"
        assert all(reasons == ("ok",) for ok, reasons in first[kind] if ok)
pytestmark = pytest.mark.release
