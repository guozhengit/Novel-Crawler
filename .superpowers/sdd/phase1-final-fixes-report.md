# Phase 1 Final Fixes Report

Date: 2026-07-11

## Scope completed

- `Urllib3PinnedTransport` now opens responses with `preload_content=False`, skips redirect bodies, and always releases/closes the response and pool.
- `HttpPageAcquirer(max_body_bytes=10 * 1024 * 1024)` forwards a strict response limit. Valid oversized `Content-Length` values fail before streaming; decoded streaming chunks (`decode_content=True`) are counted and fail closed with `AcquisitionError(code="response_too_large", recoverable=False)`.
- One monotonic deadline is created before the fetch loop and shared by every redirect hop and approved-IP fallback. Deadline exhaustion is reported as a recoverable timeout even if an earlier IP failed for another transport reason.
- `PageSnapshot.requested_url`, `PageSnapshot.final_url`, and `RedirectHop.url` enforce `redact_url()` at the immutable model boundary. Full URLs remain local to `fetch` for request-target construction and redirect `urljoin` only. `repr()` and `dataclasses.asdict()` are covered for secret absence.
- `docs/ARCHITECTURE.md` documents the acquisition API, mandatory address pinning, timeout/redirect/body limits, snapshot privacy, and the rule that callers consume normalized snapshot fields rather than re-parsing. The local fixture transport is explicitly not presented as real TLS E2E evidence.

## TDD evidence

New regression tests were run red before implementation for URL privacy, content-length/actual decoded size limits, a shared redirect deadline, redirect body avoidance, streaming/preload behavior, model-level redaction, pre-stream Content-Length rejection, and IP-fallback deadline exhaustion. Each focused test was rerun green after the corresponding minimal implementation.

## Verification

- Focused HTTP tests: `31 passed`.
- Acquisition suite: `83 passed`.
- Full suite: `177 passed`.
- Acquisition branch coverage: `90.87%` (required `>=85%`).
- Ruff: clean.
- mypy: clean for 36 source files.
- `git diff --check`: clean apart from Git's informational LF-to-CRLF worktree warnings.
- `python -m build`: isolated environment creation failed before invoking the backend because Windows Store Python did not create the temporary `Scripts/python.exe`.
- `python -m build --no-isolation`: succeeded and produced both sdist and wheel. This verifies the project build backend while preserving the isolated-build environment limitation above.

## Minor / test-boundary note

No claim of real-network TLS E2E coverage is made. HTTPS host pinning, SNI, hostname configuration, streaming, and cleanup are deterministic unit tests around urllib3 pool construction and response behavior; the integration fixture remains plain HTTP and injected only in tests.
