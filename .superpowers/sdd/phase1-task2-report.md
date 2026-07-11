# Phase 1 Task 2 Report: URL and redirect safety

## Outcome

Implemented the URL safety boundary in `novel_crawler.acquisition.security` without changing `data/` or issuing network requests from tests.

- Added `UrlSafetyError(code, safe_url)`, immutable `ResolvedTarget`, injectable `UrlSafetyPolicy`, and `redact_url`.
- Normalizes internationalized host names with IDNA and handles bracketed IPv6 plus explicit/default ports.
- Rejects non-HTTP(S) schemes, embedded credentials, localhost names, malformed DNS names, invalid ports, and every non-global literal or resolved address.
- Rejects a DNS name when even one answer is private, loopback, link-local, reserved, multicast, or unspecified.
- Revalidates each redirect target independently and permits public cross-domain targets.
- Removes credentials, query strings, and fragments from errors.

## TDD evidence

1. Initial RED: `python -m pytest tests/acquisition/test_security.py -q` failed during collection with `ModuleNotFoundError: novel_crawler.acquisition`.
2. Initial GREEN: 23 focused tests passed after the minimal implementation.
3. Additional RED: malformed `bad_host.example` produced `dns_resolution_failed` instead of `malformed_host`.
4. Final GREEN: DNS label validation was tightened and all 24 focused tests passed.

The injected stub resolver records `(host, port)` calls and returns deterministic addresses; no real DNS or HTTP requests occur in the tests.

## Verification

- Focused: `python -m pytest tests/acquisition/test_security.py -q` — 41 passed.
- Full: `python -m pytest -q` — 135 passed.
- Ruff (maintained package/tests): `python -m ruff check novel_crawler tests` — passed.
- Mypy: `python -m mypy novel_crawler` — passed, 33 files checked.
- Build: `python -m build --no-isolation` — sdist and wheel built successfully.

`python -m build` could not create an isolated virtual environment under the Microsoft Store Python installation, so the equivalent non-isolated build was used after installing the declared build requirements. A repository-wide `ruff check .` also reports pre-existing violations in root utility scripts (`calibrate.py`, `decode_font.py`, `download_novel.py`, `merge_final.py`, and `verify_parts.py`); the new and maintained package/test paths are clean.

## Review follow-up

Review findings were addressed in a second strict RED/GREEN cycle:

- RED produced six expected failures covering relative redirects, scoped IPv6, Unicode IDNA separators, and empty ports. The only assertion-only correction was the standard library's canonical spelling for public IPv4-mapped IPv6.
- `validate_redirect` now joins relative targets against the source URL before applying the complete validation path. Sequential hop tests prove resolver invocation on every hop.
- IPv6 scope/zone identifiers are rejected, including on otherwise-public literals.
- Tests explicitly cover private and public IPv4-mapped IPv6, deterministic resolver handling of integer/octal/hex IPv4-like hosts, Unicode separator/fullwidth localhost forms, and default/explicit/empty ports.
- API documentation states that a connector MUST connect to a validated `ResolvedTarget.addresses` entry, using `host` only for Host/SNI. Re-resolving the host is unsafe. Task 2 validates resolution but does **not** claim to close DNS TOCTOU; address pinning belongs to Task 3's connector.
