# -*- coding: utf-8 -*-
from pathlib import Path
import json

# 修正两个形近字映射误判
map_path = Path("font_decode_map.json")
mp = json.loads(map_path.read_text(encoding="utf-8"))
mp[chr(0xAEB6)] = "大"
mp[chr(0xAF6D)] = "天"
map_path.write_text(json.dumps(mp, ensure_ascii=False), encoding="utf-8")

parts = [Path("林七夜_全本.txt"), Path("林七夜_1122-2032.txt")]
texts = []
for p in parts:
    text = p.read_text(encoding="utf-8")
    # 下载时旧映射产生的形近字误判，按语境全局修正
    text = text.replace("夭", "天").replace("犬", "大")
    texts.append(text.rstrip() + "\n\n\n")

out = Path("林七夜_全本_合并版.txt")
out.write_text("".join(texts), encoding="utf-8")

text = out.read_text(encoding="utf-8")
titles = [line for line in text.splitlines() if line.startswith("第") and line.endswith("章")]
print("output", out)
print("bytes", out.stat().st_size)
print("chars", len(text))
print("title-lines", len(titles))
print("first", titles[:3])
print("last", titles[-10:])
print("residual_hangul", sum(1 for ch in text if 0xAC00 <= ord(ch) <= 0xD7A3))
