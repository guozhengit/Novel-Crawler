# -*- coding: utf-8 -*-
from pathlib import Path

for name in ["林七夜_全本.txt", "林七夜_1122-2032.txt"]:
    p = Path(name)
    if not p.exists():
        print(name, "missing")
        continue
    text = p.read_text(encoding="utf-8")
    titles = [line for line in text.splitlines() if line.startswith("第") and line.endswith("章")]
    print(name)
    print("bytes", p.stat().st_size, "chars", len(text), "title-lines", len(titles))
    print("first", titles[:3])
    print("last", titles[-10:])
    print("tail", repr(text[-200:]))
    print()
