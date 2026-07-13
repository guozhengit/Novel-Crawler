from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from novel_crawler.core.models import Book, Chapter, ChapterStatus


class _Storage(Protocol):
    def get_book(self, book_id: int) -> Book: ...
    def all_chapters(self, book_id: int) -> list[Chapter]: ...


@dataclass(frozen=True)
class EasyVoiceExportResult:
    book_id: int
    export_path: Path
    chapter_count: int


@dataclass(frozen=True)
class EasyVoiceConversionResult:
    book_id: int
    export_path: Path
    output_dir: Path
    manifest_path: Path
    returncode: int
    stdout: str
    stderr: str

    @property
    def completed(self) -> bool:
        return self.returncode == 0

    @property
    def incomplete(self) -> bool:
        return self.returncode == 2


@dataclass(frozen=True)
class EasyVoiceOptions:
    base_url: str = "http://localhost:9549"
    voice: str = "zh-CN-YunxiNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"
    use_llm: bool = False
    poll_interval: float = 3
    task_timeout: float = 3600
    retries: int = 3
    assemble: bool = False
    media_container: str | None = None
    media_host_root: Path | None = None
    media_container_root: Path | None = None
    pipeline: Path | None = None


def export_book_for_easyvoice(storage: _Storage, book_id: int, destination: Path) -> EasyVoiceExportResult:
    book = storage.get_book(book_id)
    chapters = [
        chapter
        for chapter in storage.all_chapters(book_id)
        if chapter.status == ChapterStatus.DONE and chapter.content_path and chapter.content_path.is_file()
    ]
    if not chapters:
        raise ValueError("easyvoice_no_completed_chapters")
    document = {
        "book": {
            "id": f"book-{book_id}",
            "title": book.title,
            "author": book.author or "",
        },
        "chapters": [
            {
                "id": f"book-{book_id}-chapter-{chapter.index}",
                "number": chapter.index,
                "title": chapter.title,
                "content": _chapter_content(chapter),
            }
            for chapter in sorted(chapters, key=lambda item: item.index)
        ],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return EasyVoiceExportResult(book_id=book_id, export_path=destination, chapter_count=len(chapters))


def run_easyvoice_pipeline(
    *,
    input_path: Path,
    output_dir: Path,
    project_dir: Path,
    options: EasyVoiceOptions,
) -> EasyVoiceConversionResult:
    pipeline = options.pipeline or project_dir / "integrations" / "easyvoice" / "novel_tts_pipeline.py"
    if not pipeline.is_file():
        raise FileNotFoundError(f"easyvoice pipeline not found: {pipeline}")
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(pipeline),
        "--input",
        str(input_path),
        "--output",
        str(output_dir),
        "--base-url",
        normalize_easyvoice_base_url(options.base_url),
        "--voice",
        options.voice,
        "--rate",
        options.rate,
        "--pitch",
        options.pitch,
        "--volume",
        options.volume,
        "--poll-interval",
        str(options.poll_interval),
        "--task-timeout",
        str(options.task_timeout),
        "--retries",
        str(options.retries),
    ]
    if options.use_llm:
        command.append("--use-llm")
    if options.assemble:
        command.append("--assemble")
    if options.media_container:
        command.extend(["--media-container", options.media_container])
    if options.media_host_root:
        command.extend(["--media-host-root", str(options.media_host_root)])
    if options.media_container_root:
        command.extend(["--media-container-root", str(options.media_container_root)])
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    manifest_path = output_dir / _book_id_from_export(input_path) / "manifest.json"
    return EasyVoiceConversionResult(
        book_id=_numeric_book_id_from_export(input_path),
        export_path=input_path,
        output_dir=output_dir,
        manifest_path=manifest_path,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _chapter_content(chapter: Chapter) -> str:
    assert chapter.content_path is not None
    content = chapter.content_path.read_text(encoding="utf-8", errors="replace").strip()
    lines = content.splitlines()
    if lines and lines[0].strip() == chapter.title:
        return "\n".join(lines[1:]).strip()
    return content


def normalize_easyvoice_base_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.path.rstrip("/") == "/generate":
        parts = parts._replace(path="", query="", fragment="")
    return urlunsplit(parts).rstrip("/")


def _book_id_from_export(input_path: Path) -> str:
    try:
        document = json.loads(input_path.read_text(encoding="utf-8"))
        return str(document["book"]["id"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return input_path.stem


def _numeric_book_id_from_export(input_path: Path) -> int:
    book_id = _book_id_from_export(input_path)
    if book_id.startswith("book-") and book_id[5:].isdigit():
        return int(book_id[5:])
    if book_id.isdigit():
        return int(book_id)
    return 0
