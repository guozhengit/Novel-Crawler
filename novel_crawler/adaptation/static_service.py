from __future__ import annotations

from urllib.parse import urlsplit

from novel_crawler.adaptation.config_manager import ConfigResolution, ResolutionKind
from novel_crawler.adaptation.config_schema import SiteConfig
from novel_crawler.browser.adaptive import AdaptiveResult
from novel_crawler.core.domains import canonical_domain
from novel_crawler.sites.router import AdapterRouter


class StaticAdaptiveService:
    """Resolve dedicated adapters or bounded static configs without browser escalation."""

    def __init__(self, config_manager: object, router: AdapterRouter) -> None:
        self.config_manager = config_manager
        self._router = router

    def resolve(self, url: str, _task_key: str) -> AdaptiveResult:
        if self._router.dedicated(url) is not None:
            domain = canonical_domain(urlsplit(url).hostname or "")
            placeholder = SiteConfig.new(
                site=domain,
                domain=domain,
                url_patterns=["/**"],
                selectors={"clean": (), "book": {}, "chapter": {}},
            )
            return AdaptiveResult(
                ConfigResolution(
                    ResolutionKind.REUSED,
                    config=placeholder,
                    reason_ids=("dedicated_adapter",),
                )
            )
        try:
            resolution = self.config_manager.resolve(url)  # type: ignore[attr-defined]
        except Exception:
            resolution = ConfigResolution(
                ResolutionKind.TRANSIENT_FAILURE,
                reason_ids=("static_adaptation_failed",),
            )
        return AdaptiveResult(resolution)

    @staticmethod
    def prepare_task_access(_url: str, _task_key: str) -> None:
        return None

    @staticmethod
    def continue_verification(_ticket: object) -> AdaptiveResult:
        return StaticAdaptiveService._unsupported("browser_verification_disabled")

    @staticmethod
    def cancel(_ticket: object) -> AdaptiveResult:
        return StaticAdaptiveService._unsupported("browser_verification_disabled")

    @staticmethod
    def retry_cleanup(_ticket: object) -> AdaptiveResult:
        return StaticAdaptiveService._unsupported("browser_cleanup_unavailable")

    @staticmethod
    def expire_sweep() -> int:
        return 0

    @staticmethod
    def _unsupported(reason: str) -> AdaptiveResult:
        return AdaptiveResult(
            ConfigResolution(ResolutionKind.VERIFICATION_FAILED, reason_ids=(reason,))
        )


__all__ = ["StaticAdaptiveService"]
