# Phase 1 Task 1 Report

## Status

Implemented Task 1 within the planned production boundary: `auto.py`, `detector.py`, and `fetcher.py`, plus `tests/test_mojibake.py`.

## TDD evidence

- RED: focused suite initially reported 2 expected behavior failures after test setup was corrected: promotional phrases caused valid story text to be discarded, and the empty-content error remained English.
- RED: a separate fetch-failure message test failed against the former English message before its production change.
- GREEN: focused suite passed with 7 tests.

## Changes

- Kept the existing readable chapter-title regular expression and added acceptance coverage for Chinese numerals, spaced Arabic numerals, and English chapter titles.
- Centralized the four planned promotional phrases and removed those phrases from paragraphs while preserving surrounding story content.
- Replaced the two remaining user-facing English fetch errors with readable Chinese.
- Added a UTF-8 semantic source guard for Python, Markdown, JSON, and YAML. It only checks the explicit known-fragment list and skips deliberately marked fixture lines and Markdown inline-code examples.

## Verification

- `python -m pytest -q tests/test_mojibake.py`: 7 passed.
- `python -m build`: succeeded.
- `python -m pytest -q`: 91 passed.
- `python -m ruff check novel_crawler tests`: passed.
- `git diff --check`: passed.

## Concerns

- Repository-wide `python -m ruff check .` still reports 57 pre-existing style findings in root-level legacy utility scripts. These are outside Task 1 and were not modified. The application package and complete test tree are Ruff-clean.
- The source guard intentionally uses a narrow known-fragment list. This avoids broad heuristics that could flag ordinary Chinese, but newly observed corruption families should be added as explicit regression fixtures.

## Review follow-up

- RED: the strengthened content-quality assertion exposed leftover punctuation, a URL, and the promotional suffix in both sample paragraphs.
- GREEN: promotional cleanup now removes only the four established phrases and their attached punctuation/fixed suffix/URL tail; the exact output is `正文开始。\n正文继续。`.
- The source guard now obtains a deterministic file list from `git ls-files -z`, scans only `README.md`, `novel_crawler/`, and public `docs/` product text, and explicitly excludes `docs/superpowers/` internal plans.
- Replaced single-character blacklist entries with encoding-specific multi-character corruption tokens. Added a regression proving legitimate “璇玑” text is accepted.
- Removed the broad negative-assertion exemption. Only lines explicitly carrying `# mojibake-fixture` and Markdown inline-code examples are exempt.
- Fresh verification after review: focused pytest 9 passed; full pytest 93 passed; build succeeded; `ruff check novel_crawler tests` passed.

## Second review follow-up

- RED: an inline `最新网址：www.example.test。正文二。` regression proved the previous non-whitespace URL match swallowed following story text.
- GREEN: URL cleanup now recognizes a structured domain/URL and stops at whitespace or Chinese/ASCII sentence punctuation, preserving both surrounding story segments.
- Fresh verification is recorded in the task handoff after this report update.
