"""Local sticker library: drop images into data/stickers/, the model labels them once."""
from __future__ import annotations

import json
import shutil
import threading
import time
import re
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat

from settings import DATA

KINDS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# WeChat renders these bracket codes as its built-in small emoji when sent as text.
WECHAT_EMOJI = (
    "微笑 撇嘴 色 发呆 得意 流泪 害羞 闭嘴 睡 大哭 尴尬 发怒 调皮 呲牙 惊讶 难过 囧 抓狂 吐 偷笑 愉快 白眼 "
    "傲慢 困 惊恐 憨笑 悠闲 咒骂 疑问 嘘 晕 衰 骷髅 敲打 再见 擦汗 抠鼻 鼓掌 坏笑 鄙视 委屈 快哭了 亲亲 "
    "可怜 笑脸 生病 脸红 破涕为笑 恐惧 失望 无语 嘿哈 捂脸 奸笑 机智 皱眉 耶 吃瓜 加油 汗 天啊 Emm 社会社会 "
    "旺柴 好的 打脸 哇 翻白眼 666 让我看看 叹气 苦涩 裂开 爱心 心碎 拥抱 强 弱 握手 胜利 抱拳 OK 合十 "
    "啤酒 咖啡 蛋糕 玫瑰 凋谢 炸弹 便便 月亮 太阳 庆祝 礼物 红包 烟花 爆竹 猪头 跳跳 发抖 转圈").split()


def first_frame(path: Path) -> Image.Image:
    with Image.open(path) as image:
        image.seek(0)
        return image.convert("RGB")


