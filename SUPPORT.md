# Support and Known Limits

## Supported

- Python 3.11-3.13
- Current Windows, macOS and Linux runners used by CI
- Dedicated adapters for known static sites and bounded static exploration for unknown sites
- Background task creation, pause/resume/cancel, restart recovery and selector confirmation
- TXT, EPUB, Markdown and JSONL export

## Current limits

- The production crawl pipeline is intentionally single-task sequential per crawl (`concurrency=1`).
- Chase mode and command-line proxy files are not available through the unified task pipeline.
- `inspect`, `wizard` and book-ID `resume` are retired compatibility commands.
- Remote Web access has no authentication or TLS; use loopback or a trusted tunnel.
- Automatic selectors can require user confirmation after structural drift.
- Sites using unsupported DRM, CAPTCHAs, authenticated paywalls or native applications may not work and are not bypassed.
- JavaScript-only pages are unsupported by default HTTP crawling. If the user explicitly chooses `--browser visible`, the crawler may use a user-visible Chrome session for public pages the user can already access; CAPTCHAs, authenticated paywalls and DRM are still not bypassed.

## Getting help

Open a GitHub issue with the application version, platform, safe task status/error code and a synthetic reproduction. Do not include real source URLs, Cookies, downloaded text or local databases.

Security reports must follow [SECURITY.md](SECURITY.md).
