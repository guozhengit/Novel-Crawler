"""Configurable heuristic extraction of scored selector candidates."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

import soupsieve
from bs4 import BeautifulSoup, Tag

from novel_crawler.acquisition.classifier import PageKind
from novel_crawler.acquisition.models import PageSnapshot

from .models import Candidate, Evidence, ExtractionResult, FieldKind, MetadataValue

DEFAULT_CHAPTER_PATTERNS = (
    r"第\s*[0-9零一二三四五六七八九十百千万两]+(?:\.[0-9]+)?\s*[章节回卷]",
    r"(?:chapter|part|book)\s+(?:\d+(?:\.\d+)?|[ivxlcdm]+)\b",
    r"(?:prologue|epilogue)\b",
    r"(?:foreword|afterword|interlude)\b",
    r"chapter\s+(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)(?:[ -]+|\b))+",
    r"(?:序章|楔子|番外(?:\s*\d+)?)",
)
DEFAULT_AUTHOR_PATTERNS = (r"^(?:作者\s*[：:]?|by\s+|written\s+by\s+|author\s*[：:]\s*)(.+)$",)
DEFAULT_NOISE_PATTERN = r"(?:comment|recommend|related|advert|\bad\b|banner|footer|share|评论|推荐|广告)"


@dataclass(frozen=True)
class ExtractorConfig:
    min_chapter_links: int = 3
    min_content_chars: int = 45
    chapter_title_patterns: tuple[str, ...] = DEFAULT_CHAPTER_PATTERNS
    author_patterns: tuple[str, ...] = DEFAULT_AUTHOR_PATTERNS
    noise_pattern: str = DEFAULT_NOISE_PATTERN
    enabled_fields: frozenset[FieldKind] = frozenset(FieldKind)
    version: str = "v2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "chapter_title_patterns", tuple(self.chapter_title_patterns))
        object.__setattr__(self, "author_patterns", tuple(self.author_patterns))
        object.__setattr__(self, "enabled_fields", frozenset(self.enabled_fields))
        if self.min_chapter_links < 1 or self.min_content_chars < 1:
            raise ValueError("thresholds must be positive")
        if not self.chapter_title_patterns or not self.author_patterns:
            raise ValueError("pattern sets must not be empty")
        for pattern in (*self.chapter_title_patterns, *self.author_patterns, self.noise_pattern):
            re.compile(pattern)
        if not self.enabled_fields or not all(isinstance(field, FieldKind) for field in self.enabled_fields):
            raise ValueError("enabled_fields must contain FieldKind values")


@runtime_checkable
class ExtractionRule(Protocol):
    rule_id: str
    fields: frozenset[FieldKind]

    def extract(self, extractor: CandidateExtractor, soup: BeautifulSoup, page_kind: PageKind) -> Iterable[Candidate]: ...


@dataclass(frozen=True)
class _BuiltinRule:
    rule_id: str
    fields: frozenset[FieldKind]
    method_name: str

    def extract(self, extractor: CandidateExtractor, soup: BeautifulSoup, page_kind: PageKind) -> Iterable[Candidate]:
        method: Callable[[BeautifulSoup, PageKind], Iterable[Candidate]] = getattr(extractor, self.method_name)
        return method(soup, page_kind)


class CandidateExtractor:
    """Generate multiple private candidates from modular extraction rules."""

    def __init__(self, rules: Sequence[ExtractionRule] | None = None, config: ExtractorConfig | None = None) -> None:
        self.config = config or ExtractorConfig()
        self._chapter = re.compile("|".join(f"(?:{item})" for item in self.config.chapter_title_patterns), re.I)
        self._authors = tuple(re.compile(item, re.I) for item in self.config.author_patterns)
        self._noise_pattern = re.compile(self.config.noise_pattern, re.I)
        self.rules: tuple[ExtractionRule, ...] = tuple(rules) if rules is not None else self._builtin_rules()
        if not self.rules or not all(isinstance(rule, ExtractionRule) for rule in self.rules):
            raise TypeError("rules must implement ExtractionRule")

    @staticmethod
    def _builtin_rules() -> tuple[ExtractionRule, ...]:
        return cast(tuple[ExtractionRule, ...], (
            _BuiltinRule("heading", frozenset({FieldKind.TITLE, FieldKind.CHAPTER_TITLE}), "_headings"),
            _BuiltinRule("author", frozenset({FieldKind.AUTHOR}), "_author_candidates"),
            _BuiltinRule("chapter_list", frozenset({FieldKind.CHAPTER_LIST}), "_chapter_lists"),
            _BuiltinRule("content", frozenset({FieldKind.CONTENT}), "_content"),
            _BuiltinRule("navigation", frozenset({FieldKind.PREV_LINK, FieldKind.NEXT_LINK, FieldKind.INDEX_LINK}), "_navigation"),
            _BuiltinRule("noise", frozenset({FieldKind.CLEAN_SELECTOR}), "_noise"),
        ))

    def extract(self, snapshot: PageSnapshot, page_kind: PageKind) -> ExtractionResult:
        soup = BeautifulSoup(snapshot.html, "lxml")
        candidates = [candidate for rule in self.rules if rule.fields & self.config.enabled_fields for candidate in rule.extract(self, soup, page_kind) if candidate.field in self.config.enabled_fields]
        candidates.sort(key=lambda item: (item.field.value, -item.raw_score, item.selector))
        provenance = "builtin" if all(isinstance(rule, _BuiltinRule) for rule in self.rules) else "custom"
        return ExtractionResult(tuple(candidates), provenance, self.config.version)

    def _headings(self, soup: BeautifulSoup, page_kind: PageKind) -> Iterable[Candidate]:
        seen: set[tuple[FieldKind, str]] = set()
        for node in soup.select("h1, h2, title"):
            text = node.get_text(" ", strip=True)
            if not text:
                continue
            field = FieldKind.CHAPTER_TITLE if page_kind is PageKind.CHAPTER and self._chapter.search(text) else FieldKind.TITLE
            summary = self._summary(text)
            if (field, summary) in seen:
                continue
            seen.add((field, summary))
            score = (0.9 if node.name == "h1" else 0.65) + (0.15 if field is FieldKind.CHAPTER_TITLE else 0)
            candidate = self._candidate(soup, field, node, summary, score, "heading.semantic", f"tag={node.name};length_bucket={self._length_bucket(len(text))}")
            if candidate:
                yield candidate

    def _author_candidates(self, soup: BeautifulSoup, page_kind: PageKind) -> Iterable[Candidate]:
        del page_kind
        nodes = soup.select("[class*=author i], [id*=author i], [class*=writer i], [id*=writer i], p, span")
        for node in nodes:
            text = node.get_text(" ", strip=True)
            author = next((match.group(1).strip() for pattern in self._authors if (match := pattern.match(text))), "")
            if author:
                candidate = self._candidate(soup, FieldKind.AUTHOR, node, self._summary(author), 0.85, "author.label", f"length_bucket={self._length_bucket(len(author))}")
                if candidate:
                    yield candidate
        for label in soup.find_all(string=lambda value: bool(value and re.fullmatch(r"\s*作者\s*[：:]?\s*", value))):
            parent = label.parent
            sibling = parent.find_next_sibling() if isinstance(parent, Tag) else None
            if isinstance(sibling, Tag) and (text := sibling.get_text(" ", strip=True)):
                candidate = self._candidate(soup, FieldKind.AUTHOR, sibling, self._summary(text), 0.75, "author.sibling", f"length_bucket={self._length_bucket(len(text))}")
                if candidate:
                    yield candidate

    def _chapter_lists(self, soup: BeautifulSoup, page_kind: PageKind) -> Iterable[Candidate]:
        del page_kind
        for node in soup.find_all(["div", "section", "ul", "ol", "main"]):
            if self._in_noise(node):
                continue
            anchors = node.find_all("a", href=True)
            chapter_links = [link for link in anchors if self._chapter.search(link.get_text(" ", strip=True))]
            children = node.find_all(["div", "section", "ul", "ol"], recursive=False)
            if len(chapter_links) < self.config.min_chapter_links:
                continue
            if any(len([link for link in child.find_all("a", href=True) if self._chapter.search(link.get_text(" ", strip=True))]) >= self.config.min_chapter_links for child in children):
                continue
            container = self._unique_css(soup, node)
            if not container:
                continue
            selected = self._chapter_anchor_selector(soup, container, chapter_links)
            if not selected:
                continue
            selector, strategy = selected
            score = 0.55 + min(len(chapter_links), 10) * 0.04
            metadata: dict[str, MetadataValue] = {"link_count": len(chapter_links), "container_selector": container, "link_text_rule": "chapter_title.v2", "selector_strategy": strategy}
            yield self._make(FieldKind.CHAPTER_LIST, selector, f"link_count={len(chapter_links)}", score, "chapter_list.cluster", f"link_count={len(chapter_links)}", metadata)

    def _content(self, soup: BeautifulSoup, page_kind: PageKind) -> Iterable[Candidate]:
        if page_kind is not PageKind.CHAPTER:
            return
        for node in soup.find_all(["article", "main", "section", "div"]):
            if self._in_noise(node):
                continue
            clone = BeautifulSoup(str(node), "lxml").find(node.name)
            if not isinstance(clone, Tag):
                continue
            for descendant in list(clone.find_all(True)):
                if descendant.attrs is not None and self._is_noise(descendant):
                    descendant.decompose()
            text = clone.get_text(" ", strip=True)
            paragraphs = len(clone.find_all("p"))
            marker = " ".join([str(node.get("id", "")), *node.get("class", [])])
            hint = node.name == "article" or bool(re.search(r"(?:content|article|chapter|reader|read|正文|内容)", marker, re.I))
            if len(text) < self.config.min_content_chars or not hint and paragraphs < 2:
                continue
            score = min(1.2, 0.35 + len(text) / 500 + paragraphs * 0.12 + (0.25 if hint else 0))
            candidate = self._candidate(soup, FieldKind.CONTENT, node, self._summary(text), score, "content.text_density", f"length_bucket={self._length_bucket(len(text))};paragraphs={paragraphs}", {"length_bucket": self._length_bucket(len(text)), "paragraph_count": paragraphs})
            if candidate:
                yield candidate

    def _navigation(self, soup: BeautifulSoup, page_kind: PageKind) -> Iterable[Candidate]:
        del page_kind
        patterns = ((FieldKind.PREV_LINK, re.compile(r"^(?:上一[章节页]|前一[章节页]|previous(?:\s+chapter)?|prev)$", re.I)), (FieldKind.NEXT_LINK, re.compile(r"^(?:下一[章节页]|后一[章节页]|next(?:\s+chapter)?)$", re.I)), (FieldKind.INDEX_LINK, re.compile(r"^(?:目录|章节列表|返回书页|table\s+of\s+contents|contents|index)$", re.I)))
        for link in soup.find_all("a", href=True):
            text = re.sub(r"\s+", " ", link.get_text(" ", strip=True))
            rel_tokens = {str(item).lower() for item in link.get("rel", [])}
            field = FieldKind.PREV_LINK if "prev" in rel_tokens else FieldKind.NEXT_LINK if "next" in rel_tokens else None
            if field is None:
                field = next((kind for kind, pattern in patterns if pattern.fullmatch(text)), None)
            if field is None:
                continue
            candidate = self._candidate(soup, field, link, self._summary(text), 0.9 if field.value.removesuffix("_link") in rel_tokens else 0.7, "navigation.semantic", f"rel_count={len(rel_tokens)};length_bucket={self._length_bucket(len(text))}")
            if candidate:
                yield candidate

    def _noise(self, soup: BeautifulSoup, page_kind: PageKind) -> Iterable[Candidate]:
        del page_kind
        for node in soup.find_all(True):
            if not self._is_noise(node):
                continue
            candidate = self._candidate(soup, FieldKind.CLEAN_SELECTOR, node, "noise_region=1", 0.85, "noise.marker", f"tag={node.name};length_bucket={self._length_bucket(len(self._marker(node)))}")
            if candidate:
                yield candidate

    def _candidate(self, soup: BeautifulSoup, field: FieldKind, node: Tag, preview: str, score: float, rule_id: str, detail: str, metadata: dict[str, MetadataValue] | None = None) -> Candidate | None:
        selector = self._unique_css(soup, node)
        return self._make(field, selector, preview, score, rule_id, detail, metadata) if selector else None

    @staticmethod
    def _make(field: FieldKind, selector: str, preview: str, score: float, rule_id: str, detail: str, metadata: dict[str, MetadataValue] | None = None) -> Candidate:
        confidence = max(0.0, min(1.0, score)) if math.isfinite(score) else score
        return Candidate(field, selector, preview, score, confidence, (Evidence(rule_id, score, detail),), metadata or {})

    @staticmethod
    def _summary(text: str) -> str:
        normalized = re.sub(r"\s+", " ", text).strip()
        return f"length_bucket={CandidateExtractor._length_bucket(len(normalized))}"

    @staticmethod
    def _length_bucket(length: int) -> str:
        if length <= 16:
            return "1-16"
        if length <= 64:
            return "17-64"
        return "65+"

    def _marker(self, node: Tag) -> str:
        if node.attrs is None:
            return node.name
        return " ".join([node.name, str(node.get("id", "")), *node.get("class", [])])

    def _is_noise(self, node: Tag) -> bool:
        return bool(self._noise_pattern.search(self._marker(node)))

    def _in_noise(self, node: Tag) -> bool:
        return self._is_noise(node) or any(self._is_noise(parent) for parent in node.parents if isinstance(parent, Tag))

    @staticmethod
    def _unique_css(soup: BeautifulSoup, node: Tag) -> str | None:
        node_id = node.get("id")
        if isinstance(node_id, str) and node_id and CandidateExtractor._semantic_attribute(node_id, node):
            selector = f"#{soupsieve.escape(node_id)}"
            if soup.select(selector) == [node]:
                return selector
        classes = [item for item in node.get("class", []) if isinstance(item, str) and item and CandidateExtractor._semantic_attribute(item, node)]
        if classes:
            selector = "." + ".".join(soupsieve.escape(item) for item in classes)
            if soup.select(selector) == [node]:
                return selector
        parts: list[str] = []
        current: Tag | None = node
        while current is not None and current.name != "[document]":
            if not isinstance(current.parent, Tag):
                break
            siblings = current.parent.find_all(current.name, recursive=False)
            segment = soupsieve.escape(current.name)
            if len(siblings) > 1:
                segment += f":nth-of-type({siblings.index(current) + 1})"
            parts.append(segment)
            selector = " > ".join(reversed(parts))
            if soup.select(selector) == [node]:
                return selector
            current = current.parent
        return None

    @staticmethod
    def _semantic_attribute(value: str, node: Tag) -> bool:
        if re.search(r"(?:token|secret|session|password)", value, re.I):
            return False
        if "@" in value or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value, re.I):
            return False
        if len(value) > 48 or sum(character.isdigit() for character in value) > 4:
            return False
        compact_value = re.sub(r"\W+", "", value).casefold()
        compact_text = re.sub(r"\W+", "", node.get_text(" ", strip=True)).casefold()
        if len(compact_value) >= 4 and compact_value == compact_text:
            return False
        words = set(re.split(r"[-_]", value.casefold()))
        vocabulary = {"content", "chapter", "catalog", "book", "list", "reader", "article", "author", "title", "nav", "pagination", "main", "body", "text", "novel", "entry", "item", "link", "comment", "recommend", "related", "advert", "ad", "banner", "footer", "share"}
        return any(word in vocabulary or word.removesuffix("s") in vocabulary for word in words)

    def _chapter_anchor_selector(self, soup: BeautifulSoup, container: str, links: list[Tag]) -> tuple[str, str] | None:
        common_classes = set(links[0].get("class", []))
        for link in links[1:]:
            common_classes &= set(link.get("class", []))
        for class_name in sorted(common_classes):
            if not isinstance(class_name, str) or not self._semantic_attribute(class_name, links[0]):
                continue
            selector = f"{container} a.{soupsieve.escape(class_name)}"
            if soup.select(selector) == links:
                return selector, "semantic_class"
        for selector in (f"{container} li > a", f"{container} dd > a", f"{container} tr a"):
            if soup.select(selector) == links:
                return selector, "structural_group"
        hrefs = [str(link.get("href", "")) for link in links]
        if all(re.fullmatch(r"/[A-Za-z][A-Za-z0-9_-]*/[A-Za-z0-9._-]+", href) for href in hrefs):
            prefixes = {href.rsplit("/", 1)[0] + "/" for href in hrefs}
            if len(prefixes) == 1:
                prefix = prefixes.pop()
                selector = f'{container} a[href^="{prefix}"]'
                if soup.select(selector) == links:
                    return selector, "safe_href"
        if len(links) <= 12:
            selectors = [self._unique_css(soup, link) for link in links]
            if all(selectors):
                selector = ", ".join(cast(list[str], selectors))
                if soup.select(selector) == links:
                    return selector, "exact_group"
        return None
