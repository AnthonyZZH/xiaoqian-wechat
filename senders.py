"""Who said it in a group: avatar fingerprint + OCR'd nickname + optional roster."""
from __future__ import annotations

import difflib
import json
from collections import Counter
from pathlib import Path

from PIL import Image, ImageOps


def avatar_hash(image: Image.Image) -> int:
    """64-bit difference hash; stable under small scaling/compression changes."""
    gray = ImageOps.grayscale(image).resize((9, 8), Image.LANCZOS)
    pixels = list(gray.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return bits


def distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def snap(name: str, roster: list[str], cutoff=0.5) -> str | None:
    """Closest roster name, to absorb OCR slips like 测试成元甲 → 测试成员甲."""
    if not name or not roster:
        return None
    if name in roster:
        return name
    match = difflib.get_close_matches(name, roster, n=1, cutoff=cutoff)
    return match[0] if match else None


class SenderBook:
    """Per-group memory of avatar fingerprints and the names seen with them."""

    MATCH = 8  # bits

    def __init__(self, path: Path, roster=None):
        self.path = Path(path)
        self.roster = [r for r in (roster or []) if r]
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = []
        self.people = [{"hash": int(p["hash"]), "names": Counter(p["names"]), "label": p.get("label")}
                       for p in data]

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [{"hash": str(p["hash"]), "names": dict(p["names"]), "label": p.get("label")}
                for p in self.people]
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def find(self, fingerprint):
        best = min(self.people, key=lambda p: distance(p["hash"], fingerprint), default=None)
        if best and distance(best["hash"], fingerprint) <= self.MATCH:
            return best
        return None

    @staticmethod
    def best_name(person):
        if person.get("label"):
            return person["label"]
        return person["names"].most_common(1)[0][0] if person["names"] else None

    def resolve(self, fingerprint: int | None, ocr_name: str | None) -> str:
        """Name for this bubble. Never raises; falls back to a stable 群友N label."""
        name = snap(ocr_name, self.roster) if self.roster else (ocr_name or None)
        if fingerprint is None:
            return name or "群友"
        person = self.find(fingerprint)
        if person is None:
            person = {"hash": fingerprint, "names": Counter(), "label": None}
            self.people.append(person)
        if name:
            person["names"][name] += 1
            self.save()
        known = self.best_name(person)
        if known:
            return known
        self.save()
        return f"群友{self.people.index(person) + 1}"
