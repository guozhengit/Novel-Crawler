from __future__ import annotations


class ApplicationError(RuntimeError):
    """Stable, presentation-safe failure exposed by the application boundary."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        task_id: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.task_id = task_id

    def __repr__(self) -> str:
        return (
            f"ApplicationError(code={self.code!r}, retryable={self.retryable!r}, "
            f"task_id={self.task_id!r})"
        )
