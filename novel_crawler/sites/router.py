from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from novel_crawler.sites.auto import AutoAdapter
from novel_crawler.sites.base import SiteAdapter

if TYPE_CHECKING:
    from novel_crawler.core.fetcher import Fetcher


class AdapterRouter:
    """Route known domains to dedicated adapters and unknown ones to static exploration."""

    def __init__(self, dedicated: Iterable[SiteAdapter], fetcher: Fetcher | None = None) -> None:
        self._dedicated = tuple(dedicated)
        if fetcher is not None:
            for adapter in self._dedicated:
                adapter.set_fetcher(fetcher)

    def dedicated(self, url: str) -> SiteAdapter | None:
        for adapter in self._dedicated:
            if adapter.match(url):
                return adapter
        return None

    def resolve(self, url: str) -> SiteAdapter:
        return self.dedicated(url) or AutoAdapter()


__all__ = ["AdapterRouter"]
