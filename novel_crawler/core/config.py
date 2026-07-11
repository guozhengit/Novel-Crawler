import json
from pathlib import Path
from typing import Any


def load_config(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        return json.loads(text)
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except Exception as exc:
            raise RuntimeError("读取 YAML 配置需要安装 PyYAML：pip install pyyaml") from exc
        data = yaml.safe_load(text)
        return data or {}
    raise ValueError(f"不支持的配置格式: {path}")
