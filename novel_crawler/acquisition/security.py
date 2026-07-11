"""URL validation and DNS rebinding defenses for page acquisition."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

Resolver = Callable[[str, int], Iterable[str]]


class UrlSafetyError(ValueError):
    """A URL was rejected without retaining its sensitive components."""

    def __init__(self, code: str, safe_url: str) -> None:
        self.code = code
        self.safe_url = safe_url
        super().__init__(f"{code}: {safe_url}")


@dataclass(frozen=True)
class ResolvedTarget:
    host: str
    port: int
    addresses: tuple[str, ...]


def redact_url(url: str) -> str:
    """Remove credentials, query parameters, and fragments from a URL."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if host is None:
            return urlunsplit((parts.scheme, "", parts.path, "", ""))
        display_host = f"[{host}]" if ":" in host else host
        try:
            port = parts.port
        except ValueError:
            port = None
        netloc = display_host if port is None else f"{display_host}:{port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except (TypeError, ValueError):
        return "<invalid-url>"


def _system_resolver(host: str, port: int) -> Iterable[str]:
    return (str(item[4][0]) for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))


def _is_unsafe(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not address.is_global


class UrlSafetyPolicy:
    def __init__(
        self,
        allowed_schemes: tuple[str, ...] = ("http", "https"),
        resolver: Resolver = _system_resolver,
    ) -> None:
        self.allowed_schemes = tuple(scheme.lower() for scheme in allowed_schemes)
        self.resolver = resolver

    def validate(self, url: str) -> ResolvedTarget:
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
            answers = tuple(dict.fromkeys(self.resolver(host, port)))
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

    def validate_redirect(self, source_url: str, target_url: str) -> ResolvedTarget:
        del source_url  # Cross-domain redirects are allowed when the target is public.
        return self.validate(target_url)

    @staticmethod
    def _normalize_host(host: str, safe_url: str) -> str:
        host = host.rstrip(".").lower()
        if not host or any(character.isspace() for character in host):
            raise UrlSafetyError("malformed_host", safe_url)
        try:
            normalized = host.encode("idna").decode("ascii")
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
