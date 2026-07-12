# Changelog

All notable changes to this project are documented here.

## [0.2.0] - 2026-07-12

### Added

- Multi-stage automatic site adaptation with candidate scoring, structural fingerprints, revalidation and immutable configuration revisions.
- Safe pinned-IP HTTP acquisition and restricted Chromium fallback with persistent local sessions and manual verification.
- Durable background task engine with CAS transitions, events, checkpoints, restart recovery, bounded execution and chapter claims.
- Unified `ApplicationService`, JSON CLI task controls and a local CSRF-protected Web console.
- Durable book deletion, idempotent content writes and four export formats.

### Changed

- Crawls now run as background tasks and preserve their exact chapter range across restarts.
- Presentation APIs expose allowlisted safe DTOs instead of raw URLs, paths, exception text or browser tokens.
- Runtime data defaults to the operating system application-data directory and can be overridden with `--data-dir`.
- Browser and Web shutdown now drain active work before closing dependent resources.

### Fixed

- Prevented SSRF, unsafe redirects, DNS rebinding, direct browser egress, WebSocket/Service Worker/download leaks and cleanup-handle loss.
- Correctly distinguishes recoverable acquisition failures from terminal HTTP errors.
- Prevented same-book history from expanding a task's `start`, `count` or `max_chapters` range.
- Preserved task identity and partial-success accounting when secondary view construction or queue submission fails.
- Fixed Windows atomic registry publication cleanup and added deterministic resource finalization for SQLite and browser-session handles.

### Removed

- Retired direct network behavior from legacy `inspect`, `wizard` and book-ID `resume` commands.
- Removed one-off target-specific scripts and generated font mapping data from the release repository.
