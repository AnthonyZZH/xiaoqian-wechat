"""One chat's reply loop. Persistence and send control are independent of the LLM."""
from __future__ import annotations

import difflib
import json
import random
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from brain import mentions, wants_silence
from memory import Memory
from stickers import PLACEHOLDER_WORDS

# A short reply ending on one of these is a cut-off thought ("你发了个"), not a message.
DANGLING = tuple("个把给跟与被让对向从和在将的了")


def dangling(text):
    core = text.rstrip(" ~～…。！？!?")
    return len(core) <= 8 and core.endswith(DANGLING) and not core.endswith(("好的", "是的", "真的", "对的", "行了", "好了", "算了", "走了", "来了", "完了", "懂了", "对了", "得了"))
from personas import safe_name
from senders import avatar_hash, distance
from settings import DATA, ROOT

NEVER = float("inf")
IMAGE_CACHE = []  # (fingerprint, description), shared by every chat in this process


def compact(text):
    return "".join(str(text).split())


def signature(row):
    return row.role, compact(row.text)


def sticker_hash(path):
    from stickers import first_frame, trim
    return avatar_hash(trim(first_frame(path)))


def load_history(path: Path, chat: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        pass
    try:
        # The Codex pilot kept history next to app.py; adopt it once, minus repeats.
        old = json.loads((ROOT / f"history_{chat}.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []
    return [row for i, row in enumerate(old) if i == 0 or row != old[i - 1]]


class Runner:
    def __init__(self, settings, adapter, enabled, stop, emit, brain, chat,
                 history_dir: Path = DATA / "history", reply_latest=False, stickers=None,
                 memory_dir: Path = DATA / "memory"):
        self.stickers = stickers
        self.memory = Memory(Path(memory_dir) / f"{safe_name(chat)}.json")
        self.unlearned, self.learn_failed_at = 0, -NEVER
        self.recent_stickers = deque()  # (monotonic, image hash) of stickers we pasted
        self.partner = ("", 0.0)  # group member last answered, and when
        # Owner tools, wired up by the scheduler: which chats she may speak in on the
        # owner's behalf, and a digest of those chats for the owner's private chat.
        self.relay_targets = lambda: []
        self.context = lambda: ""
        self.relay = None
        self.mode = settings.chat_mode(chat)
        if self.mode not in ("private", "group"):
            raise ValueError(f"「{chat}」不在允许的聊天列表里")
        self.config, self.adapter, self.brain, self.chat = settings, adapter, brain, chat
        self.enabled, self.stop, self.emit = enabled, stop, emit
        self.reply_latest = reply_latest
        self.seen, self.seen_order = set(), deque()
        self.history_path = Path(history_dir) / f"{safe_name(chat)}.json"
        self.history = load_history(self.history_path, chat)
        self.pending = []
        self.last_incoming = self.last_send = 0.0
        self.next_proactive = NEVER
        self.proactive_used = True
        self.muted = False
        self.sent_times = deque()
        self.recent_sends = deque()  # (monotonic, compact text), also for uncertain sends
        self.initialized = False
        self.active_epoch = None
        self.last_status = ""
        self.error_count = 0
        self.retry_after = 0.0

    @property
    def name(self):
        return self.config["persona"]

    def status(self, text):
        if text != self.last_status:
            self.last_status = text
            self.emit("status", text)

    def mark(self, msg):
        if msg.key in self.seen:
            return False
        self.seen.add(msg.key)
        self.seen_order.append(msg.key)
        if len(self.seen_order) > 1200:
            self.seen.discard(self.seen_order.popleft())
        return True

    def save(self):
        self.history = self.history[-max(self.config["history_messages"], 10):]
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.history_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.history, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.history_path)

    def clear_history(self):
        self.history = []
        self.save()

    def fresh(self, rows):
        """Unseen rows below the newest seen row; everything above it is old."""
        anchor = max((i for i, row in enumerate(rows) if row.key in self.seen), default=-1)
        if anchor < 0:
            # Switching chats rebuilds WeChat's rows with new ids; find our place by content.
            anchor = self.content_anchor(rows)
        self.tail = [signature(r) for r in rows][-40:]
        if anchor < 0:
            # List re-rendered, scrolled too far, or first load was empty: fail closed.
            if any([self.mark(row) for row in rows]):
                self.status("聊天列表刷新，无法确认哪些是新消息，这些消息不回复")
            return []
        for row in rows[:anchor + 1]:
            self.mark(row)
        return [row for row in rows[anchor + 1:] if self.mark(row)]

    def content_anchor(self, rows):
        """Index in `rows` of the last row we saw before, matched by the run of rows
        ending there (≥2 rows, or one long one). The latest match wins so repeated
        short messages never pull old rows back in. -1 if not found."""
        sigs = [signature(r) for r in rows]
        tail = getattr(self, "tail", [])
        for end in range(len(sigs) - 1, -1, -1):
            k = 0
            while k < len(tail) and end - k >= 0 and sigs[end - k] == tail[-1 - k]:
                k += 1
            if k >= 2 or (k == 1 and len(tail[-1][1]) >= 8):
                return end
        return -1

    def own_echo(self, text):
        """Our own bubble read back as someone else's within two minutes."""
        now, text = time.monotonic(), compact(text)
        while self.recent_sends and now - self.recent_sends[0][0] > 600:
            self.recent_sends.popleft()
        return any(now - at <= 120 and text == sent for at, sent in self.recent_sends)

    def own_sticker(self, image):
        """Our own pasted sticker read back as someone else's."""
        now, mark = time.monotonic(), avatar_hash(image)
        while self.recent_stickers and now - self.recent_stickers[0][0] > 120:
            self.recent_stickers.popleft()
        return any(distance(mark, sent) <= 10 for _, sent in self.recent_stickers)

    def describe(self, row):
        """'[表情包：…]' text for an incoming picture; the bare label if the model can't see.

        Stickers recur constantly, so descriptions are cached by image fingerprint.
        """
        if row.image is None or not self.config["describe_images"]:
            return row.text
        label = "图片" if "图片" in row.text else "视频" if "视频" in row.text else "表情包"
        mark = avatar_hash(row.image)
        known = next((desc for h, desc in IMAGE_CACHE if distance(h, mark) <= 6), None)
        if known:
            return f"[{label}：{known}]"
        try:
            desc = self.brain.describe_image(row.image)
        except Exception as exc:
            self.status(f"看不清这张图：{str(exc)[:60]}")
            return row.text
        if any(word in desc for word in PLACEHOLDER_WORDS):
            return f"[{label}]"  # Never let a loading box become a topic.
        if label == "表情包" and self.stickers and self.config["collect_stickers"]:
            # Stickers only, never photos: people's pictures stay out of the library.
            try:
                if self.stickers.inbox.add(row.image, desc, self.chat, getattr(row, "sender", ""),
                                           self.stickers.hashes()):
                    self.emit("inbox", desc)
            except OSError:
                pass
        IMAGE_CACHE.append((mark, desc))
        del IMAGE_CACHE[:-300]
        return f"[{label}：{desc}]"

    def accept(self, row):
        if row.role != "user" or self.own_echo(row.text):
            return False
        if getattr(row, "image", None) is not None:
            if self.own_sticker(row.image):
                return False
            row.text = self.describe(row)
        entry = {"role": "user", "text": row.text}
        sender = getattr(row, "sender", "")
        if self.mode == "group" and sender:
            entry["sender"] = sender
        self.history.append(entry)
        self.unlearned += 1
        self.pending.append(row)
        self.last_incoming = time.monotonic()
        self.proactive_used = False
        self.next_proactive = NEVER
        self.muted = wants_silence(row.text, self.name)
        self.emit("received", f"{sender}：{row.text}" if entry.get("sender") else row.text)
        return True

    def ingest(self, rows):
        changed = False
        for row in self.fresh(rows):
            changed = self.accept(row) or changed
        if changed:
            self.save()
        return changed

    def quiet(self):
        start, end = self.config["quiet_hours"]
        hour = datetime.now().hour
        return start <= hour < end if start < end else hour >= start or hour < end

    def allowed_to_send(self):
        now = time.monotonic()
        while self.sent_times and now - self.sent_times[0] > 600:
            self.sent_times.popleft()
        return (self.enabled.is_set() and not self.stop.is_set()
                and now - self.last_send >= self.config["minimum_gap_seconds"]
                and len(self.sent_times) < self.config["max_messages_per_10_minutes"])

    def expired(self, now):
        """A batch nobody answered in time is dropped instead of answered late."""
        if self.pending and now - self.last_incoming > self.config["max_reply_delay_seconds"]:
            self.pending.clear()
            self.proactive_used = True
            self.status("消息等太久了，这轮不再补回复")
            return True
        return False

    def parse(self, result):
        """(speak, text, sticker path or None); repeats, overlong text and unknown stickers are dropped."""
        if not isinstance(result, dict) or not isinstance(result.get("speak"), bool):
            raise ValueError("模型返回格式错误")
        text = " ".join(str(result.get("text") or "").split())
        recent = [compact(r["text"]) for r in self.history[-8:] if r["role"] == "assistant"]
        # Near-repeats too: a reworded copy of the last joke reads as a bot.
        def repeats(old, new=compact(text)):
            if min(len(old), len(new)) < 10:
                return old == new  # Short replies like "哈哈行" legitimately recur.
            return difflib.SequenceMatcher(None, new, old).ratio() >= 0.6

        if len(text) > self.config["max_reply_chars"] or any(repeats(old) for old in recent) or dangling(text):
            text = ""
        sticker = None
        if result.get("sticker") and self.stickers and self.config["stickers_enabled"]:
            sticker = self.stickers.path(result["sticker"])
            if sticker and self.sticker_label(result["sticker"]) in {r["text"] for r in self.history[-6:]}:
                sticker = None  # Same sticker again so soon reads as a bot.
        if not result["speak"] or not (text or sticker):
            return False, "", None
        return True, text, (result["sticker"], sticker) if sticker else None

    def sent(self, text, proactive):
        """Bookkeeping for one confirmed message, saved before anything else can fail."""
        self.last_send = time.monotonic()
        self.sent_times.append(self.last_send)
        self.history.append({"role": "assistant", "text": text})
        self.emit("sent", text)
        if not proactive:
            low, high = self.config["proactive_after_seconds"]
            self.next_proactive = self.last_send + random.uniform(low, high)
        self.save()

    def is_owner(self):
        return self.mode == "private" and self.chat == self.config.get("owner", "")

    def take_relay(self):
        relay, self.relay = self.relay, None
        return relay

    def say(self, text, why="主人让我说的"):
        """Send `text` in this chat without asking the model (owner relay).

        Same rules as any send: only while running, never retried, pause on doubt.
        """
        text = " ".join(str(text).split())
        if not text or len(text) > self.config["max_reply_chars"]:
            raise ValueError("要转告的话为空或太长")
        if not self.enabled.is_set() or self.stop.is_set():
            raise RuntimeError("已暂停，没有转告")
        epoch = self.enabled.epoch

        def cancel():
            return self.stop.is_set() or not self.enabled.is_set() or self.enabled.epoch != epoch

        self.recent_sends.append((time.monotonic(), compact(text)))
        try:
            self.adapter.send(text, cancel=cancel)
        except Exception:
            self.enabled.clear()
            self.emit("error", "转告发送未确认，已暂停。请检查微信，避免重复。")
            raise
        self.sent(text, proactive=True)
        self.status(f"已在「{self.chat}」说了（{why}）")

    def digest(self, rows=10):
        """Recent lines of this chat, for the owner's private chat."""
        lines = []
        for r in self.history[-rows:]:
            who = "你" if r["role"] == "assistant" else (r.get("sender") or "群友")
            lines.append(f"  {who}：{r['text']}")
        return f"【群「{self.chat}」最近的聊天】\n" + "\n".join(lines) if lines else ""

    def sticker_label(self, sid):
        return f"[表情包：{dict(self.stickers.catalog(1000)).get(sid, sid)}]"

    def model_failed(self, exc):
        self.error_count += 1
        self.retry_after = time.monotonic() + min(60, 5 * 2 ** min(self.error_count, 4))
        if self.error_count >= 3:
            self.enabled.clear()
            self.emit("error", f"模型连续 {self.error_count} 次出错，已暂停：{exc}")

    def resume(self, rows):
        """After any pause, messages from the pause go to context only, never answered late."""
        self.active_epoch = self.enabled.epoch
        self.ingest(rows)
        self.pending.clear()
        self.proactive_used = True
        self.next_proactive = NEVER
        self.error_count = 0
        self.retry_after = 0.0
        self.status("已继续 · 暂停期间的消息不补回复，等新消息")

    def speakers(self):
        """Who the current conversation is with, most recent first."""
        if self.mode == "private":
            return [self.chat]
        rows = [getattr(m, "sender", "") for m in reversed(self.pending)]
        rows += [r.get("sender", "") for r in reversed(self.history[-20:])]
        return [n for n in dict.fromkeys(rows) if n and not n.startswith("群友")]

    def learn(self, now, force=False):
        """Update long-term memory from recent chat while the chat is quiet.

        Runs between conversations (never with a reply pending) so it does not
        delay answers; a failure is only retried later and never pauses the bot.
        """
        if not self.config["memory_enabled"] or self.pending or not self.unlearned:
            return False
        due = (self.unlearned >= self.config["memory_every"] and now - self.last_incoming >= 30) \
            or self.unlearned >= 40
        if not (force or due) or now - self.learn_failed_at < 120:
            return False
        rows = self.history[-min(40, max(self.unlearned + 6, 12)):]
        names = [self.chat] if self.mode == "private" else [r["sender"] for r in rows if r.get("sender")]
        self.status("整理记忆中…")
        try:
            update = self.brain.learn(rows, chat=self.chat, mode=self.mode, memory=self.memory)
        except Exception as exc:
            self.learn_failed_at = now
            self.status(f"这次没整理好记忆，稍后再试：{str(exc)[:60]}")
            return False
        changed = self.memory.apply(update, names)
        self.unlearned = 0
        self.status(f"记住了 {changed} 条新东西" if changed else "这段聊天没什么要记的")
        return True

    def may_proactive(self, now):
        return (self.mode == "private" and self.config["proactive"] and not self.proactive_used
                and not self.muted and not self.quiet() and now >= self.next_proactive
                and now - self.last_incoming <= self.config["proactive_window_seconds"])

    def step(self):
        if not self.enabled.is_set() or self.stop.is_set():
            self.status("已暂停")
            return
        rows = self.adapter.read()
        if not self.initialized:
            for row in rows:
                self.mark(row)
            self.tail = [signature(r) for r in rows][-40:]
            self.initialized = True
            self.active_epoch = self.enabled.epoch
            if self.reply_latest and rows and rows[-1].role == "user" and self.accept(rows[-1]):
                self.save()
            self.status(f"监听中 · {self.chat}")
            return
        if self.enabled.epoch != self.active_epoch:
            self.resume(rows)
            return
        self.ingest(rows)
        now = time.monotonic()
        if self.expired(now) or now < self.retry_after:
            return
        has_pending = bool(self.pending)
        if has_pending and now - self.last_incoming < self.config["batch_seconds"]:
            self.status("等这一轮说完…")
            return
        proactive = not has_pending
        if proactive and not self.may_proactive(now):
            if not self.learn(now):
                self.status(f"监听中 · {self.chat}")
            return
        if self.muted:
            self.pending.clear()
            self.proactive_used = True
            self.status("本轮保持安静 · 新消息可重新开始")
            return
        # Her persona name, plus the bot account's WeChat nickname that @ uses.
        names = [self.name] + [n for n in self.config.get("aliases", []) if n]
        mentioned = any(mentions(m.text, n) for m in self.pending for n in names)
        continuing = False
        if self.mode == "group" and not mentioned:
            # Whoever she just answered may keep talking to her (attention), but the
            # model still judges whether it is addressed to her. Otherwise she cools
            # down between interjections, tires after a run, and only sometimes even
            # considers chiming in (cheap first filter, as in Group Chat Plus).
            partner, since = self.partner
            if any(getattr(m, "sender", "") == partner for m in self.pending) and now - since < 120:
                continuing = True
            elif now - self.last_send < self.config["group_gap_seconds"]:
                self.pending.clear()
                self.status("群里刚说过话，这轮不插嘴")
                return
            elif sum(now - t < 300 for t in self.sent_times) >= 4:
                self.pending.clear()
                self.status("这会儿说得够多了，先听大家聊")
                return
            elif random.random() >= self.config["group_chime_in"]:
                self.pending.clear()
                self.status("没叫她，这轮先听着")
                return
        replying_to = getattr(self.pending[-1], "sender", "") if self.pending else ""
        if not self.allowed_to_send():
            self.status("控制发言频率中")
            return
        self.status("想一句自然的接话…" if proactive else f"{self.name}正在想怎么接话…")
        epoch = self.enabled.epoch
        pending_keys = [m.key for m in self.pending]
        try:
            result = self.brain.decide(
                self.history, proactive, chat=self.chat, mode=self.mode, mentioned=mentioned,
                continuing=continuing,
                memory=self.memory.prompt_block(self.speakers()) if self.config["memory_enabled"] else "",
                **({"relay_targets": self.relay_targets(), "context": self.context()} if self.is_owner() else {}))
            speak, text, sticker = self.parse(result)
            if speak and not proactive and not mentioned and isinstance(result, dict) \
                    and result.get("to_me") is False:
                speak = False  # Read the room: not talking to her, so she stays out of it.
        except Exception as exc:
            self.model_failed(exc)
            raise
        self.error_count = 0

        def cancel():
            return self.stop.is_set() or not self.enabled.is_set() or self.enabled.epoch != epoch

        if cancel():
            self.status("已暂停 · 本次草稿作废")
            return
        # A reply based on stale history must never be sent.
        if self.ingest(self.adapter.read()):
            self.status("收到新消息，重新想一句")
            return
        if pending_keys != [m.key for m in self.pending] or self.expired(time.monotonic()):
            return
        self.pending.clear()
        if self.is_owner() and isinstance(result, dict) and result.get("relay"):
            # Carried out by the scheduler once this chat is done: it has to switch chats.
            self.relay = result["relay"]
        if proactive:
            self.proactive_used = True  # Also stop repeated polling if the LLM chose silence.
        if not speak:
            self.proactive_used = True
            self.status("这轮先不接话")
            return
        if not self.allowed_to_send():
            self.status("控制发言频率中 · 本次草稿作废")
            return
        try:
            if text:
                # Remember before sending: an uncertain send may still land in the chat.
                self.recent_sends.append((time.monotonic(), compact(text)))
                self.adapter.send(text, cancel=cancel)
                self.sent(text, proactive)
            if sticker:
                sid, path = sticker
                self.recent_stickers.append((time.monotonic(), sticker_hash(path)))
                self.adapter.send_image(path, cancel=cancel)
                self.sent(self.sticker_label(sid), proactive)
        except Exception:
            # Any uncertain UI send requires human inspection. Never retry.
            cancelled = cancel()
            self.enabled.clear()
            self.proactive_used = True
            self.next_proactive = NEVER
            self.emit("error", "已暂停，草稿未发送；请检查微信输入框是否留有文字。" if cancelled
                      else "发送未确认，已暂停。请检查微信输入框和消息，避免重复。")
            raise
        self.status("已发出 · 没人接话时最多再主动接一次" if self.mode == "private" else "已发出")
        if replying_to:
            self.partner = (replying_to, time.monotonic())
        try:
            # Messages that arrived while typing are queued now, so they are not
            # mistaken for old rows once our bubble becomes the newest seen row.
            self.ingest(self.adapter.read())
        except Exception:
            pass
