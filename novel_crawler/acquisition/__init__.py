"""Safe page acquisition primitives."""

from .classifier import Classification, PageClassifier, PageKind
from .http import AcquisitionError, HttpPageAcquirer
from .models import AcquiredPage, PageSnapshot, RedirectHop
from .security import ResolvedTarget, UrlSafetyError, UrlSafetyPolicy, redact_url

__all__ = [
    "AcquisitionError",
    "AcquiredPage",
    "Classification",
    "HttpPageAcquirer",
    "PageClassifier",
    "PageKind",
    "PageSnapshot",
    "RedirectHop",
    "ResolvedTarget",
    "UrlSafetyError",
    "UrlSafetyPolicy",
    "redact_url",
]
