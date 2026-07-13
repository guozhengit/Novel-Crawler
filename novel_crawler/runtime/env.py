import importlib.util
import os
import platform
from dataclasses import dataclass
from pathlib import Path

from novel_crawler.core.utils import ensure_dir


@dataclass
class RuntimeContext:
    os_name: str
    python_version: str
    project_dir: Path
    data_dir: Path
    cache_dir: Path
    output_dir: Path
    db_path: Path
    font_dirs: list[Path]
    chinese_fonts: list[Path]
    features: dict[str, bool]
    proxies: dict[str, str]


def detect_os() -> str:
    name = platform.system().lower()
    if name == "darwin":
        return "macos"
    if name in {"windows", "linux"}:
        return name
    return name or "unknown"


def detect_features() -> dict[str, bool]:
    packages = {
        "requests": "requests",
        "bs4": "bs4",
        "lxml": "lxml",
        "fonttools": "fontTools",
        "pillow": "PIL",
        "numpy": "numpy",
        "ebooklib": "ebooklib",
        "charset_normalizer": "charset_normalizer",
    }
    return {name: importlib.util.find_spec(pkg) is not None for name, pkg in packages.items()}


def get_font_dirs(os_name: str) -> list[Path]:
    home = Path.home()
    if os_name == "windows":
        return [Path("C:/Windows/Fonts")]
    if os_name == "macos":
        return [Path("/System/Library/Fonts"), Path("/Library/Fonts"), home / "Library/Fonts"]
    if os_name == "linux":
        return [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), home / ".fonts", home / ".local/share/fonts"]
    return []


FONT_PRIORITY = [
    "msyh.ttc", "msjh.ttc", "simhei.ttf", "simsun.ttc", "simkai.ttf",
    "PingFang.ttc", "Hiragino Sans GB.ttc", "Songti.ttc",
    "NotoSansCJK-Regular.ttc", "NotoSerifCJK-Regular.ttc", "SourceHanSansCN-Regular.otf",
    "wqy-microhei.ttc", "wqy-zenhei.ttc",
]

LEGACY_DATA_ENTRIES = ("crawler.db", "cache", "contents", "output")


def find_chinese_fonts(font_dirs: list[Path]) -> list[Path]:
    found: list[Path] = []
    for name in FONT_PRIORITY:
        for directory in font_dirs:
            if not directory.exists():
                continue
            direct = directory / name
            if direct.exists():
                found.append(direct)
                continue
            try:
                found.extend(directory.rglob(name))
            except Exception:
                pass
    seen = set()
    unique = []
    for path in found:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def detect_proxies() -> dict[str, str]:
    proxies = {}
    for scheme in ("http", "https"):
        value = os.getenv(f"{scheme.upper()}_PROXY") or os.getenv(f"{scheme}_proxy")
        if value:
            proxies[scheme] = value
    return proxies


def default_data_dir(app_name: str = "novel-crawler") -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif platform.system().lower() == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / app_name


def create_runtime_context(
    project_dir: Path | None = None,
    data_dir: Path | None = None,
) -> RuntimeContext:
    project_dir = (project_dir or Path.cwd()).resolve()
    if data_dir is None:
        legacy_data_dir = project_dir / "data"
        data_dir = legacy_data_dir if any((legacy_data_dir / name).exists() for name in LEGACY_DATA_ENTRIES) else default_data_dir()
    data_dir = ensure_dir(data_dir.resolve())
    cache_dir = ensure_dir(data_dir / "cache")
    output_dir = ensure_dir(data_dir / "output")
    os_name = detect_os()
    font_dirs = get_font_dirs(os_name)
    return RuntimeContext(
        os_name=os_name,
        python_version=platform.python_version(),
        project_dir=project_dir,
        data_dir=data_dir,
        cache_dir=cache_dir,
        output_dir=output_dir,
        db_path=data_dir / "crawler.db",
        font_dirs=font_dirs,
        chinese_fonts=find_chinese_fonts(font_dirs),
        features=detect_features(),
        proxies=detect_proxies(),
    )


def format_runtime_report(ctx: RuntimeContext) -> str:
    feature_lines = "\n".join(f"  {k}: {'yes' if v else 'no'}" for k, v in sorted(ctx.features.items()))
    font_lines = "\n".join(f"  {p}" for p in ctx.chinese_fonts[:8]) or "  <none>"
    proxy_lines = "\n".join(f"  {k}: {v}" for k, v in ctx.proxies.items()) or "  <none>"
    return (
        f"Runtime:\n"
        f"  OS: {ctx.os_name}\n"
        f"  Python: {ctx.python_version}\n"
        f"  Project: {ctx.project_dir}\n"
        f"  Cache: {ctx.cache_dir}\n"
        f"  Output: {ctx.output_dir}\n"
        f"\nFeatures:\n{feature_lines}\n"
        f"\nFonts:\n{font_lines}\n"
        f"\nProxies:\n{proxy_lines}"
    )
