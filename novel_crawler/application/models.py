from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from novel_crawler.application.errors import ApplicationError

_FORMATS = frozenset({"txt", "epub", "md", "jsonl"})
_OPTION_KEYS = frozenset(
    {"start", "count", "max_chapters", "concurrency", "export", "export_format", "chase", "browser"}
)
_BROWSERS = frozenset({"http", "visible"})


@dataclass(frozen=True)
class CrawlOptions:
    start: int | None = None
    count: int | None = None
    max_chapters: int | None = None
    concurrency: int = 1
    export: bool = True
    export_format: str = "txt"
    chase: bool = False
    browser: str = "http"

    @classmethod
    def parse(cls, value: CrawlOptions | dict[str, Any] | None) -> CrawlOptions:
        if value is None:
            result = cls()
        elif isinstance(value, cls):
            result = value
        elif isinstance(value, dict):
            if set(value) - _OPTION_KEYS:
                raise ApplicationError("options_invalid")
            try:
                result = cls(**value)
            except TypeError as exc:
                raise ApplicationError("options_invalid") from exc
        else:
            raise ApplicationError("options_invalid")
        result._validate()
        return result

    def _validate(self) -> None:
        _bounded_optional(self.start, "start", 1, 10_000_000)
        _bounded_optional(self.count, "count", 1, 1_000_000)
        _bounded_optional(self.max_chapters, "max_chapters", 1, 1_000_000)
        if isinstance(self.concurrency, bool) or not isinstance(self.concurrency, int) or not 1 <= self.concurrency <= 64:
            raise ApplicationError("concurrency_invalid")
        if not isinstance(self.export, bool):
            raise ApplicationError("export_invalid")
        if not isinstance(self.export_format, str) or self.export_format not in _FORMATS:
            raise ApplicationError("export_format_invalid")
        if not isinstance(self.chase, bool):
            raise ApplicationError("chase_invalid")
        if not isinstance(self.browser, str) or self.browser not in _BROWSERS:
            raise ApplicationError("browser_invalid")

    def to_metadata(self) -> dict[str, dict[str, bool | int | str | None]]:
        return {
            "crawl": {
                "chase": self.chase,
                "concurrency": self.concurrency,
                "count": self.count,
                "export": self.export,
                "export_format": self.export_format,
                "max_chapters": self.max_chapters,
                "start": self.start,
                "browser": self.browser,
            }
        }


def _bounded_optional(value: object, name: str, minimum: int, maximum: int) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum
    ):
        raise ApplicationError(f"{name}_invalid")


@dataclass(frozen=True)
class InteractionView:
    kind: str
    attempt: int
    expires_at: str | None
    safe_origin: str | None = field(repr=False)
    verification_required: bool
    confirmation_required: bool
    cleanup_required: bool

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "attempt": self.attempt,
            "expires_at": self.expires_at,
            "safe_origin": self.safe_origin,
            "verification_required": self.verification_required,
            "confirmation_required": self.confirmation_required,
            "cleanup_required": self.cleanup_required,
        }


@dataclass(frozen=True)
class TaskView:
    task_id: str
    status: str
    version: int
    created_at: str
    updated_at: str
    error_code: str | None
    resume_status: str | None
    terminal: bool
    cleanup_required: bool
    checkpoint_count: int = 0
    checkpoint_version_total: int = 0
    interaction: InteractionView | None = None
    progress: MappingProxyType[str, int] = field(default_factory=lambda: MappingProxyType({}))

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error_code": self.error_code,
            "resume_status": self.resume_status,
            "terminal": self.terminal,
            "cleanup_required": self.cleanup_required,
            "checkpoint_count": self.checkpoint_count,
            "checkpoint_version_total": self.checkpoint_version_total,
            "progress": dict(self.progress),
            "interaction": self.interaction.to_safe_dict() if self.interaction is not None else None,
        }


@dataclass(frozen=True)
class TaskEventView:
    event_id: int
    task_id: str
    from_status: str | None
    to_status: str
    task_version: int
    created_at: str
    error_code: str | None

    def to_safe_dict(self) -> dict[str, str | int | None]:
        return dict(vars(self))
