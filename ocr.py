"""Offline text recognition: RapidOCR (PaddleOCR models) for nicknames, the Windows built-in engine (zh-Hans)
as the fallback and for everything else."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent / "vendor_ocr"))  # optional local install; after site-packages
try:
    import onnxruntime  # noqa: F401  before winrt: its older msvcp140.dll crashes onnxruntime loaded later
except Exception:
    pass

from PIL import Image, ImageOps, ImageStat
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter

_engine = None
_rapid = None
NOISE = "「」|丨｜[]【】:：.,，。·'\""


def engine():
    global _engine
    if _engine is None:
        _engine = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
        if _engine is None:
            raise RuntimeError("Windows 没有中文 OCR 语言包，无法识别群昵称")
    return _engine


async def _recognize(image: Image.Image):
    rgba = image.convert("RGBA")
    r, g, b, a = rgba.split()
    raw = Image.merge("RGBA", (b, g, r, a)).tobytes()  # BGRA8
    writer = DataWriter()
    writer.write_bytes(raw)
    bitmap = SoftwareBitmap(BitmapPixelFormat.BGRA8, rgba.width, rgba.height, BitmapAlphaMode.PREMULTIPLIED)
    bitmap.copy_from_buffer(writer.detach_buffer())
    result = await engine().recognize_async(bitmap)
    return [line.text for line in result.lines]


def prepare(image: Image.Image) -> Image.Image:
    """Dark text on a white margin, whatever WeChat's theme (dark mode is light-on-dark)."""
    gray = ImageOps.grayscale(image.convert("RGB"))
    if ImageStat.Stat(gray).mean[0] < 128:
        gray = ImageOps.invert(gray)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    # Crop to the text itself: a short nickname in a wide empty strip reads as nothing.
    box = gray.point(lambda v: 255 if v < 128 else 0).getbbox()
    if box:
        gray = gray.crop((max(0, box[0] - 4), 0, min(gray.width, box[2] + 4), gray.height))
    return ImageOps.expand(gray, 12, fill=255).convert("RGB")


def read_lines(image: Image.Image, scale: int = 3) -> list[str]:
    """Small UI text reads far better enlarged."""
    if scale > 1:
        image = image.resize((image.width * scale, image.height * scale), Image.LANCZOS)
    return asyncio.run(_recognize(image))


def read_text(image: Image.Image, scale: int = 3) -> str:
    # The engine puts spaces between CJK glyphs; nicknames rarely contain real spaces.
    # Edge strokes (avatar borders, bubble tails) often come back as 「|丨 noise.
    return "".join("".join(read_lines(image, scale)).split()).strip(NOISE)


def rapid():
    """RapidOCR, or None when vendor_ocr is missing. Windows OCR returns nothing for names like 嘎嘎嘎."""
    global _rapid
    if _rapid is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _rapid = RapidOCR()
        except Exception:
            _rapid = False
    return _rapid or None


def read_name(image: Image.Image, min_score: float = 0.8) -> str:
    """A raw nickname strip from the screenshot -> the nickname, or "" when unsure (a wrong name sticks)."""
    reader = rapid()
    if reader is None:
        return read_text(prepare(image), 2)
    import numpy
    found, _ = reader(numpy.array(image.convert("RGB")), use_cls=False)
    lines = [text for _, text, score in (found or []) if float(score) >= min_score]
    return "".join(lines[0].split()).strip(NOISE) if lines else ""
