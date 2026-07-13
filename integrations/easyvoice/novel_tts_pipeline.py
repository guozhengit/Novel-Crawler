#!/usr/bin/env python3
"""Persistent novel-crawler -> EasyVoice -> audiobook pipeline.

Input is a crawler-neutral JSON document. The pipeline persists every chapter in
SQLite, so it can resume safely after either process restarts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


TERMINAL = {"COMPLETED", "SKIPPED"}
INVALID_FILENAME = re.compile(r'[/\\?%*:|"<>\r\n\s#]+')
MEDIA_CONTAINER: str | None = None
MEDIA_HOST_ROOT: Path | None = None
MEDIA_CONTAINER_ROOT: Path | None = None


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def safe_name(value: str, fallback: str = "untitled", limit: int = 80) -> str:
    cleaned = INVALID_FILENAME.sub("-", value.strip()).strip("-.")
    return (cleaned or fallback)[:limit]


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


@dataclass(frozen=True)
class Settings:
    voice: str = "zh-CN-YunxiNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"
    use_llm: bool = False

    def request_body(self, text: str) -> dict[str, Any]:
        return {
            "text": text,
            "voice": self.voice,
            "rate": self.rate,
            "pitch": self.pitch,
            "volume": self.volume,
            "useLLM": self.use_llm,
        }

    @property
    def digest(self) -> str:
        return stable_hash(self.__dict__)


class JobRepository:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tts_jobs (
              id INTEGER PRIMARY KEY,
              book_id TEXT NOT NULL,
              book_title TEXT NOT NULL,
              book_author TEXT NOT NULL DEFAULT '',
              chapter_id TEXT NOT NULL,
              chapter_number INTEGER NOT NULL,
              chapter_title TEXT NOT NULL,
              content TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              settings_hash TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'QUEUED',
              easyvoice_task_id TEXT,
              remote_audio_file TEXT,
              remote_srt_file TEXT,
              local_audio_path TEXT,
              local_srt_path TEXT,
              audio_sha256 TEXT,
              duration_seconds REAL,
              retry_count INTEGER NOT NULL DEFAULT 0,
              error_message TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(book_id, chapter_id, content_hash, settings_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_tts_jobs_book_status
              ON tts_jobs(book_id, status, chapter_number);
            """
        )
        self.connection.commit()

    def ingest(self, document: dict[str, Any], settings: Settings) -> str:
        book = document.get("book") or {}
        book_id = str(book.get("id") or stable_hash(book.get("title", "book"))[:16])
        title = str(book.get("title") or book_id)
        author = str(book.get("author") or "")
        chapters = document.get("chapters")
        if not isinstance(chapters, list) or not chapters:
            raise ValueError("input must contain a non-empty chapters array")
        for index, chapter in enumerate(chapters, start=1):
            content = normalize_text(str(chapter.get("content") or chapter.get("text") or ""))
            number = int(chapter.get("number") or index)
            chapter_id = str(chapter.get("id") or number)
            chapter_title = str(chapter.get("title") or f"第{number}章")
            status = "QUEUED" if len(content) >= 5 else "SKIPPED"
            self.connection.execute(
                """
                INSERT OR IGNORE INTO tts_jobs (
                  book_id, book_title, book_author, chapter_id, chapter_number,
                  chapter_title, content, content_hash, settings_hash, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    book_id,
                    title,
                    author,
                    chapter_id,
                    number,
                    chapter_title,
                    content,
                    stable_hash(content),
                    settings.digest,
                    status,
                ),
            )
        self.connection.commit()
        return book_id

    def jobs(self, book_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM tts_jobs WHERE book_id=? ORDER BY chapter_number, id", (book_id,)
            )
        )

    def update(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        sql = ", ".join(f"{key}=?" for key in fields)
        self.connection.execute(
            f"UPDATE tts_jobs SET {sql} WHERE id=?", (*fields.values(), job_id)
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class EasyVoiceClient:
    def __init__(self, base_url: str, timeout: float = 30):
        self.api_url = f"{base_url.rstrip('/')}/api/v1/tts"
        self.timeout = timeout

    def _json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {error.code}: {detail}") from error
        if not body.get("success", True):
            raise RuntimeError(f"EasyVoice business error: {body}")
        return body

    def health(self) -> bool:
        url = self.api_url.rsplit("/api/v1/tts", 1)[0] + "/api/health"
        request = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status == 200 and json.loads(response.read()).get("status") == "ok"
        except (OSError, ValueError):
            return False

    def create(self, text: str, settings: Settings) -> str:
        data = json.dumps(settings.request_body(text), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_url}/create",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return str(self._json(request)["data"]["id"])

    def task(self, task_id: str) -> dict[str, Any] | None:
        request = urllib.request.Request(
            f"{self.api_url}/task/{urllib.parse.quote(task_id, safe='')}"
        )
        try:
            return self._json(request)["data"]
        except RuntimeError as error:
            if str(error).startswith("HTTP 404:"):
                return None
            raise

    def download(self, remote_file: str, destination: Path) -> None:
        safe_remote = Path(remote_file).name
        request = urllib.request.Request(
            f"{self.api_url}/download/{urllib.parse.quote(safe_remote, safe='')}"
        )
        with urllib.request.urlopen(request, timeout=max(self.timeout, 300)) as response:
            data = response.read()
        if not data:
            raise RuntimeError(f"downloaded empty file: {safe_remote}")
        atomic_write(destination, data)


def normalize_text(text: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", "", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def configure_media_tools(
    container: str | None,
    host_root: Path | None,
    container_root: Path | None,
) -> None:
    global MEDIA_CONTAINER, MEDIA_HOST_ROOT, MEDIA_CONTAINER_ROOT
    MEDIA_CONTAINER = container
    MEDIA_HOST_ROOT = host_root.resolve() if host_root else None
    MEDIA_CONTAINER_ROOT = container_root if container_root else None
    if container and (not host_root or not container_root):
        raise ValueError("--media-container requires --media-host-root and --media-container-root")


def media_path(path: Path) -> str:
    resolved = path.resolve()
    if MEDIA_CONTAINER:
        assert MEDIA_HOST_ROOT is not None and MEDIA_CONTAINER_ROOT is not None
        try:
            relative = resolved.relative_to(MEDIA_HOST_ROOT)
        except ValueError as error:
            raise RuntimeError(f"media path is outside mounted host root: {resolved}") from error
        return str(MEDIA_CONTAINER_ROOT / relative)
    return str(resolved)


def run_media(tool: str, arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if shutil.which(tool):
        command = [tool, *arguments]
    elif MEDIA_CONTAINER:
        command = ["docker", "exec", MEDIA_CONTAINER, tool, *arguments]
    else:
        raise RuntimeError(
            f"{tool} is not installed; configure --media-container to use the EasyVoice container"
        )
    return subprocess.run(command, **kwargs)


def probe_duration(path: Path) -> float:
    result = run_media(
        "ffprobe",
        [
            "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", media_path(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise RuntimeError(f"invalid audio duration: {path}")
    return duration


class NovelTTSWorker:
    def __init__(
        self,
        repository: JobRepository,
        client: EasyVoiceClient,
        output_root: Path,
        settings: Settings,
        poll_interval: float = 3,
        task_timeout: float = 3600,
        retries: int = 3,
    ):
        self.repository = repository
        self.client = client
        self.output_root = output_root
        self.settings = settings
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout
        self.retries = retries

    def run_book(self, book_id: str) -> dict[str, Any]:
        if not self.client.health():
            raise RuntimeError("EasyVoice health check failed")
        for job in self.repository.jobs(book_id):
            if job["status"] == "SKIPPED":
                continue
            if job["status"] == "COMPLETED" and self._completed_files_exist(job):
                continue
            self._run_with_retry(job)
        return self.write_manifest(book_id)

    def _completed_files_exist(self, job: sqlite3.Row) -> bool:
        return bool(
            job["local_audio_path"] and Path(job["local_audio_path"]).is_file()
            and job["local_srt_path"] and Path(job["local_srt_path"]).is_file()
        )

    def _run_with_retry(self, original_job: sqlite3.Row) -> None:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            job = next(row for row in self.repository.jobs(original_job["book_id"]) if row["id"] == original_job["id"])
            try:
                self._run_job(job)
                return
            except Exception as error:  # boundary: persist all operational failures
                last_error = error
                self.repository.update(
                    job["id"],
                    status="RETRY_WAIT" if attempt < self.retries else "FAILED",
                    retry_count=attempt + 1,
                    error_message=str(error)[:2000],
                )
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 30))
        raise RuntimeError(f"chapter {original_job['chapter_number']} failed: {last_error}")

    def _run_job(self, job: sqlite3.Row) -> None:
        task_id = job["easyvoice_task_id"]
        task = self.client.task(task_id) if task_id else None
        if task is None or task.get("status") == "failed":
            task_id = self.client.create(job["content"], self.settings)
            self.repository.update(
                job["id"], status="TTS_SUBMITTED", easyvoice_task_id=task_id, error_message=None
            )

        deadline = time.monotonic() + self.task_timeout
        while time.monotonic() < deadline:
            task = self.client.task(task_id)
            if task is None:
                # EasyVoice restarted. Clear the volatile id and let retry create a new task.
                self.repository.update(job["id"], easyvoice_task_id=None, status="QUEUED")
                raise RuntimeError(f"EasyVoice task disappeared: {task_id}")
            if task["status"] == "failed":
                raise RuntimeError(task.get("message") or f"EasyVoice task failed: {task_id}")
            if task["status"] == "completed":
                self._download_result(job, task["result"])
                return
            self.repository.update(job["id"], status="TTS_PROCESSING")
            time.sleep(self.poll_interval)
        raise TimeoutError(f"EasyVoice task timed out: {task_id}")

    def _download_result(self, job: sqlite3.Row, result: dict[str, Any]) -> None:
        book_dir = self.output_root / safe_name(job["book_id"])
        chapter_base = f"{job['chapter_number']:04d}-{safe_name(job['chapter_title'])}"
        audio_path = book_dir / "chapters" / f"{chapter_base}.mp3"
        srt_path = book_dir / "chapters" / f"{chapter_base}.srt"
        self.repository.update(job["id"], status="DOWNLOADING")
        self.client.download(result["file"], audio_path)
        self.client.download(result["srt"], srt_path)
        duration = probe_duration(audio_path)
        self.repository.update(
            job["id"],
            status="COMPLETED",
            remote_audio_file=Path(result["file"]).name,
            remote_srt_file=Path(result["srt"]).name,
            local_audio_path=str(audio_path.resolve()),
            local_srt_path=str(srt_path.resolve()),
            audio_sha256=sha256_file(audio_path),
            duration_seconds=duration,
            error_message=None,
        )

    def write_manifest(self, book_id: str) -> dict[str, Any]:
        jobs = self.repository.jobs(book_id)
        first = jobs[0]
        manifest = {
            "book": {
                "id": book_id,
                "title": first["book_title"],
                "author": first["book_author"],
            },
            "settings": self.settings.__dict__,
            "status": "COMPLETED" if all(job["status"] in TERMINAL for job in jobs) else "INCOMPLETE",
            "chapters": [
                {
                    "id": job["chapter_id"],
                    "number": job["chapter_number"],
                    "title": job["chapter_title"],
                    "status": job["status"],
                    "audio": job["local_audio_path"],
                    "srt": job["local_srt_path"],
                    "durationSeconds": job["duration_seconds"],
                    "sha256": job["audio_sha256"],
                    "error": job["error_message"],
                }
                for job in jobs
            ],
        }
        manifest_path = self.output_root / safe_name(book_id) / "manifest.json"
        atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        return manifest


SRT_BLOCK = re.compile(
    r"(?P<index>\d+)\s*\n(?P<start>\d\d:\d\d:\d\d,\d{3})\s+-->\s+"
    r"(?P<end>\d\d:\d\d:\d\d,\d{3})\s*\n(?P<text>.*?)(?=\n\s*\n|\Z)",
    re.S,
)


def srt_seconds(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def srt_timestamp(value: float) -> str:
    millis = max(0, round(value * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def merge_srts(chapters: Iterable[sqlite3.Row], output: Path) -> None:
    offset = 0.0
    index = 1
    blocks: list[str] = []
    for chapter in chapters:
        content = Path(chapter["local_srt_path"]).read_text(encoding="utf-8-sig")
        for match in SRT_BLOCK.finditer(content.replace("\r\n", "\n")):
            blocks.append(
                f"{index}\n{srt_timestamp(srt_seconds(match['start']) + offset)} --> "
                f"{srt_timestamp(srt_seconds(match['end']) + offset)}\n{match['text'].strip()}"
            )
            index += 1
        offset += float(chapter["duration_seconds"])
    atomic_write(output, ("\n\n".join(blocks) + "\n").encode("utf-8"))


def assemble_book(
    repository: JobRepository, book_id: str, output_root: Path
) -> tuple[Path, Path, Path]:
    chapters = [job for job in repository.jobs(book_id) if job["status"] == "COMPLETED"]
    if not chapters or len(chapters) != len([j for j in repository.jobs(book_id) if j["status"] != "SKIPPED"]):
        raise RuntimeError("cannot assemble an incomplete book")
    assembled = output_root / safe_name(book_id) / "assembled"
    assembled.mkdir(parents=True, exist_ok=True)
    concat_file = assembled / "chapters.txt"
    lines = [
        f"file '{media_path(Path(job['local_audio_path'])).replace(chr(39), chr(39) + '\\' + chr(39) + chr(39))}'"
        for job in chapters
    ]
    atomic_write(concat_file, ("\n".join(lines) + "\n").encode("utf-8"))
    audio_output = assembled / f"{safe_name(book_id)}.mp3"
    run_media(
        "ffmpeg",
        [
            "-y", "-v", "error", "-f", "concat", "-safe", "0",
            "-i", media_path(concat_file), "-c", "copy", media_path(audio_output),
        ],
        check=True,
    )
    probe_duration(audio_output)
    srt_output = assembled / f"{safe_name(book_id)}.srt"
    merge_srts(chapters, srt_output)
    m4b_output = assembled / f"{safe_name(book_id)}.m4b"
    run_media(
        "ffmpeg",
        [
            "-y", "-v", "error", "-i", media_path(audio_output),
            "-vn", "-c:a", "aac", "-b:a", "96k", media_path(m4b_output),
        ],
        check=True,
    )
    probe_duration(m4b_output)
    return audio_output, srt_output, m4b_output


def load_document(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="crawler export JSON")
    parser.add_argument("--output", type=Path, default=Path("novel-audio"))
    parser.add_argument("--base-url", default="http://localhost:9549")
    parser.add_argument("--voice", default="zh-CN-YunxiNeural")
    parser.add_argument("--rate", default="+0%")
    parser.add_argument("--pitch", default="+0Hz")
    parser.add_argument("--volume", default="+0%")
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=3)
    parser.add_argument("--task-timeout", type=float, default=3600)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--assemble", action="store_true")
    parser.add_argument("--media-container", help="container providing ffmpeg and ffprobe")
    parser.add_argument("--media-host-root", type=Path, help="host side of the media bind mount")
    parser.add_argument("--media-container-root", type=Path, help="container side of the media bind mount")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    settings = Settings(args.voice, args.rate, args.pitch, args.volume, args.use_llm)
    configure_media_tools(args.media_container, args.media_host_root, args.media_container_root)
    repository = JobRepository(args.output / "tts-jobs.sqlite3")
    try:
        book_id = repository.ingest(load_document(args.input), settings)
        worker = NovelTTSWorker(
            repository,
            EasyVoiceClient(args.base_url),
            args.output,
            settings,
            args.poll_interval,
            args.task_timeout,
            args.retries,
        )
        manifest = worker.run_book(book_id)
        if args.assemble and manifest["status"] == "COMPLETED":
            audio, srt, m4b = assemble_book(repository, book_id, args.output)
            manifest["assembled"] = {
                "mp3": str(audio.resolve()),
                "m4b": str(m4b.resolve()),
                "srt": str(srt.resolve()),
                "durationSeconds": probe_duration(audio),
            }
            manifest_path = args.output / safe_name(book_id) / "manifest.json"
            atomic_write(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            print(f"assembled_audio={audio}")
            print(f"assembled_m4b={m4b}")
            print(f"assembled_srt={srt}")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0 if manifest["status"] == "COMPLETED" else 2
    finally:
        repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
