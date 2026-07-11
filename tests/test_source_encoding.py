from pathlib import Path

TEXT_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json"}
SKIP_PARTS = {".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "data"}


def test_project_text_files_are_utf8_without_bom():
    root = Path(__file__).resolve().parents[1]
    failures = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        raw = path.read_bytes()
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"{path.relative_to(root)}: {exc}")
        if raw.startswith(b"\xef\xbb\xbf"):
            failures.append(f"{path.relative_to(root)}: UTF-8 BOM")
    assert failures == []
