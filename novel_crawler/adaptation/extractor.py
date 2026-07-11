"""Heuristic extraction of multiple scored selector candidates."""

from __future__ import annotations

import re
from collections.abc import Iterable

from bs4 import BeautifulSoup, Tag

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.acquisition.models import PageSnapshot

from .models import Candidate, Evidence, ExtractionResult, FieldKind, MetadataValue

_CHAPTER = re.compile(r"(?:第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章节回卷]|chapter\s+\d+)", re.I)
_AUTHOR = re.compile(r"^(?:作者[：:]?|作\s*者[：:]?|by\s+)(.+)$", re.I)
_PREV = re.compile(r"(?:上一[章节页]|前一[章节页]|previous(?:\s+chapter)?|prev)", re.I)
_NEXT = re.compile(r"(?:下一[章节页]|后一[章节页]|next(?:\s+chapter)?)", re.I)
_INDEX = re.compile(r"(?:目录|章节列表|返回书页|table\s+of\s+contents|contents|index)", re.I)
_NOISE = re.compile(r"(?:comment|recommend|related|advert|\bad\b|banner|footer|share|评论|推荐|广告)", re.I)
_CONTENT_HINT = re.compile(r"(?:content|article|chapter|reader|read|正文|内容)", re.I)
_SAFE_TOKEN = re.compile(r"^[A-Za-z_][\w-]{0,63}$")


