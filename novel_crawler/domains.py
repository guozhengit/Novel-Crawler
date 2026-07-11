"""Shared strict canonicalization for DNS domain names."""

from __future__ import annotations

import re

import idna

_ASCII_DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*")


def canonical_domain(domain: str) -> str:
    """Return lowercase UTS #46 ASCII using non-transitional, STD3 rules."""
    if not isinstance(domain, str):
        raise TypeError("domain must be a string")
    value = domain.rstrip(".")
    if not value or any(character in value for character in "/@?#:\\"):
        raise ValueError("domain is invalid")
    try:
        canonical = idna.encode(
            value,
            uts46=True,
            transitional=False,
            std3_rules=True,
        ).decode("ascii").lower()
        # Decode validates supplied A-labels, including fake/invalid xn-- labels.
        idna.decode(canonical.encode("ascii"), uts46=True, std3_rules=True)
    except idna.IDNAError:
        raise ValueError("domain is invalid") from None
    if len(canonical) > 253 or not _ASCII_DOMAIN.fullmatch(canonical):
        raise ValueError("domain is invalid")
    return canonical


__all__ = ["canonical_domain"]
