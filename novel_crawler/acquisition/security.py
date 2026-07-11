"""URL validation and DNS rebinding defenses for page acquisition."""

from __future__ import annotations

import inspect
import ipaddress
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import dns.exception
import dns.resolver


class Resolver(Protocol):
    def __call__(self, host: str, port: int, timeout: float | None = None) -> Iterable[str]: ...


class UrlSafetyError(ValueError):
    """A URL was rejected without retaining its sensitive components."""

    def __init__(self, code: str, safe_url: str, recoverable: bool = False) -> None:
        self.code = code
        self.safe_url = safe_url
        self.recoverable = recoverable
        super().__init__(f"{code}: {safe_url}")


@dataclass(frozen=True)
class ResolvedTarget:
    """A validated endpoint and the exact addresses approved for connection.

    Connectors MUST pin the connection to one of ``addresses``. ``host`` is
    only for the HTTP Host header and TLS SNI; resolving it again is unsafe
    because DNS may change after validation. This policy validates resolution
    results but does not itself close that DNS TOCTOU window.
    """

    host: str
    port: int
    addresses: tuple[str, ...]


def redact_url(url: str) -> str:
    """Reduce a URL to its normalized origin, excluding every navigation component."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if host is None:
            return urlunsplit((parts.scheme.lower(), "", "/", "", ""))
        host = host.lower()
        if ":" not in host:
            host = host.encode("idna").decode("ascii").rstrip(".")
        display_host = f"[{host}]" if ":" in host else host
        try:
            port = parts.port
        except ValueError:
            port = None
        scheme = parts.scheme.lower()
        default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
        netloc = display_host if port is None or port == default_port else f"{display_host}:{port}"
        return urlunsplit((scheme, netloc, "/", "", ""))
    except (TypeError, ValueError):
        return "<invalid-url>"


def _system_resolver(host: str, port: int, timeout: float | None = None) -> Iterable[str]:
    """Resolve A and AAAA records with one dnspython lifetime budget."""
    resolver = dns.resolver.Resolver()
    lifetime = timeout
    answers: list[str] = []
    started = time.monotonic()
    for record_type in ("A", "AAAA"):
        remaining = None if lifetime is None else lifetime - (time.monotonic() - started)
        if remaining is not None and remaining <= 0:
            raise TimeoutError("DNS resolution deadline exhausted")
        try:
            response = resolver.resolve(host, record_type, lifetime=remaining)
        except dns.resolver.NoAnswer:
            continue
        except dns.exception.Timeout as exc:
            raise TimeoutError("DNS resolution deadline exhausted") from exc
        except dns.exception.DNSException:
            continue
        answers.extend(str(answer) for answer in response)
    return answers


def _is_unsafe(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not address.is_global


class UrlSafetyPolicy:
    """Validate URL syntax and all resolver answers before network access.

    The resolver contract is ``resolver(idna_ascii_host, port) -> addresses``.
    Every returned address is checked, including answers for noncanonical
    integer/octal/hex-looking host names. Consumers must obey the pinning
    contract documented by :class:`ResolvedTarget`.
    """

    def __init__(
        self,
        allowed_schemes: tuple[str, ...] = ("http", "https"),
        resolver: Resolver = _system_resolver,
    ) -> None:
        self.allowed_schemes = tuple(scheme.lower() for scheme in allowed_schemes)
        self.resolver = resolver

    def validate(self, url: str, *, timeout: float | None = None) -> ResolvedTarget:
        safe_url = redact_url(url)
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise UrlSafetyError("malformed_host", safe_url) from exc
        scheme = parts.scheme.lower()
        if scheme not in self.allowed_schemes:
            raise UrlSafetyError("scheme_not_allowed", safe_url)
        if parts.username is not None or parts.password is not None:
            raise UrlSafetyError("credentials_not_allowed", safe_url)
        raw_host = parts.hostname
        if raw_host is None or not raw_host:
            raise UrlSafetyError("malformed_host", safe_url)
        if "%" in raw_host:
            raise UrlSafetyError("zone_identifier_not_allowed", safe_url)
        authority = parts.netloc.rsplit("@", 1)[-1]
        if authority.endswith(":"):
            raise UrlSafetyError("invalid_port", safe_url)
        try:
            port = parts.port
        except ValueError as exc:
            raise UrlSafetyError("invalid_port", safe_url) from exc
        port = port if port is not None else (443 if scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise UrlSafetyError("invalid_port", safe_url)

        host = self._normalize_host(raw_host, safe_url)
        if host == "localhost" or host.endswith(".localhost"):
            raise UrlSafetyError("localhost", safe_url)

        literal = self._parse_ip(host)
        if literal is not None:
            self._require_public(literal, safe_url)
            return ResolvedTarget(host, port, (literal.compressed,))

        try:
            answers = tuple(dict.fromkeys(self._resolve(host, port, timeout)))
        except TimeoutError as exc:
            raise UrlSafetyError("dns_timeout", safe_url, True) from exc
        except (OSError, KeyError, UnicodeError, ValueError) as exc:
            raise UrlSafetyError("dns_resolution_failed", safe_url) from exc
        if not answers:
            raise UrlSafetyError("dns_resolution_failed", safe_url)
        normalized: list[str] = []
        for answer in answers:
            try:
                address = ipaddress.ip_address(answer)
            except ValueError as exc:
                raise UrlSafetyError("dns_resolution_failed", safe_url) from exc
            self._require_public(address, safe_url)
            normalized.append(address.compressed)
        return ResolvedTarget(host, port, tuple(normalized))

    def validate_redirect(
        self, source_url: str, target_url: str, *, timeout: float | None = None
    ) -> ResolvedTarget:
        """Resolve a possibly relative redirect and fully validate its target."""
        return self.validate(urljoin(source_url, target_url), timeout=timeout)

    def _resolve(self, host: str, port: int, timeout: float | None) -> Iterable[str]:
        """Call timeout-aware resolvers while retaining legacy two-argument injection."""
        try:
            signature = inspect.signature(self.resolver)
            signature.bind(host, port, timeout)
        except (TypeError, ValueError):
            return self.resolver(host, port)
        return self.resolver(host, port, timeout)

    @staticmethod
    def _normalize_host(host: str, safe_url: str) -> str:
        host = host.lower()
        if not host or any(character.isspace() for character in host):
            raise UrlSafetyError("malformed_host", safe_url)
        try:
            normalized = host.encode("idna").decode("ascii").rstrip(".")
        except UnicodeError as exc:
            raise UrlSafetyError("malformed_host", safe_url) from exc
        invalid_dns_label = ":" not in normalized and any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in normalized.split(".")
        )
        if len(normalized) > 253 or invalid_dns_label:
            raise UrlSafetyError("malformed_host", safe_url)
        return normalized

    @staticmethod
    def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return ipaddress.ip_address(host)
        except ValueError:
            return None

    @staticmethod
    def _require_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address, safe_url: str) -> None:
        if _is_unsafe(address):
            raise UrlSafetyError("unsafe_address", safe_url)