def trim(image: Image.Image, tolerance=18) -> Image.Image:
    """Cut away the chat background around a sticker bubble."""
    rgb = image.convert("RGB")
    background = Image.new("RGB", rgb.size, rgb.getpixel((2, rgb.height // 2)))
    diff = ImageOps.grayscale(ImageChops.difference(rgb, background)).point(lambda v: 255 if v > tolerance else 0)
    box = diff.getbbox()
    return rgb.crop(box) if box else rgb


def looks_unloaded(image: Image.Image) -> bool:
    """WeChat's loading / failed placeholder: a small or nearly flat grey box."""
    if image.width < 24 or image.height < 24:
        return True
    gray = ImageOps.grayscale(image)
    return ImageStat.Stat(gray).stddev[0] < 18


PLACEHOLDER_WORDS = ("加载失败", "占位", "未加载", "加载中", "无法显示", "图片损坏", "空白")


def set_desc(entries, key, desc):
    desc = " ".join(str(desc).split())[:80]
    if not desc:
        raise ValueError("描述不能为空")
    if key not in entries:
        raise KeyError(key)
    entries[key]["desc"] = desc


def sticker_id(path: Path) -> str:
    return re.sub(r"\s+", "_", path.stem)[:40]


class StickerLibrary:
    def __init__(self, folder: Path = DATA / "stickers"):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.index_path = self.folder / "index.json"
        try:
            self.index = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.index = {}
        self.lock = threading.RLock()  # the scan runs on its own thread
        self.skipped = []
        self.inbox = StickerInbox(self.folder / "_inbox")

    def hashes(self):
        """Fingerprints of the library, so a sticker already kept is not collected again."""
        from senders import avatar_hash
        out = []
        for path in self.files().values():
            try:
                out.append(avatar_hash(trim(first_frame(path))))
            except OSError:
                continue
        return out

    def files(self):
        return {sticker_id(p): p for p in sorted(self.folder.iterdir()) if p.suffix.lower() in KINDS}

    def save(self):
        with self.lock:
            temp = self.index_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.index, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.index_path)

    def scan(self, describe, progress=lambda done, total: None) -> int:
        """Label new images with `describe(image) -> str`; forget deleted ones.

        Runs for minutes on a big folder while the owner may still be tidying it,
        so one missing or unreadable picture is skipped instead of ending the scan.
        Labels are saved one by one: an interrupted scan resumes where it stopped.
        """
        files = self.files()
        with self.lock:
            for gone in set(self.index) - set(files):
                self.index.pop(gone)
            new = [i for i in files if i not in self.index or self.index[i].get("file") != files[i].name]
        self.skipped = []
        for n, sid in enumerate(new, 1):
            try:
                desc = describe(first_frame(files[sid]))
            except Exception as exc:  # gone, broken, or the model hiccuped on this one
                self.skipped.append(f"{files[sid].name}：{str(exc)[:60]}")
                progress(n, len(new))
                continue
            with self.lock:
                self.index[sid] = {"file": files[sid].name, "desc": desc}
            self.save()
            progress(n, len(new))
        self.save()
        return len(new) - len(self.skipped)

    def catalog(self, limit=60):
        files = self.files()
        with self.lock:
            rows = list(self.index.items())
        return [(sid, row["desc"]) for sid, row in rows if sid in files and row.get("desc")][:limit]

    def path(self, sid):
        from social_policy import selected_sticker
        return selected_sticker(self, sid)

    def set_desc(self, sid, desc):
        """The owner's wording wins; rescans keep it because the file is unchanged."""
        with self.lock:
            set_desc(self.index, sid, desc)
        self.save()

    def remove(self, sid):
        path = self.path(sid)
        with self.lock:
            self.index.pop(sid, None)
        if path:
            path.unlink(missing_ok=True)
        self.save()

    def add(self, source: Path, desc: str, name: str) -> str:
        """Adopt an already-described image (from the inbox) without asking the model again."""
        base = re.sub(r'[\/:*?"<>|\s，。！？、,.!?]+', "_", name).strip("_")[:16] or "表情包"
        sid, n = base, 2
        while (self.folder / f"{sid}{source.suffix}").exists():
            sid, n = f"{base}_{n}", n + 1
        target = self.folder / f"{sid}{source.suffix}"
        shutil.copyfile(source, target)
        with self.lock:
            self.index[sticker_id(target)] = {"file": target.name, "desc": desc}
        self.save()
        return sticker_id(target)


class StickerInbox:
    """Stickers other people sent, waiting for the owner to keep or discard them.

    Only crops of the chat window are kept (static png): WeChat's own sticker files
    are encrypted and are never read.
    """

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.index_path = self.folder / "index.json"
        self.lock = threading.RLock()
        try:
            self.items = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.items = {}

    def save(self):
        with self.lock:
            temp = self.index_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.index_path)

    def add(self, image: Image.Image, desc: str, chat: str, sender: str, known_hashes=()) -> str | None:
        """Store a new sticker crop; None if it duplicates the inbox or the library."""
        from senders import avatar_hash, distance
        mark = avatar_hash(trim(image))  # same normalisation as StickerLibrary.hashes()
        hashes = [int(i["hash"]) for i in self.items.values()] + list(known_hashes)
        if any(distance(mark, h) <= 6 for h in hashes) or len(self.items) >= 300:
            return None
        with self.lock:
            iid = f"{int(time.time() * 1000)}"
            image.convert("RGB").save(self.folder / f"{iid}.png")
            self.items[iid] = {"desc": desc, "chat": chat, "sender": sender, "hash": str(mark),
                               "time": time.strftime("%m-%d %H:%M")}
            self.save()
        return iid

    def path(self, iid):
        return self.folder / f"{iid}.png"

    def keep(self, iid, library: "StickerLibrary", name=None) -> str:
        item = self.items[iid]
        sid = library.add(self.path(iid), item["desc"], name or item["desc"])
        self.discard(iid)
        return sid

    def set_desc(self, iid, desc):
        with self.lock:
            set_desc(self.items, iid, desc)
            self.save()

    def discard(self, iid):
        with self.lock:
            self.items.pop(iid, None)
            self.path(iid).unlink(missing_ok=True)
            self.save()