class CandidateExtractor:
    """Generate plausible candidates without prematurely selecting a winner."""

    def extract(self, snapshot: PageSnapshot, page_kind: PageKind) -> ExtractionResult:
        soup = BeautifulSoup(snapshot.html, "lxml")
        candidates: list[Candidate] = []
        candidates.extend(self._headings(soup, page_kind))
        candidates.extend(self._authors(soup))
        candidates.extend(self._chapter_lists(soup))
        candidates.extend(self._content(soup, page_kind))
        candidates.extend(self._navigation(soup))
        candidates.extend(self._noise(soup))
        return ExtractionResult(tuple(sorted(candidates, key=lambda item: (item.field.value, -item.raw_score))))

    def _headings(self, soup: BeautifulSoup, page_kind: PageKind) -> Iterable[Candidate]:
        seen: set[tuple[FieldKind, str]] = set()
        for node in soup.select("h1, h2, title"):
            text = self._preview(node.get_text(" ", strip=True))
            if not text:
                continue
            field = FieldKind.CHAPTER_TITLE if page_kind is PageKind.CHAPTER and _CHAPTER.search(text) else FieldKind.TITLE
            key = (field, text)
            if key in seen:
                continue
            seen.add(key)
            score = 0.9 if node.name == "h1" else 0.65
            if field is FieldKind.CHAPTER_TITLE:
                score += 0.15
            yield self._candidate(field, node, text, score, "heading.semantic", f"tag={node.name};text_len={len(text)}")

    def _authors(self, soup: BeautifulSoup) -> Iterable[Candidate]:
        for node in soup.select("[class*=author i], [id*=author i], [class*=writer i], [id*=writer i], p, span"):
            text = node.get_text(" ", strip=True)
            match = _AUTHOR.match(text)
            if match and 0 < len(match.group(1).strip()) <= 80:
                value = self._preview(match.group(1).strip())
                yield self._candidate(FieldKind.AUTHOR, node, value, 0.85, "author.label", f"text_len={len(value)}")

    def _chapter_lists(self, soup: BeautifulSoup) -> Iterable[Candidate]:
        for node in soup.find_all(["div", "section", "ul", "ol", "main"]):
            if self._is_noise(node):
                continue
            links = [link for link in node.find_all("a", href=True, recursive=True) if _CHAPTER.search(link.get_text(" ", strip=True))]
            direct_clusters = [child for child in node.find_all(["div", "section", "ul", "ol"], recursive=False)]
            if len(links) < 3 or any(sum(1 for link in child.find_all("a", href=True) if _CHAPTER.search(link.get_text(" ", strip=True))) >= 3 for child in direct_clusters):
                continue
            score = 0.55 + min(len(links), 10) * 0.04
            yield self._candidate(
                FieldKind.CHAPTER_LIST, node, f"chapter_links={len(links)}", score,
                "chapter_list.cluster", f"link_count={len(links)}", {"link_count": len(links)},
            )

    def _content(self, soup: BeautifulSoup, page_kind: PageKind) -> Iterable[Candidate]:
        if page_kind is not PageKind.CHAPTER:
            return
        for node in soup.find_all(["article", "main", "section", "div"]):
            if self._is_noise(node) or node.find_parent(lambda tag: isinstance(tag, Tag) and self._is_noise(tag)):
                continue
            text = node.get_text(" ", strip=True)
            paragraphs = len(node.find_all("p", recursive=False))
            hint = bool(_CONTENT_HINT.search(" ".join([str(node.get("id", "")), *node.get("class", [])])))
            if len(text) < 45 or (not hint and paragraphs < 2):
                continue
            score = min(1.2, 0.35 + len(text) / 500 + paragraphs * 0.12 + (0.25 if hint else 0))
            yield self._candidate(
                FieldKind.CONTENT, node, self._preview(text), score, "content.text_density",
                f"text_len={len(text)};paragraphs={paragraphs}", {"text_length": len(text), "paragraph_count": paragraphs},
            )

    def _navigation(self, soup: BeautifulSoup) -> Iterable[Candidate]:
        for link in soup.find_all("a", href=True):
            text = link.get_text(" ", strip=True)
            rel = " ".join(link.get("rel", []))
            marker = f"{rel} {text}"
            matches = ((FieldKind.PREV_LINK, _PREV), (FieldKind.NEXT_LINK, _NEXT), (FieldKind.INDEX_LINK, _INDEX))
            for field, pattern in matches:
                if pattern.search(marker):
                    yield self._candidate(field, link, self._preview(text), 0.9 if rel else 0.7, "navigation.semantic", f"rel={rel or 'none'};text_len={len(text)}")
                    break

    def _noise(self, soup: BeautifulSoup) -> Iterable[Candidate]:
        seen: set[str] = set()
        for node in soup.find_all(True):
            marker = " ".join([str(node.get("id", "")), *node.get("class", [])])
            if not marker or not _NOISE.search(marker):
                continue
            selector = self._css_path(node)
            if selector in seen:
                continue
            seen.add(selector)
            yield self._candidate(FieldKind.CLEAN_SELECTOR, node, "noise_region", 0.85, "noise.marker", f"tag={node.name};marker_len={len(marker)}")

    @staticmethod
    def _preview(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()[:80]

    @staticmethod
    def _is_noise(node: Tag) -> bool:
        marker = " ".join([node.name, str(node.get("id", "")), *node.get("class", [])])
        return bool(_NOISE.search(marker))

    def _candidate(
        self, field: FieldKind, node: Tag, preview: str, raw_score: float, rule_id: str, detail: str,
        metadata: dict[str, MetadataValue] | None = None,
    ) -> Candidate:
        return Candidate(
            field, self._css_path(node), preview, raw_score, max(0.0, min(1.0, raw_score)),
            (Evidence(rule_id, raw_score, detail[:80]),), metadata or {},
        )

    @staticmethod
    def _css_path(node: Tag) -> str:
        node_id = node.get("id")
        if isinstance(node_id, str) and _SAFE_TOKEN.fullmatch(node_id):
            return f"#{node_id}"
        classes = [item for item in node.get("class", []) if isinstance(item, str) and _SAFE_TOKEN.fullmatch(item)]
        if classes:
            return "." + ".".join(classes[:2])
        parts: list[str] = []
        current: Tag | None = node
        while current is not None and current.name != "[document]" and len(parts) < 4:
            siblings = [sibling for sibling in current.parent.find_all(current.name, recursive=False)] if isinstance(current.parent, Tag) else []
            suffix = f":nth-of-type({siblings.index(current) + 1})" if len(siblings) > 1 else ""
            parts.append(f"{current.name}{suffix}")
            current = current.parent if isinstance(current.parent, Tag) else None
        return " > ".join(reversed(parts))
