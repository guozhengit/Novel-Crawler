"""Immutable models shared by acquisition and classification."""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

from .security import redact_url

_V = TypeVar("_V")
_SAFE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,79}")


class _FrozenMapping(Mapping[str, _V]):
    """Small immutable mapping that remains compatible with dataclasses.asdict()."""

    def __init__(self, values: Mapping[str, _V]) -> None:
        self._values = dict(values)

    def __getitem__(self, key: str) -> _V:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return repr(self._values)

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, _V]:
        return dict(self._values)


@dataclass(frozen=True)
class RedirectHop:
    url: str
    status_code: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", redact_url(self.url))


@dataclass(frozen=True)
class PageSnapshot:
    requested_url: str
    final_url: str
    status_code: int
    headers: Mapping[str, str]
    encoding: str
    html: str
    body: bytes
    method: str
    redirects: tuple[RedirectHop, ...]
    retrieved_at: datetime
    sample_id: str = field(default_factory=lambda: f"sample-{secrets.token_hex(16)}", repr=False)

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.sample_id):
            raise ValueError("sample_id must be a safe structural identifier")
        object.__setattr__(self, "requested_url", redact_url(self.requested_url))
        object.__setattr__(self, "final_url", redact_url(self.final_url))
        object.__setattr__(self, "headers", _FrozenMapping(self.headers))


class AcquiredPage:
    """A safe snapshot paired with a process-private navigation URL."""

    __slots__ = ("_navigation_url", "_snapshot")

    def __init__(self, snapshot: PageSnapshot, navigation_url: str) -> None:
        if not isinstance(snapshot, PageSnapshot) or not isinstance(navigation_url, str):
            raise TypeError("invalid acquired page")
        object.__setattr__(self, "_snapshot", snapshot)
        object.__setattr__(self, "_navigation_url", navigation_url)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("AcquiredPage is immutable")

    snapshot = property(lambda self: self._snapshot)
    navigation_url = property(lambda self: self._navigation_url)

    def __repr__(self) -> str:
        return f"AcquiredPage(snapshot={self.snapshot!r}, navigation_url='<redacted>')"
