"""Live settings shared by the control window and the worker thread."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DEFAULTS = {
    "base_url": "http://127.0.0.1:1234/v1",
    "lms_path": str(Path.home() / ".lmstudio" / "bin" / "lms.exe"),
    "model": {"key": "your-local-model", "identifier": "xiaoqian-qwen", "context": 8192},
    "persona": "小千",
    "chat": "示例好友",
    # Only chats listed here can ever be read or answered.
    "chats": {"示例好友": {"mode": "private"}},
    # Watch every allowed chat at once, switching between them when one has news.
    "multi_chat": False,
    # Whose private chat may tell her to say things in groups.
    "owner": "示例好友",
    "poll_seconds": 1,
    "batch_seconds": 3,
    "minimum_gap_seconds": 10,
    "max_messages_per_10_minutes": 20,
    "max_reply_delay_seconds": 180,
    "proactive": True,
    "proactive_after_seconds": [150, 240],
    "proactive_window_seconds": 1800,
    "quiet_hours": [1, 8],
    "group_gap_seconds": 60,
    "group_chime_in": 0.3,  # chance she even considers joining when nobody called her
    "history_messages": 40,
    "max_reply_chars": 100,
    "describe_images": True,
    # Sending stickers pastes an image file; keep off until verified on this WeChat build.
    "stickers_enabled": False,
    "web_search": True,
    "collect_stickers": True,  # save stickers others send into the inbox for review
    # The bot account's own WeChat nickname(s); "@机器人昵称" in a group counts as calling her.
    "aliases": [],
    "memory_enabled": True,
    "memory_every": 12,  # new messages between memory updates
    # The owner's private chat can hand tasks to local command-line agents (agents.py).
    "agents_enabled": True,
    "agent_passphrase": "",  # empty: no passphrase needed
    "agent_overrides": {},   # fields over agents.default_configs(), e.g. {"codex": {"model": "…"}}
}


def merged(defaults, values):
    result = copy.deepcopy(defaults)
    for key, value in values.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict) and key != "chats":
            result[key] = merged(result[key], value)
        else:
            result[key] = value
    return result


class Settings:
    """Dict-like, thread safe, saved atomically on every change."""

    def __init__(self, path: Path = DATA / "settings.json"):
        self.path = Path(path)
        self.lock = threading.RLock()
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            values = self.migrate()
        self.values = merged(DEFAULTS, values)
        self.save()

    def migrate(self):
        """Carry over the Codex pilot's config.json once."""
        try:
            old = json.loads((self.path.parent.parent / "config.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        keep = {k: old[k] for k in DEFAULTS if k in old and not isinstance(DEFAULTS[k], dict)}
        if "target" in old:
            keep["chat"] = old["target"]
            keep["chats"] = {old["target"]: {"mode": old.get("mode", "private")}}
        if "model" in old:
            keep["model"] = {"identifier": old["model"]}
        return keep

    def __getitem__(self, key):
        with self.lock:
            return copy.deepcopy(self.values[key])

    def get(self, key, default=None):
        with self.lock:
            return copy.deepcopy(self.values.get(key, default))

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.values)

    def update(self, **changes):
        with self.lock:
            self.values.update(copy.deepcopy(changes))
            self.save()

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)

    def chat_mode(self, chat):
        return self.get("chats", {}).get(chat, {}).get("mode")
