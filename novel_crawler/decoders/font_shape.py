import importlib
import json
from pathlib import Path


class FontShapeDecoderBuilder:
    """通过字形轮廓比对生成混淆字体映射表。

    依赖 fontTools、Pillow、numpy。若缺失依赖或中文字体，调用方应降级为已有映射表。
    """

    def __init__(self, reference_fonts: list[Path], size: int = 64):
        self.reference_fonts = reference_fonts
        self.size = size

    def build_map(self, obfuscated_font: Path, output: Path | None = None) -> dict[str, str]:
        from fontTools.ttLib import TTFont
        from PIL import Image, ImageDraw, ImageFont

        np = importlib.import_module("numpy")

        ttf_path = self._ensure_ttf(obfuscated_font)
        obf_font = ImageFont.truetype(str(ttf_path), 72)
        tt = TTFont(str(ttf_path))
        codepoints = sorted(tt.getBestCmap().keys())
        candidates = self._candidate_chars()

        mats = []
        for ref in self.reference_fonts:
            font = ImageFont.truetype(str(ref), 72)
            chars, vecs = [], []
            for ch in candidates:
                vector = self._render_crop(font, ch, Image, ImageDraw, np)
                if vector is not None:
                    chars.append(ch)
                    vecs.append(vector)
            if vecs:
                matrix = np.stack(vecs)
                matrix = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-8)
                mats.append((matrix, chars))
        if not mats:
            raise RuntimeError("没有可用参考字体")

        mapping: dict[str, str] = {}
        for cp in codepoints:
            vector = self._render_crop(obf_font, chr(cp), Image, ImageDraw, np)
            if vector is None:
                continue
            vector = vector / (np.linalg.norm(vector) + 1e-8)
            best = (-1.0, None)
            for matrix, chars in mats:
                sims = matrix @ vector
                idx = int(np.argmax(sims))
                score = float(sims[idx])
                if score > best[0]:
                    best = (score, chars[idx])
            if best[1]:
                mapping[chr(cp)] = best[1]

        if output:
            output.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        return mapping

    def _ensure_ttf(self, font_path: Path) -> Path:
        if font_path.suffix.lower() != ".woff2":
            return font_path
        from fontTools.ttLib import TTFont
        target = font_path.with_suffix(".ttf")
        if target.exists():
            return target
        tt = TTFont(str(font_path))
        tt.flavor = None
        tt.save(str(target))
        return target

    def _candidate_chars(self) -> list[str]:
        chars = set()
        for i in range(0xB0, 0xF8):
            for j in range(0xA1, 0xFF):
                try:
                    chars.add(bytes([i, j]).decode("gb2312"))
                except Exception:
                    pass
        for hi in range(0xA4, 0xC7):
            for lo in list(range(0x40, 0x7F)) + list(range(0xA1, 0xFF)):
                try:
                    chars.add(bytes([hi, lo]).decode("big5"))
                except Exception:
                    pass
        return [ch for ch in chars if len(ch) == 1 and 0x4E00 <= ord(ch) <= 0x9FFF]

    def _render_crop(self, font, ch, Image, ImageDraw, np):
        big = 96
        img = Image.new("L", (big, big), 0)
        draw = ImageDraw.Draw(img)
        draw.text((big // 2, big // 2), ch, fill=255, font=font, anchor="mm")
        arr = np.asarray(img)
        ys, xs = np.where(arr > 40)
        if len(xs) == 0:
            return None
        crop = img.crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)).resize((self.size, self.size))
        return np.asarray(crop, dtype=np.float32).ravel()
