"""Read-only calibration for group sender recognition. Never clicks or types.

Run from the control window ("校准群聊识别") or directly:
    python inspect_group.py
with a group chat open in WeChat. Writes crops and a report to data/debug/.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from PIL import ImageGrab, ImageOps

from settings import DATA


def inspect(adapter, out_dir: Path) -> str:
    from ocr import prepare, read_lines, read_text
    from wechat_adapter import GROUP_LAYOUT, _user32, descendants

    title, single = adapter.current()
    if title is None:
        raise RuntimeError("没找到聊天标题，请在微信里打开一个群")
    if single:
        raise RuntimeError(f"当前打开的「{title}」是私聊；请打开一个群再校准")
    out = Path(out_dir) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    listing = adapter.window.ListControl(AutomationId="chat_message_list")
    rect = listing.BoundingRectangle
    dpi = _user32.GetDpiForWindow(adapter.hwnd) / 96
    frame = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
    frame.save(out / "list.png")
    layout = {**GROUP_LAYOUT, **(adapter.layout or {})}
    saved_mode, adapter.mode = adapter.mode, "group"
    report, names = [], []
    try:
        rows = [i for i in listing.GetChildren() if i.ControlTypeName == "ListItemControl"][-8:]
        for n, item in enumerate(rows):
            box = item.BoundingRectangle
            if box.bottom <= rect.top or box.top >= rect.bottom:
                continue
            controls = list(descendants(item))
            top, bottom = box.top - rect.top, box.bottom - rect.top
            row_img = frame.crop((0, max(0, top), frame.width, min(frame.height, bottom)))
            row_img.save(out / f"row{n}.png")
            left = ImageOps.expand(ImageOps.autocontrast(ImageOps.grayscale(
                row_img.crop((0, 0, row_img.width * 2 // 3, row_img.height))), cutoff=1), 10, fill=255)
            role = adapter.role_of(item, controls, rect, lambda: frame)
            entry = {"row": n, "class": item.ClassName, "name": item.Name, "role": role,
                     "children": [[c.ControlTypeName, c.Name, c.AutomationId] for c in controls if c.Name],
                     "ocr_left_lines": read_lines(left.convert("RGB"), 2)}
            if role == "user":
                avatar, strip = adapter.crops(item, rect, lambda: frame, dpi)
                avatar.save(out / f"row{n}_avatar.png")
                strip.save(out / f"row{n}_name.png")
                entry["ocr_name_strip"] = read_text(prepare(strip), 2)
                names.append(entry["ocr_name_strip"] or "?")
            report.append(entry)
    finally:
        adapter.mode = saved_mode
    (out / "report.json").write_text(json.dumps({"title": title, "dpi": dpi, "layout": layout,
                                                 "rows": report}, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    return (f"校准完成（{title}）：最近群友消息认出的昵称：{('、'.join(names) or '无')}。"
            f"截图和报告在 {out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    from wechat_adapter import WeChatAdapter, auto
    with auto.UIAutomationInitializerInThread():
        print(inspect(WeChatAdapter("", "group"), DATA / "debug"))
