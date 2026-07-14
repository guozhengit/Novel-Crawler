from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import soupsieve
from bs4 import BeautifulSoup, Tag

from novel_crawler.acquisition.http import HttpPageAcquirer
from novel_crawler.core.domains import canonical_domain

CHAPTER_TEXT = re.compile(r"(?:第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章节回卷]|chapter\s*\d+)", re.I)
CHAPTER_PATH = re.compile(r"(?:/book/[^/?#]+-\d+|/chapter/|/\d+\.html|/\d+/?$)", re.I)


@dataclass(frozen=True)
class HtmlSample:
    url: str
    html: str


def explore_site(
    url: str,
    *,
    sample: int = 3,
    acquirer: HttpPageAcquirer | None = None,
) -> dict[str, Any]:
    if isinstance(sample, bool) or not 1 <= sample <= 5:
        raise ValueError("sample must be between 1 and 5")
    fetcher = acquirer or HttpPageAcquirer(max_body_bytes=1_048_576)
    start = _fetch(fetcher, url)
    catalog_url = _catalog_url(start)
    catalog = start if catalog_url == start.url else _fetch(fetcher, catalog_url)
    catalog_analysis = _analyze_catalog(catalog)
    chapter_urls = [item["url"] for item in catalog_analysis["chapter_links"][:sample]]
    chapter_samples = [_fetch(fetcher, chapter_url) for chapter_url in chapter_urls]
    chapter_analysis = _analyze_chapters(chapter_samples)
    domain = canonical_domain(urlsplit(catalog.url).hostname or "")
    config = _proposed_config(domain, catalog_analysis, chapter_analysis)
    warnings = _warnings(catalog_analysis, chapter_analysis)
    return {
        "schema": "novel-crawler-exploration-v1",
        "source_url": url,
        "domain": domain,
        "sample_count": len(chapter_samples),
        "catalog": catalog_analysis,
        "chapters": chapter_analysis,
        "warnings": warnings,
        "requires_dedicated_adapter": any(item["code"] == "chapter_has_pagination" for item in warnings),
        "proposed_config": config,
    }


