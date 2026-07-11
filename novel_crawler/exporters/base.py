from pathlib import Path
from typing import Protocol

from novel_crawler.core.storage import Storage


class Exporter(Protocol):
    def export(self, storage: Storage, book_id: int, output: Path | None = None) -> Path:
        ...


_REGISTRY: dict[str, type] = {}


def register_exporter(fmt: str):
    def decorator(cls):
        _REGISTRY[fmt] = cls
        return cls
    return decorator


def get_exporter(fmt: str, output_dir: Path) -> Exporter:
    cls = _REGISTRY.get(fmt, _REGISTRY.get("txt"))
    return cls(output_dir)


def list_formats() -> list[str]:
    return list(_REGISTRY.keys())
