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
- Added a UTF-8 semantic source guard for Python, Markdown, JSON, and YAML. It only checks the explicit known-fragment list and skips deliberately marked fixture lines, negative assertions, and Markdown inline-code examples.

## Verification

- `python -m pytest -q tests/test_mojibake.py`: 7 passed.
- `python -m build`: succeeded.
- `python -m pytest -q`: 91 passed.
- `python -m ruff check novel_crawler tests`: passed.
- `git diff --check`: passed.

## Concerns

- Repository-wide `python -m ruff check .` still reports 57 pre-existing style findings in root-level legacy utility scripts. These are outside Task 1 and were not modified. The application package and complete test tree are Ruff-clean.
- The source guard intentionally uses a narrow known-fragment list. This avoids broad heuristics that could flag ordinary Chinese, but newly observed corruption families should be added as explicit regression fixtures.