def write_report(report: dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def propose_config_from_report(report_path: Path, output: Path) -> Path:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    config = report.get("proposed_config")
    if not isinstance(config, dict):
        raise ValueError("report does not contain proposed_config")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def _fetch(acquirer: HttpPageAcquirer, url: str) -> HtmlSample:
    page = acquirer.fetch_page(url, max_body_bytes=1_048_576, locked_origin=_origin(url))
    return HtmlSample(page.navigation_url, page.snapshot.html)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def _catalog_url(sample: HtmlSample) -> str:
    soup = BeautifulSoup(sample.html, "lxml")
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        if text in {"目录", "返回目录", "章节列表", "书页"}:
            return urljoin(sample.url, str(anchor.get("href")))
    return sample.url


def _analyze_catalog(sample: HtmlSample) -> dict[str, Any]:
    soup = BeautifulSoup(sample.html, "lxml")
    links = _chapter_links(soup, sample.url)
    container = _best_chapter_container(soup, links)
    return {
        "url": sample.url,
        "title_selector": _first_existing(soup, ("meta[property='og:novel:book_name']", "meta[property='og:title']", "h1", ".book-title", ".name")),
        "author_selector": _first_existing(soup, ("meta[property='og:novel:author']", ".author", ".writer", "[class*=author i]", "[class*=writer i]")),
        "chapter_list_selector": container["selector"],
        "chapter_link_count": len(links),
        "chapter_links": [
            {"title": link.get_text(" ", strip=True), "url": urljoin(sample.url, str(link.get("href")))}
            for link in links[:20]
        ],
        "candidates": {
            "chapter_list": container["candidates"],
        },
    }


def _analyze_chapters(samples: list[HtmlSample]) -> dict[str, Any]:
    analyses = []
    content_selectors = []
    title_selectors = []
    for sample in samples:
        soup = BeautifulSoup(sample.html, "lxml")
        content = _content_selector(soup)
        title = _first_existing(soup, ("h1.title", ".readCon h1", "h1", ".chapter-title"))
        content_selectors.append(content["selector"])
        title_selectors.append(title)
        analyses.append(
            {
                "url": sample.url,
                "title_selector": title,
                "content_selector": content["selector"],
                "content_chars": content["chars"],
                "paragraphs": content["paragraphs"],
                "has_next_page": _has_next_page(soup),
                "candidates": {"content": content["candidates"]},
            }
        )
    return {
        "samples": analyses,
        "title_selector": _common_or_first(title_selectors),
        "content_selector": _common_or_first(content_selectors),
        "paragraph_selector": "p",
        "sample_min_chars": min((item["content_chars"] for item in analyses), default=0),
        "sample_min_paragraphs": min((item["paragraphs"] for item in analyses), default=0),
        "has_paginated_chapter": any(item["has_next_page"] for item in analyses),
    }


def _chapter_links(soup: BeautifulSoup, base_url: str) -> list[Tag]:
    links: list[Tag] = []
    seen = set()
    origin = urlsplit(base_url).netloc
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", ""))
        text = anchor.get_text(" ", strip=True)
        full = urljoin(base_url, href)
        if urlsplit(full).netloc != origin:
            continue
        if not (CHAPTER_TEXT.search(text) or CHAPTER_PATH.search(urlsplit(full).path)):
            continue
        key = _chapter_key(full)
        if key in seen:
            continue
        seen.add(key)
        links.append(anchor)
    return sorted(links, key=lambda item: _chapter_sort_key(urljoin(base_url, str(item.get("href")))))


def _chapter_key(url: str) -> str:
    path = urlsplit(url).path
    path = re.sub(r"(-\d+)(?:-\d+)(\.html)$", r"\1\2", path)
    return path


def _chapter_sort_key(url: str) -> tuple[int, str]:
    match = re.search(r"-(\d+)(?:-\d+)?\.html$", urlsplit(url).path) or re.search(r"/(\d+)(?:\.html)?/?$", urlsplit(url).path)
    return (int(match.group(1)) if match else 10**9, url)


def _best_chapter_container(soup: BeautifulSoup, links: list[Tag]) -> dict[str, Any]:
    candidates = []
    for node in soup.find_all(["div", "section", "ul", "ol"]):
        node_links = [link for link in node.find_all("a", href=True) if link in links]
        if len(node_links) < 2:
            continue
        selector = _unique_css(soup, node)
        if selector:
            candidates.append({"selector": f"{selector} a", "link_count": len(node_links)})
    candidates.sort(key=lambda item: item["link_count"])
    if candidates:
        return {"selector": candidates[0]["selector"], "candidates": candidates[:5]}
    return {"selector": "a", "candidates": []}


def _content_selector(soup: BeautifulSoup) -> dict[str, Any]:
    candidates = []
    for node in soup.find_all(["article", "main", "section", "div"]):
        marker = " ".join([str(node.get("id", "")), *[str(item) for item in node.get("class", [])]])
        if not re.search(r"(?:content|chapter|read|article|entry|正文)", marker, re.I):
            continue
        text = node.get_text(" ", strip=True)
        paragraphs = len(node.find_all("p"))
        if len(text) < 45 or paragraphs < 1:
            continue
        selector = _unique_css(soup, node)
        if selector:
            candidates.append({"selector": selector, "chars": len(text), "paragraphs": paragraphs})
    candidates.sort(key=lambda item: (item["chars"], item["paragraphs"]), reverse=True)
    if candidates:
        best = dict(candidates[0])
        best["candidates"] = candidates[:5]
        return best
    return {"selector": "body", "chars": len(soup.get_text(" ", strip=True)), "paragraphs": len(soup.find_all("p")), "candidates": []}


def _has_next_page(soup: BeautifulSoup) -> bool:
    for anchor in soup.find_all("a", href=True):
        if anchor.get_text(" ", strip=True) == "下一页":
            return True
    return False


def _first_existing(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        if soup.select_one(selector):
            return selector
    return ""


def _common_or_first(values: list[str]) -> str:
    values = [value for value in values if value]
    if not values:
        return ""
    for value in values:
        if values.count(value) == len(values):
            return value
    return values[0]


def _proposed_config(domain: str, catalog: dict[str, Any], chapters: dict[str, Any]) -> dict[str, Any]:
    site = domain.split(".")[0]
    return {
        "site": site,
        "domain": [domain],
        "book": {
            "title_selector": catalog.get("title_selector") or "h1",
            "author_selector": catalog.get("author_selector") or ".author",
            "chapter_list_selector": catalog.get("chapter_list_selector") or "a",
        },
        "chapter": {
            "title_selector": chapters.get("title_selector") or "h1",
            "content_selector": chapters.get("content_selector") or "body",
            "paragraph_selector": chapters.get("paragraph_selector") or "p",
        },
        "clean": {
            "remove_selectors": ["script", "style", "iframe", ".ad", ".ads", ".recommend", ".footer"],
            "remove_text_contains": ["请收藏本站", "最新网址", "章节错误", "加入书签"],
        },
        "request": {
            "delay_min": 2.0,
            "delay_max": 6.0,
            "retries": 3,
            "timeout": 25,
        },
    }


def _warnings(catalog: dict[str, Any], chapters: dict[str, Any]) -> list[dict[str, str]]:
    warnings = []
    if int(catalog.get("chapter_link_count") or 0) < 2:
        warnings.append({"code": "catalog_links_low", "message": "目录页章节链接不足，候选配置需要人工确认"})
    if int(chapters.get("sample_min_chars") or 0) < 200:
        warnings.append({"code": "content_short", "message": "样本正文偏短，可能误选正文容器"})
    if chapters.get("has_paginated_chapter"):
        warnings.append({"code": "chapter_has_pagination", "message": "检测到章内分页，通用配置不能自动合并分页，建议生成专属适配器"})
    return warnings


def _unique_css(soup: BeautifulSoup, node: Tag) -> str | None:
    node_id = node.get("id")
    if isinstance(node_id, str) and node_id:
        selector = f"#{soupsieve.escape(node_id)}"
        if soup.select(selector) == [node]:
            return selector
    classes = [item for item in node.get("class", []) if isinstance(item, str) and item]
    for class_name in classes:
        selector = f"{soupsieve.escape(node.name)}.{soupsieve.escape(class_name)}"
        if soup.select(selector) == [node]:
            return selector
    current: Tag | None = node
    parts: list[str] = []
    while current is not None and current.name != "[document]":
        parent = current.parent
        if not isinstance(parent, Tag):
            break
        siblings = parent.find_all(current.name, recursive=False)
        segment = soupsieve.escape(current.name)
        if len(siblings) > 1:
            segment += f":nth-of-type({siblings.index(current) + 1})"
        parts.append(segment)
        selector = " > ".join(reversed(parts))
        if soup.select(selector) == [node]:
            return selector
        current = parent
    return None
