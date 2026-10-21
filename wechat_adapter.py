"""WeChat 4.1 UI adapter. Reads and writes only the chat whose title matches the target."""
from __future__ import annotations

import ctypes
import hashlib
import json
import re
import struct
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import uiautomation as auto
from PIL import ImageGrab, ImageOps, ImageStat

from senders import SenderBook, avatar_hash

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetDpiForWindow.argtypes = [wintypes.HWND]


def visible_point(hwnd, x, y):
    hit = _user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return _user32.GetAncestor(hit, 2) == hwnd


def foreground():
    return _user32.GetForegroundWindow()


def chat_title(name: str) -> str:
    """Group titles carry a member count, e.g. '测试成员甲(12)'."""
    return re.sub(r"\s*[(（]\d+[)）]\s*$", "", name or "").strip()


@dataclass
class Message:
    key: str
    text: str
    role: str
    sender: str = ""
    image: object = None  # PIL crop of a sticker/picture bubble, for incoming media only


TEXT_ITEM = "mmui::ChatTextItemView"
# WeChat 4.1 row classes, as mapped by pyweixin (Hello-Mr-Crab/pywechat): quoted replies,
# pictures, videos and stickers are ChatBubbleReferItemView; files, links and red
# packets are ChatBubbleItemView; times and notices are system rows.
CONTENT_ITEMS = {TEXT_ITEM, "mmui::ChatBubbleReferItemView", "mmui::ChatBubbleItemView",
                 "mmui::ChatPersonalCardItemView"}
SYSTEM_ITEMS = {"mmui::ChatItemView", "mmui::ChatSystemInfoItemView"}
MEDIA_ITEM = re.compile(r"Emoti|Emoji|Sticker|Image|Picture|Photo|Gif", re.I)
MEDIA_NAME = re.compile(r"^\[?(?:动画表情|表情|图片|视频|Sticker|Photo|Image|Video)\]?")


# Where a group member's avatar and nickname sit inside a message row, in 96-dpi
# pixels from the row's top-left. Calibrate with inspect_group.py.
GROUP_LAYOUT = {"avatar_left": 20, "avatar_top": 10, "avatar_size": 36,
                "name_left": 68, "name_top": 4, "name_height": 22, "name_width": 360}


def descendants(control, depth=6):
    if depth <= 0:
        return
    for child in control.GetChildren():
        yield child
        yield from descendants(child, depth - 1)


