"""Immutable models shared by acquisition and classification."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType


@dataclass(frozen=True)
class RedirectHop:
    url: str
    status_code: int


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
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
