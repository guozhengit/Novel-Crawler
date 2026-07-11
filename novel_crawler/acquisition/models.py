"""Immutable models shared by acquisition and classification."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from .security import redact_url

_V = TypeVar("_V")


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_url", redact_url(self.requested_url))
        object.__setattr__(self, "final_url", redact_url(self.final_url))
        object.__setattr__(self, "headers", _FrozenMapping(self.headers))
