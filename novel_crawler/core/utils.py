import re
from pathlib import Path
from urllib.parse import urljoin

INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_filename(name: str, max_len: int = 120) -> str:
    cleaned = INVALID_FILENAME.sub("_", name).strip(" ._")
    return (cleaned[:max_len] or "untitled")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def absolute_url(base: str, href: str) -> str:
    return urljoin(base, href)


def normalize_blank_lines(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out = []
    blank = 0
    for line in lines:
        if line.strip():
            blank = 0
            out.append(line.strip())
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip()


def progress_bar(current: int, total: int, width: int = 30, prefix: str = "") -> str:
    if total <= 0:
        return f"{prefix}[{'?' * width}] 0%"
    pct = min(current / total, 1.0)
    filled = int(pct * width)
    bar = "#" * filled + "-" * (width - filled)
    percent = int(pct * 100)
    return f"{prefix}[{bar}] {percent}% ({current}/{total})"


def parse_chapter_content(raw: str) -> tuple[str, str]:
    """统一解析章节文件格式：第一行是标题，空行后是正文。"""
    title, sep, body = raw.partition("\n\n")
    if not sep:
        return raw.strip(), ""
    return title.strip(), body.strip()


def format_chapter_content(title: str, body: str) -> str:
    """统一格式化章节内容为存储格式。"""
    return f"{title}\n\n{body}"
