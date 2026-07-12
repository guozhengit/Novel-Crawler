# Privacy and Local Data

Novel Crawler is a local application. It does not include analytics, cloud synchronization or an application-operated telemetry service.

## Stored data

The selected data directory may contain:

- `tasks.db`: private source URLs, task state, events and checkpoints
- `crawler.db`: book/chapter metadata, status, hashes and deletion jobs
- `contents/`: downloaded chapter text
- `output/`: exported books
- `cache/`: temporary response data used by compatible legacy tools
- `config-registry/`: immutable site configs, salts, fingerprints and quarantine events
- `browser-sessions/`: Chromium profile data and Cookies needed for a target origin

The default location is described in [README.md](README.md). `--data-dir` selects a different private root.

## What is not persisted

- browser verification and confirmation tokens
- raw exception tracebacks in task events
- full URL paths/queries in safe DTOs and public snapshots
- response HTML or chapter text in configuration validation samples

Some target-site Cookies are necessarily stored inside the local browser profile. They are never intended for logs, CLI JSON, Web JSON or commits.

## Logs and diagnostics

Presentation-facing errors use stable codes. URL paths, query strings, credentials, Windows/Unix paths and token-like text are redacted. Avoid enabling third-party shell tracing around commands that contain a private source URL.

When reporting a bug, use a local synthetic server and remove databases, content, profile files and target identifiers.

## Deletion and retention

- `delete BOOK_ID` removes the book through a durable deletion job; incomplete filesystem cleanup is reported and can be retried.
- Deleting a book is distinct from deleting a browser session or the whole data directory.
- Exported files may outlive the book record and should be removed separately when no longer needed.
- Browser cleanup failures create a task gate so the profile is not silently abandoned as reusable state.
- To remove all local state, stop the application, verify no cleanup job is running, and delete the explicitly selected data directory using normal OS tools.

Backups, filesystem snapshots and container volumes are controlled by the user and are not removed by the application.
