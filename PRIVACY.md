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

The default location is described in [README.md](README.md). `--data-dir` selects a different private root.

## What is not persisted

- configuration confirmation metadata
- raw exception tracebacks in task events
- full URL paths/queries in safe DTOs and public snapshots
- response HTML or chapter text in configuration validation samples

Production acquisition does not import or persist browser profiles or target-site Cookies. Do not add credentials, Cookies or tokens to selector overrides, configuration revisions, logs or issue reports.

## Logs and diagnostics

Presentation-facing errors use stable codes. URL paths, query strings, credentials, Windows/Unix paths and token-like text are redacted. Avoid enabling third-party shell tracing around commands that contain a private source URL.

When reporting a bug, use a local synthetic server and remove databases, content and target identifiers.

## Deletion and retention

- `delete BOOK_ID` removes the book through a durable deletion job; incomplete filesystem cleanup is reported and can be retried.
- Deleting a book is distinct from deleting configuration revisions or the whole data directory.
- Exported files may outlive the book record and should be removed separately when no longer needed.
- Cleanup gates may remain on tasks created by older releases; new static HTTP tasks do not create browser state.
- To remove all local state, stop the application, verify no cleanup job is running, and delete the explicitly selected data directory using normal OS tools.

Backups, filesystem snapshots and container volumes are controlled by the user and are not removed by the application.
