# Contributing

Contributions are welcome when they preserve the security, privacy and recovery guarantees documented in this repository.

## Setup

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
python -m pytest -q
```

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for all quality and release commands.

## Expectations

- Add a failing test before implementing a feature or bug fix.
- Use local synthetic fixtures; do not commit real novels, target Cookies, source URL lists or font maps.
- Route new CLI/Web behavior through `ApplicationService`.
- Reuse `UrlSafetyPolicy` and the pinned static HTTP acquisition path for every network capability.
- Persist only bounded, schema-validated task metadata and checkpoints.
- Use stable error codes; do not expose raw exception text.
- Explicitly close databases, files, transports and servers in tests.
- Do not add automatic Playwright/Chromium fallback. Browser use must remain explicit (`--browser visible`), user-visible, and covered by tests that do not hit real sites.
- Follow [docs/SITE_ADAPTATION.md](docs/SITE_ADAPTATION.md) when choosing a dedicated adapter or generic static exploration.

Before submitting:

```bash
python -m pytest -q
python -m ruff check novel_crawler tests
python -m mypy novel_crawler
python -m pytest --cov=novel_crawler --cov-fail-under=80 -q
python -m coverage report --include="novel_crawler/core/*" --fail-under=85
python -m build
git diff --check
```

By contributing, you agree that your contribution is licensed under the repository's MIT License. Third-party content must have a clear redistribution license and provenance.
