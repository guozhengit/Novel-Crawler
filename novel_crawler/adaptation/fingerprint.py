"""Content-independent DOM structure fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag
from soupsieve.util import SelectorSyntaxError

_KINDS = frozenset({"book", "chapter", "catalog"})
_NOISE_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
_NOISE = re.compile(r"(?:^|[-_])(ad|ads|advert|advertisement|banner|cookie|popup|promo|social|tracking)(?:$|[-_])", re.I)
_SEMANTIC = frozenset({"role", "rel", "itemprop", "itemscope", "type"})
_SAFE_VALUE = re.compile(r"[a-z][a-z0-9_-]{0,39}", re.I)


@dataclass(frozen=True)
class StructureFingerprint:
    version: int
    page_kind: str
    digest: str

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ValueError("unsupported fingerprint version")
        if self.page_kind not in _KINDS:
            raise ValueError("invalid page_kind")
        if not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ValueError("invalid fingerprint digest")

    def to_dict(self) -> dict[str, object]:
        return {"version": self.version, "page_kind": self.page_kind, "digest": self.digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> StructureFingerprint:
        if not isinstance(value, Mapping) or set(value) != {"version", "page_kind", "digest"}:
            raise ValueError("invalid fingerprint fields")
        version, page_kind, digest = value["version"], value["page_kind"], value["digest"]
        if not isinstance(version, int) or isinstance(version, bool) or not isinstance(page_kind, str) or not isinstance(digest, str):
            raise TypeError("invalid fingerprint field types")
        return cls(version, page_kind, digest)


def _is_noise(tag: Tag) -> bool:
    if tag.name in _NOISE_TAGS:
        return True
    tokens = tag.get("class", [])
    if isinstance(tokens, str):
        tokens = tokens.split()
    return any(_NOISE.search(str(token)) for token in tokens)


def _node(tag: Tag) -> object | None:
    if _is_noise(tag):
        return None
    attrs: list[tuple[str, object]] = []
    for key in sorted(_SEMANTIC & set(tag.attrs)):
        raw = tag.attrs[key]
        values = raw if isinstance(raw, list) else [raw]
        safe = sorted(str(value).lower() for value in values if _SAFE_VALUE.fullmatch(str(value)))
        attrs.append((key, safe if safe else True))
    children = [node for child in tag.children if isinstance(child, Tag) and (node := _node(child)) is not None]
    return [tag.name, attrs, children]


def fingerprint_html(html: str | bytes, page_kind: str, candidate_selectors: Mapping[str, str]) -> StructureFingerprint:
    if page_kind not in _KINDS:
        raise ValueError("invalid page_kind")
    if not isinstance(candidate_selectors, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in candidate_selectors.items()):
        raise TypeError("candidate_selectors must map names to selectors")
    soup = BeautifulSoup(html, "html.parser")
    counts: list[tuple[str, int]] = []
    for name, selector in sorted(candidate_selectors.items()):
        try:
            counts.append((name, len(soup.select(selector))))
        except SelectorSyntaxError as exc:
            raise ValueError("candidate selector syntax is invalid") from exc
    roots = [node for child in soup.children if isinstance(child, Tag) and (node := _node(child)) is not None]
    canonical = json.dumps({"structure": roots, "selector_match_counts": counts}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return StructureFingerprint(1, page_kind, hashlib.sha256(canonical.encode("ascii")).hexdigest())