class WeChatAdapter:
    def __init__(self, target: str, mode: str = "private", max_chars: int = 100):
        self.target, self.mode, self.max_chars = target, mode, max_chars
        self.window = auto.WindowControl(searchDepth=1, ClassName="mmui::MainWindow")
        self.book, self.layout, self.sender_cache, self.row_cache = None, dict(GROUP_LAYOUT), {}, {}

    def set_target(self, target, mode, book: SenderBook | None = None, layout=None):
        self.target, self.mode, self.book = target, mode, book
        self.layout = {**GROUP_LAYOUT, **(layout or {})}
        self.sender_cache, self.row_cache = {}, {}

    @property
    def hwnd(self):
        return self.window.NativeWindowHandle

    def check_window(self):
        if not self.window.Exists(0.3):
            raise RuntimeError("微信主窗口未找到，请登录机器人微信")
        if self.window.IsOffscreen:
            raise RuntimeError("微信已最小化，请恢复窗口")

    def current(self):
        """(title, is_private) of the chat open in WeChat, or (None, None)."""
        self.check_window()
        title = self.window.TextControl(RegexAutomationId=r".*big_title_line_h_view$")
        if not title.Exists(0.3):
            return None, None
        single = self.window.GroupControl(ClassName="mmui::ChatTitleBarChatSingleView").Exists(0)
        return chat_title(title.Name), single

    def validate(self):
        title, single = self.current()
        if title != self.target:
            raise RuntimeError(f"当前打开的不是「{self.target}」，暂停收发")
        if single != (self.mode == "private"):
            raise RuntimeError(f"「{self.target}」的聊天类型和设置不符（私聊/群聊），暂停收发")
        edit = self.window.EditControl(AutomationId="chat_input_field")
        if not edit.Exists(0.3):
            raise RuntimeError("找不到输入框，暂停收发")
        return edit

    def sessions(self):
        """Names in the visible part of WeChat's conversation list."""
        self.check_window()
        listing = self.window.ListControl(AutomationId="session_list")
        if not listing.Exists(0.5):
            raise RuntimeError("找不到微信会话列表，请切到“微信”聊天页")
        return [item.AutomationId[len("session_item_"):] for item in listing.GetChildren()
                if item.AutomationId.startswith("session_item_")]

    def session_states(self):
        """{chat: preview text} for the visible conversation list; a change means news."""
        listing = self.window.ListControl(AutomationId="session_list")
        if not listing.Exists(0.3):
            return {}
        return {item.AutomationId[len("session_item_"):]: item.Name for item in listing.GetChildren()
                if item.AutomationId.startswith("session_item_")}

    def open_chat(self, name):
        """Click `name` in the conversation list and confirm the title changed."""
        self.check_window()
        if self.current()[0] == name:
            return
        item = self.window.ListItemControl(AutomationId="session_item_" + name, searchDepth=30)
        if not item.Exists(0.5):
            raise RuntimeError(f"会话列表里看不到「{name}」。请先在微信里给它发过消息或置顶，让它出现在列表可见处")
        previous = foreground()
        self.window.SetActive(waitTime=0.1)
        box = item.BoundingRectangle
        if not visible_point(self.hwnd, (box.left + box.right) // 2, (box.top + box.bottom) // 2):
            raise RuntimeError("会话列表被其他窗口遮住，无法切换")
        item.Click(simulateMove=False, waitTime=0.05)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.current()[0] != name:
            time.sleep(0.2)
        self.restore(previous)
        if self.current()[0] != name:
            raise RuntimeError(f"点击后没有切到「{name}」，请手动打开")

    def restore(self, previous):
        if previous and previous != self.hwnd and foreground() == self.hwnd:
            try:
                auto.ControlFromHandle(previous).SetActive(waitTime=0)
            except Exception:
                pass

    def role_of(self, item, controls, rect, grab):
        avatars = [c for c in controls if c.ControlTypeName == "ButtonControl" and c.Name]
        if avatars:
            box = avatars[0].BoundingRectangle
            if box.left < rect.left + rect.width() * 0.3:
                if self.mode == "group" or avatars[0].Name == self.target:
                    return "user"
            elif box.left > rect.left + rect.width() * 0.7:
                return "assistant"
        # 4.1 exposes a whole-row text item without an avatar child. Compare the
        # left and right avatar strips with the background; never OCR.
        frame = grab()
        dpi = _user32.GetDpiForWindow(self.hwnd) / 96
        box = item.BoundingRectangle
        y0 = max(0, box.top - rect.top + round(10 * dpi))
        y1 = min(frame.height, y0 + round(24 * dpi))
        a, b = round(20 * dpi), round(48 * dpi)
        if y1 <= y0:
            return None
        background = ImageStat.Stat(frame.crop((4, y0, 10, y1))).mean[:3]

        def difference(x0, x1):
            pixels = list(frame.crop((x0, y0, x1, y1)).convert("RGB").getdata())
            return sum(max(abs(p[i] - background[i]) for i in range(3)) for p in pixels) / len(pixels)

        left, right = difference(a, b), difference(frame.width - b, frame.width - a)
        if left > 25 and right < 10:
            return "user"
        if right > 25 and left < 10:
            return "assistant"
        return None  # Ambiguous or occluded: fail closed.

    def read(self) -> list[Message]:
        self.validate()
        listing = self.window.ListControl(AutomationId="chat_message_list")
        if not listing.Exists(0.3):
            raise RuntimeError("消息列表未找到")
        rect = listing.BoundingRectangle
        dpi = _user32.GetDpiForWindow(self.hwnd) / 96
        cache = []

        def grab():
            if not cache:
                cache.append(ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True))
            return cache[0]

        result, visible_rows = [], 0
        for item in listing.GetChildren():
            if item.ControlTypeName != "ListItemControl":
                continue
            if item.ClassName in SYSTEM_ITEMS:
                continue
            media = item.ClassName != TEXT_ITEM and bool(
                MEDIA_ITEM.search(item.ClassName) or MEDIA_NAME.match(item.Name or ""))
            if item.ClassName not in CONTENT_ITEMS and not media:
                continue
            box = item.BoundingRectangle
            if box.bottom <= rect.top or box.top >= rect.bottom:
                continue
            visible_rows += 1
            # Read only rows whose avatar and bubble locations still belong to WeChat.
            y = min(rect.bottom - 2, max(rect.top + 2, box.top + round(25 * dpi)))
            if not all(visible_point(self.hwnd, x, y) for x in
                       (rect.left + round(32 * dpi), (rect.left + rect.right) // 2, rect.right - round(32 * dpi))):
                continue
            # A row's sender and text never change, so only new rows pay for the
            # child walk, the screenshot and OCR. This is what keeps reads fast.
            ident = (tuple(item.GetRuntimeId()), item.ClassName, item.Name)
            if ident in self.row_cache:
                result.append(self.row_cache[ident])
                continue
            controls = list(descendants(item))
            role = self.role_of(item, controls, rect, grab)
            if role is None:
                continue
            parts = []
            for c in controls:
                if c.ControlTypeName == "TextControl" and c.Name and c.Name not in (self.target, "已读") \
                        and c.Name not in parts:
                    parts.append(c.Name)
            text = ("\n".join(parts) or item.Name).strip()
            if (self.mode == "group" or media or item.ClassName != TEXT_ITEM) and item.Name:
                # Any extra text child next to the bubble content is the nickname.
                text = item.Name.strip()
            if media:
                text = "[图片]" if re.match(r"^\[?(?:图片|Photo|Image)", text) else \
                    "[视频]" if re.match(r"^\[?(?:视频|Video)", text) else "[表情包]"
            if not text:
                continue
            key = hashlib.sha256(json.dumps([list(item.GetRuntimeId()), role, text],
                                            ensure_ascii=False).encode()).hexdigest()
            sender = ""
            if self.mode == "group" and role == "user":
                extra = [p for p in parts if p != text and not MEDIA_NAME.match(p)]
                sender = self.sender_of(key, item, rect, grab, dpi, extra[0] if extra else None)
            image = self.bubble(item, rect, grab, dpi, role) if media and role == "user" else None
            result.append(Message(key, text, role, sender, image))
            if len(self.row_cache) > 2000:
                self.row_cache.clear()
            self.row_cache[ident] = Message(key, text, role, sender)  # image only matters the first time
        if visible_rows and not result:
            raise RuntimeError("消息区域被遮挡或收发方向无法确认，暂停这一轮")
        return result

    def bubble(self, item, rect, grab, dpi, role, wait=5.0):
        """The sticker/picture itself: the sender's side of the row, background trimmed.

        A fresh picture shows WeChat's grey loading box first; describing that made the
        model talk about a "broken image". Re-grab until it loads, else return None.
        """
        from stickers import looks_unloaded, trim
        box = item.BoundingRectangle
        top, bottom = max(rect.top, box.top), min(rect.bottom, box.bottom)
        if bottom - top < 8:
            return None
        margin = round(64 * dpi)
        x0, x1 = (rect.left + margin, rect.left + rect.width() * 2 // 3) if role == "user" else \
            (rect.left + rect.width() // 3, rect.right - margin)
        if self.mode == "group" and role == "user":
            top = min(bottom - 4, top + round((self.layout["name_top"] + self.layout["name_height"]) * dpi))
        deadline = time.monotonic() + wait
        while True:
            # A menu or another window over the bubble would be described as part of the sticker.
            if not visible_point(self.hwnd, (x0 + x1) // 2, (top + bottom) // 2) or \
                    not visible_point(self.hwnd, x0 + round(40 * dpi), (top + bottom) // 2):
                return None
            image = trim(ImageGrab.grab(bbox=(x0, top, x1, bottom), all_screens=True))
            if not looks_unloaded(image):
                return image
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.4)

    def crops(self, item, rect, grab, dpi):
        """(avatar image, nickname strip image) for a group row, from the list screenshot."""
        frame, box, lay = grab(), item.BoundingRectangle, self.layout
        top = box.top - rect.top

        def s(v):
            return round(v * dpi)

        x0, y0 = s(lay["avatar_left"]), top + s(lay["avatar_top"])
        avatar = frame.crop((x0, y0, x0 + s(lay["avatar_size"]), y0 + s(lay["avatar_size"])))
        nx, ny = s(lay["name_left"]), top + s(lay["name_top"])
        name = frame.crop((nx, ny, min(frame.width, nx + s(lay["name_width"])), ny + s(lay["name_height"])))
        return avatar, name

    def sender_of(self, key, item, rect, grab, dpi, uia_name):
        if key in self.sender_cache:
            return self.sender_cache[key]
        fingerprint, ocr_name = None, uia_name
        try:
            avatar, strip = self.crops(item, rect, grab, dpi)
            fingerprint = avatar_hash(avatar)
            if not ocr_name:
                from ocr import prepare, read_text
                ocr_name = read_text(prepare(strip), 2) or None
        except Exception:
            pass  # An unknown sender only degrades context; it never decides whether to send.
        name = self.book.resolve(fingerprint, ocr_name) if self.book else (ocr_name or "群友")
        if len(self.sender_cache) > 800:
            self.sender_cache.clear()
        self.sender_cache[key] = name
        return name

    @staticmethod
    def input_value(edit):
        try:
            return edit.GetValuePattern().Value or ""
        except Exception:
            return edit.GetTextPattern().DocumentRange.GetText(-1).rstrip("\r\n")

    @staticmethod
    def wait_for(condition, timeout, step=0.05):
        """Poll instead of sleeping a fixed time: typically done in one or two steps."""
        deadline = time.monotonic() + timeout
        while True:
            if condition():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(step)

    def focus_input(self, cancel):
        """Bring WeChat forward with an empty, focused input box; returns (edit, previous window)."""
        if cancel():
            raise RuntimeError("已暂停")
        edit = self.validate()
        if self.input_value(edit).strip():
            raise RuntimeError("输入框已有草稿，保留草稿并暂停发送")
        previous = foreground()
        self.window.SetActive(waitTime=0.1)
        box = edit.BoundingRectangle
        if not visible_point(self.hwnd, (box.left + box.right) // 2, (box.top + box.bottom) // 2):
            raise RuntimeError("微信输入框被其他窗口遮住，等待恢复可见")
        edit.Click(simulateMove=False, waitTime=0.05)
        if self.input_value(edit).strip() or cancel():
            raise RuntimeError("发送前状态发生变化")
        if foreground() != self.hwnd:
            raise RuntimeError("微信没有取得焦点，停止发送")
        return edit, previous

    def press_send(self, edit, cancel, previous):
        button = self.window.ButtonControl(Name="发送", searchDepth=30)
        if not button.Exists(0.3) or not button.IsEnabled:
            raise RuntimeError("发送按钮不可用，已保留草稿")
        # Last full check right before the irreversible click: still the right chat.
        self.validate()
        if cancel():
            raise RuntimeError("已暂停，草稿未发送")
        if foreground() != self.hwnd:
            raise RuntimeError("发送前焦点发生变化，保留草稿")
        button.Click(simulateMove=False, waitTime=0)
        # Do not retry an uncertain send; this avoids duplicate messages.
        if not self.wait_for(lambda: not self.input_value(edit).strip(), 1.5):
            raise RuntimeError("发送结果不确定，不自动重试；请查看微信")
        self.restore(previous)

    def send(self, text: str, cancel=lambda: False):
        if not text or "\n" in text or len(text) > self.max_chars:
            raise RuntimeError("回复内容超出长度限制")
        edit, previous = self.focus_input(cancel)
        edit.GetValuePattern().SetValue(text, waitTime=0)
        if not self.wait_for(lambda: self.input_value(edit).strip() == text, 1.0) or cancel():
            raise RuntimeError("草稿核对失败，未点击发送；请手动检查输入框")
        self.press_send(edit, cancel, previous)

    def send_image(self, path, cancel=lambda: False):
        """Paste an image file into the input box and send it. Restores clipboard text."""
        path = Path(path).resolve()
        if not path.is_file():
            raise RuntimeError(f"表情包文件不存在：{path.name}")
        edit, previous = self.focus_input(cancel)
        saved = clipboard_text()
        try:
            set_clipboard_files([path])
            auto.SendKeys("{Ctrl}v", waitTime=0.05)
            pasted = self.wait_for(lambda: bool(self.input_value(edit).strip()), 1.5)
        finally:
            set_clipboard_text(saved)
        if cancel():
            raise RuntimeError("已暂停，表情包留在输入框未发送；请手动清空")
        if not pasted:
            raise RuntimeError("粘贴表情包后输入框没有变化，未发送；请看微信是否弹出了窗口")
        self.press_send(edit, cancel, previous)


# ---- clipboard (CF_HDROP for files, CF_UNICODETEXT to restore the user's text) ----
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
CF_UNICODETEXT, CF_HDROP = 13, 15


def _open_clipboard():
    for _ in range(20):
        if _user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise RuntimeError("剪贴板被其他程序占用")


def _put(fmt, data: bytes):
    handle = _kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
    pointer = _kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, data, len(data))
    _kernel32.GlobalUnlock(handle)
    _open_clipboard()
    try:
        _user32.EmptyClipboard()
        if not _user32.SetClipboardData(fmt, handle):
            raise RuntimeError("写入剪贴板失败")
    finally:
        _user32.CloseClipboard()


def set_clipboard_files(paths):
    names = ("\0".join(str(p) for p in paths) + "\0\0").encode("utf-16-le")
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)  # DROPFILES, fWide=1
    _put(CF_HDROP, header + names)


def clipboard_text():
    _open_clipboard()
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = _kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(pointer)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def set_clipboard_text(text):
    if text is None:
        return
    _put(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
