"""HTTP acquisition with DNS-safe address pinning."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import urllib3
from charset_normalizer import from_bytes

from novel_crawler.core.domains import canonical_domain

from .models import AcquiredPage, PageSnapshot, RedirectHop
from .security import ResolvedTarget, UrlSafetyError, UrlSafetyPolicy, redact_url

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
RETAINED_HEADERS = frozenset({"content-type", "content-language", "etag", "last-modified"})


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def request(
        self,
        *,
        approved_ip: str,
        original_host: str,
        port: int,
        scheme: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
        max_body_bytes: int,
    ) -> TransportResponse: ...


class Urllib3PinnedTransport:
    """Connect to an approved IP while retaining the URL host for HTTP/TLS."""

    def request(
        self,
        *,
        approved_ip: str,
        original_host: str,
        port: int,
        scheme: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
        max_body_bytes: int,
    ) -> TransportResponse:
        pool: urllib3.HTTPConnectionPool
        if scheme == "https":
            pool = urllib3.HTTPSConnectionPool(
                approved_ip,
                port,
                assert_hostname=original_host,
                server_hostname=original_host,
            )
        else:
            pool = urllib3.HTTPConnectionPool(approved_ip, port)
        try:
            response = pool.urlopen(
                "GET",
                path,
                headers=dict(headers),
                redirect=False,
                retries=False,
                timeout=urllib3.Timeout(total=timeout),
                preload_content=False,
            )
            try:
                response_headers = dict(response.headers)
                if response.status in REDIRECT_STATUSES:
                    return TransportResponse(response.status, response_headers, b"")
                content_length = next(
                    (value for name, value in response_headers.items() if name.lower() == "content-length"),
                    None,
                )
                try:
                    declared_length = int(content_length) if content_length is not None else None
                except ValueError:
                    declared_length = None
                safe_url = redact_url(f"{scheme}://{original_host}:{port}{path}")
                if declared_length is not None and declared_length > max_body_bytes:
                    raise AcquisitionError("response_too_large", safe_url, False)
                body = bytearray()
                for chunk in response.stream(min(64 * 1024, max_body_bytes + 1), decode_content=True):
                    body.extend(chunk)
                    if len(body) > max_body_bytes:
                        raise AcquisitionError("response_too_large", safe_url, False)
                return TransportResponse(response.status, response_headers, bytes(body))
            finally:
                response.release_conn()
                response.close()
        finally:
            pool.close()


class AcquisitionError(RuntimeError):
    def __init__(self, code: str, safe_url: str, recoverable: bool) -> None:
        self.code = code
        self.safe_url = safe_url
        self.recoverable = recoverable
        super().__init__(f"{code}: {safe_url}")


class HttpPageAcquirer:
    def __init__(
        self,
        transport: HttpTransport | None = None,
        policy: UrlSafetyPolicy | None = None,
        timeout: float = 25,
        max_redirects: int = 10,
        user_agent: str = "novel-crawler/0.1",
        max_body_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self.transport = transport or Urllib3PinnedTransport()
        self.policy = policy or UrlSafetyPolicy()
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        self.max_body_bytes = max_body_bytes

    def fetch(
        self,
        url: str,
        *,
        max_body_bytes: int | None = None,
        locked_origin: str | None = None,
        classifiable_statuses: frozenset[int] = frozenset(),
    ) -> PageSnapshot:
        return self.fetch_page(
            url,
            max_body_bytes=max_body_bytes,
            locked_origin=locked_origin,
            classifiable_statuses=classifiable_statuses,
        ).snapshot

    def fetch_page(
        self,
        url: str,
        *,
        max_body_bytes: int | None = None,
        locked_origin: str | None = None,
        classifiable_statuses: frozenset[int] = frozenset(),
    ) -> AcquiredPage:
        effective_max = self.max_body_bytes if max_body_bytes is None else min(self.max_body_bytes, max_body_bytes)
        if effective_max <= 0:
            raise ValueError("max_body_bytes must be positive")
        origin_lock = self._origin_key(locked_origin) if locked_origin is not None else None
        if origin_lock is not None and self._origin_key(url) != origin_lock:
            raise AcquisitionError("cross_origin", redact_url(url), False)
        requested_url = redact_url(url)
        current_url = url
        seen = {current_url}
        redirects: list[RedirectHop] = []
        target: ResolvedTarget | None = None
        deadline = time.monotonic() + self.timeout

        while True:
            try:
                if target is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AcquisitionError("timeout", redact_url(current_url), True)
                    target = self.policy.validate(current_url, timeout=remaining)
            except UrlSafetyError as exc:
                raise AcquisitionError(exc.code, exc.safe_url, exc.recoverable) from None
            parts = urlsplit(current_url)
            scheme = parts.scheme.lower()
            headers = {
                "Host": self._host_header(target.host, target.port, scheme),
                "User-Agent": self.user_agent,
            }
            response: TransportResponse | None = None
            failures: list[Exception] = []
            deadline_exhausted = False
            for approved_ip in target.addresses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failures.append(TimeoutError())
                    deadline_exhausted = True
                    break
                try:
                    response = self.transport.request(
                        approved_ip=approved_ip,
                        original_host=target.host,
                        port=target.port,
                        scheme=scheme,
                        path=self._request_target(parts.path, parts.query),
                        headers=headers,
                        timeout=remaining,
                        max_body_bytes=effective_max,
                    )
                    break
                except AcquisitionError:
                    raise
                except Exception as exc:
                    failures.append(exc)
            if response is None:
                timed_out = bool(failures) and all(
                    isinstance(exc, (TimeoutError, urllib3.exceptions.TimeoutError)) for exc in failures
                )
                code = "timeout" if timed_out or deadline_exhausted else "transport_error"
                raise AcquisitionError(code, redact_url(current_url), True) from None

            if response.status_code in REDIRECT_STATUSES:
                location = self._header(response.headers, "location")
                if not location:
                    raise AcquisitionError("redirect_missing_location", redact_url(current_url), False)
                if len(redirects) >= self.max_redirects:
                    raise AcquisitionError("too_many_redirects", redact_url(current_url), False)
                next_url = urljoin(current_url, location)
                if origin_lock is not None and self._origin_key(next_url) != origin_lock:
                    raise AcquisitionError("cross_origin", redact_url(next_url), False)
                if next_url in seen:
                    raise AcquisitionError("redirect_loop", redact_url(next_url), False)
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AcquisitionError("timeout", redact_url(current_url), True)
                    next_target = self.policy.validate_redirect(current_url, location, timeout=remaining)
                except UrlSafetyError as exc:
                    raise AcquisitionError(exc.code, exc.safe_url, exc.recoverable) from None
                redirects.append(RedirectHop(redact_url(current_url), response.status_code))
                seen.add(next_url)
                current_url = next_url
                target = next_target
                continue

            if response.status_code >= 400 and response.status_code not in classifiable_statuses:
                recoverable = response.status_code in {408, 429} or response.status_code >= 500
                raise AcquisitionError(f"http_{response.status_code}", redact_url(current_url), recoverable)

            content_length = self._header(response.headers, "content-length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    declared_length = None
                if declared_length is not None and declared_length > effective_max:
                    raise AcquisitionError("response_too_large", redact_url(current_url), False)
            if len(response.body) > effective_max:
                raise AcquisitionError("response_too_large", redact_url(current_url), False)

            filtered = {
                name.lower(): value for name, value in response.headers.items() if name.lower() in RETAINED_HEADERS
            }
            encoding, html = self._decode(response.body, filtered.get("content-type"))
            snapshot = PageSnapshot(
                requested_url=requested_url,
                final_url=redact_url(current_url),
                status_code=response.status_code,
                headers=filtered,
                encoding=encoding,
                html=html,
                body=response.body,
                method="GET",
                redirects=tuple(redirects),
                retrieved_at=datetime.now(UTC),
            )
            return AcquiredPage(snapshot, current_url)

    @staticmethod
    def _request_target(path: str, query: str) -> str:
        target = path or "/"
        return f"{target}?{query}" if query else target

    @staticmethod
    def _origin_key(url: str) -> tuple[str, str, int]:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        raw_host = (parts.hostname or "").lower()
        host = raw_host if ":" in raw_host else canonical_domain(raw_host)
        return scheme, host, parts.port or (443 if scheme == "https" else 80)

    @staticmethod
    def _host_header(host: str, port: int, scheme: str) -> str:
        display_host = f"[{host}]" if ":" in host else host
        default_port = 443 if scheme == "https" else 80
        return display_host if port == default_port else f"{display_host}:{port}"

    @staticmethod
    def _header(headers: Mapping[str, str], wanted: str) -> str | None:
        return next((value for name, value in headers.items() if name.lower() == wanted), None)

    @staticmethod
    def _decode(body: bytes, content_type: str | None) -> tuple[str, str]:
        charset: str | None = None
        if content_type:
            message = Message()
            message["content-type"] = content_type
            charset = message.get_content_charset()
        if charset:
            try:
                return charset, body.decode(charset)
            except (LookupError, UnicodeDecodeError):
                pass
        result = from_bytes(body).best()
        if result is not None and result.encoding:
            detected = result.encoding.lower()
            normalized = "utf-8" if detected in {"utf_8", "utf-8", "utf8"} else detected.replace("_", "-")
            return normalized, str(result)
        for encoding in ("utf-8", "gb18030"):
            try:
                return encoding, body.decode(encoding)
            except UnicodeDecodeError:
                continue
        return "utf-8", body.decode("utf-8", errors="replace")
