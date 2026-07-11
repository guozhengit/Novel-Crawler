# Phase 1 Task 3 Report: unified page snapshot and safe HTTP

## Outcome

- Added immutable `RedirectHop` and `PageSnapshot` models; snapshot headers are copied into an immutable mapping.
- Added `HttpPageAcquirer`, `AcquisitionError`, a testable transport protocol, and a default urllib3 transport.
- The default transport opens the pool against a policy-approved IP. For HTTPS it separately supplies the original normalized hostname as TLS SNI and certificate hostname; HTTP `Host` retains the original hostname and any non-default port.
- Redirects are manual, relative-aware, loop/limit checked, and validated independently before the next transport call.
- Added allowlisted response headers, charset decoding, redacted failures, and recoverable/terminal status semantics.
- Added opt-in `Fetcher(acquirer=...)` delegation for both existing `fetch_text` and `fetch_bytes` APIs. Default legacy behavior is unchanged.

## TDD evidence

1. Initial RED: `python -m pytest tests/acquisition/test_http.py -q` failed collection with `ModuleNotFoundError: novel_crawler.acquisition.http`.
2. First GREEN cycle: 17 tests passed and one loop fixture exposed that `/same` was not a loop from `/same?token=...`; correcting the fixture to redirect to the identical URL produced 18 passing tests.
3. Legacy delegation RED: constructing `Fetcher(..., acquirer=...)` failed with an unexpected keyword argument; implementation made the focused test pass.
4. `fetch_bytes` delegation RED: the method entered the legacy requests path and failed. The opt-in delegation was extended consistently, after which all 19 focused tests passed.

## Address-pinning acceptance evidence

- The real `UrlSafetyPolicy` is exercised with an injected counting resolver, not replaced by a permissive fake.
- Tests prove one resolver call for the initial request and exactly one new resolver call per redirect hop.
- The fake transport receives only `approved_ip` for connection, plus separate `original_host`, scheme, port, path/query, and Host header inputs; it has no resolver contract.
- HTTPS pool inspection confirms urllib3's created connection uses the approved IP as `host` while `server_hostname` and `assert_hostname` remain the original hostname.

## Verification

- Focused: `python -m pytest tests/acquisition/test_http.py -q` — 24 passed.
- Full: `python -m pytest -q` — 159 passed.
- Ruff: `python -m ruff check novel_crawler tests` — passed.
- Mypy: `python -m mypy novel_crawler` — passed, 35 source files checked.
- Build: `python -m build --no-isolation` — sdist and wheel built successfully.
- Whitespace: `git diff --check` — passed (only Git's Windows line-ending notices).

## Review follow-up

- Added ordered fallback across every policy-approved address while sharing a monotonic total timeout budget. A regression proves an IPv6 connection failure falls through to the approved IPv4 address without another resolver call.
- Charset-normalizer's best result is now honored and its normalized codec label recorded. Header charset remains first priority; real Big5 and GB18030 samples verify detection and decoding.
- `PageSnapshot` now retains immutable original `body` bytes. Legacy `fetch_bytes` returns those exact bytes (including non-roundtrippable/BOM data), while `fetch_text` returns decoded HTML.
- Acquisition errors suppress raw transport and safety exception chains, preventing query secrets in exception causes/messages.
- Declared urllib3 as a direct dependency. Constructor-level tests prove HTTP/HTTPS pools receive only the approved IP as the connection host, while HTTPS receives the original hostname for SNI and certificate checks; IPv6 Host formatting is also covered.
