"""One chat's reply loop. Persistence and send control are independent of the LLM."""
from __future__ import annotations
from social_policy import chat_key

import difflib
import json
import random
import re
import time
from collections import deque
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from brain import EMOJI_REPEAT_REPLIES, mentions, recent_wechat_emojis, wants_silence
from memory import Memory
from stickers import PLACEHOLDER_WORDS, WECHAT_EMOJI

# A short reply ending on one of these is a cut-off thought ("你发了个"), not a message.
DANGLING = tuple("个把给跟与被让对向从和在将的了")


def dangling(text):
    core = text.rstrip(" ~～…。！？!?")
    return len(core) <= 8 and core.endswith(DANGLING) and not core.endswith(("好的", "是的", "真的", "对的", "行了", "好了", "算了", "走了", "来了", "完了", "懂了", "对了", "得了"))
from personas import safe_name
from senders import avatar_hash, distance
from settings import DATA, ROOT
from routing import preview_text, recipients

NEVER = float("inf")
NOTICE_CHARS = 500  # agent receipts and reports may be longer than a chat reply, never multi-line
IMAGE_CACHE = []  # (fingerprint, description), shared by every chat in this process
SMALL_EMOJI = re.compile(r"\[([^\[\]]{1,6})\]")
SMALL_EMOJI_NAMES = frozenset(WECHAT_EMOJI)
ODD_PRIVATE_REPLIES = ("有病？[白眼]", "你发的什么鬼？[白眼]", "？？？你干嘛")
# Last words when a private chat must be answered and the model still gave nothing usable.
MUST_REPLIES = ("嗯？你说", "我在呢", "然后呢？", "哈哈你继续")


def compact(text):
    return "".join(str(text).split())


def limit_small_emojis(text, blocked=()):
    """Keep at most one fresh WeChat small emoji; remove recent/repeated ones."""
    blocked, kept = set(blocked), False

    def replace(match):
        nonlocal kept
        name = match.group(1)
        if name not in SMALL_EMOJI_NAMES:
            return match.group(0)
        if name in blocked or kept:
            return ""
        kept = True
        return match.group(0)

    return " ".join(SMALL_EMOJI.sub(replace, str(text)).split())


def signature(row):
    return row.role, compact(row.text)


def sticker_hash(path):
    from stickers import first_frame, trim
    return avatar_hash(trim(first_frame(path)))


def load_history(path: Path, chat: str):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, list) else []
    except (FileNotFoundError, ValueError):
        return []


class Runner:
    def __init__(self, settings, adapter, enabled, stop, emit, brain, chat,
                 history_dir: Path = DATA / "history", reply_latest=False, stickers=None,
                 memory_dir: Path = DATA / "memory", outbound_mode=None):
        self.stickers = stickers
        self.memory = Memory(Path(memory_dir) / f"{chat_key(chat, settings.chat_mode(chat) or outbound_mode)}.json")
        self.learn_failed_at = -NEVER
        self.recent_stickers = deque()  # (monotonic, image hash) of stickers we pasted
        self.partner = ("", 0.0)  # group member last answered, and when
        # Owner tools, wired up by the scheduler: which chats she may speak in on the
        # owner's behalf, and a digest of those chats for the owner's private chat.
        self.relay_targets = lambda: []
        self.context = lambda: ""
        self.relay = None
        self.relay_queue = deque()
        self.owner_relay_command = lambda text: False
        self.owner_go = lambda chat, text: None
        self.owner_do = lambda task: None
        self.should_yield = lambda: False
        # Owner commands for local agents, wired up by the scheduler: (text, act) ->
        # None when it is ordinary chat, else {"log": text without the passphrase,
        # "reply": receipt or None}. Receipts and reports wait in `notices`.
        self.agent_command = lambda text, act=True: None
        # The owner's low-risk commands inside a group ("@小千 拉黑 X"), same contract.
        self.group_command = lambda text, act=True, sender="": None
        self.notices = deque()
        # Opening a topic on her own (a group bubble, a message to the owner), set by the
        # scheduler for one step: {"idle_minutes", "unanswered"}. The hooks report back to it.
        self.opening = None
        self.resume_once = False  # the owner pressed 重新接话 for this chat
        self.insist = False  # 重新接话 took in missed lines: answer them whatever the model thinks
        self.opening_active = False
        self.on_heard = lambda: None
        self.on_owner_message = lambda text: None
        self.owner_asleep = lambda: False
        self.on_spoke = lambda opening: None
        self.on_declined = lambda: None
        # The owner asked for real work (a drawing, a Codex task): (kind, task) -> receipt or None.
        self.hand_off = lambda kind, task: None
        self.replaying = False  # set by ingest(replay=True): pause-time messages are never acted on
        self.mode = settings.chat_mode(chat) or outbound_mode
        if self.mode not in ("private", "group"):
            raise ValueError(f"「{chat}」不在允许的聊天列表里")
        self.config, self.adapter, self.brain, self.chat = settings, adapter, brain, chat
        self.enabled, self.stop, self.emit = enabled, stop, emit
        self.reply_latest = reply_latest
        self.seen, self.seen_order = set(), deque()
        self.history_path = Path(history_dir) / f"{chat_key(chat, settings.chat_mode(chat) or outbound_mode)}.json"
        self.history = load_history(self.history_path, chat)
        # Where memory got to, kept next to the history so a restart does not forget it.
        self.learned_path = self.history_path.with_suffix(".learned")
        self.unlearned, self.learned_at = self.load_learned()
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

    def load_learned(self):
        """(user messages since the last memory update, when that was in wall-clock seconds)."""
        try:
            mark = json.loads(self.learned_path.read_text(encoding="utf-8"))
            last, at = tuple(mark["last"]), float(mark["at"])
        except (OSError, ValueError, KeyError, TypeError):
            last, at = None, 0.0
        start = 0
        for i in range(len(self.history) - 1, -1, -1):
            if last and (self.history[i]["role"], compact(self.history[i]["text"])) == last:
                start = i + 1
                break
        return sum(row["role"] == "user" for row in self.history[start:]), at

    def mark_learned(self, rows):
        self.learned_at = time.time()
        mark = {"last": [rows[-1]["role"], compact(rows[-1]["text"])], "at": self.learned_at}
        try:
            self.learned_path.parent.mkdir(parents=True, exist_ok=True)
            self.learned_path.write_text(json.dumps(mark, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

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
        from stickers import trim
        # What she sent was fingerprinted trimmed (sticker_hash); a bubble read back carries the
        # chat background around it, so compare it trimmed too, and as read for good measure.
        now, marks = time.monotonic(), (avatar_hash(trim(image)), avatar_hash(image))
        while self.recent_stickers and now - self.recent_stickers[0][0] > 120:
            self.recent_stickers.popleft()
        return any(distance(mark, sent) <= 10 for _, sent in self.recent_stickers for mark in marks)

    def describe(self, row):
        generic = '[收到一张图片]'
        if (self.replaying or not self.config.get('describe_images', True)
                or self.config.chat_mode(self.chat) != self.mode
                or any(getattr(m, '_social_image_used', False) for m in self.pending)):
            return generic
        row._social_image_used = True
        try:
            desc = self.brain.describe_image(row.image)
        except Exception:
            self.emit('error', 'SOCIAL_IMAGE_FAILED')
            return generic
        # Only explicitly labelled stickers are collected; photos never go to inbox.
        label = str(getattr(row, 'text', ''))
        if self.stickers and self.config.get('collect_stickers', True) and ('表情' in label or 'sticker' in label.lower()):
            try:
                if self.stickers.inbox.add(row.image, desc, self.chat, getattr(row,'sender',''), self.stickers.hashes()):
                    self.emit('inbox', 'SOCIAL_STICKER_INBOX_ADDED')
            except Exception:
                self.emit('error', 'SOCIAL_STICKER_INBOX_FAILED')
        return '[图片描述：' + desc + ']'

    def accept(self, row):
        if row.role != "user" or self.own_echo(row.text):
            return False
        if self.is_owner() and not self.replaying and getattr(row, 'voice', None) is None:
            self.on_owner_message(row.text)
        if self.is_owner() and not self.replaying and getattr(row, 'image', None) is None and getattr(row, 'voice', None) is None and self.owner_relay_command(row.text):
            self.history.append({"role": "user", "text": row.text})
            self.emit("received", row.text)
            return True
        if getattr(row, "voice", None) is not None:
            try:
                row.text = self.adapter.transcribe(row)
            except Exception as exc:
                row.text = "[语音：微信没能转成文字]"
                self.status(f"语音没转出来：{str(exc)[:60]}")
            if "没能转成文字" in row.text:
                row.odd = True
            if self.is_owner() and not self.replaying:
                self.on_owner_message(row.text)
        if self.is_owner() and getattr(row, "image", None) is None:
            handled = self.agent_command(row.text, act=not self.replaying)
            if handled:
                # Never a model turn; only the passphrase-free form reaches history and logs.
                self.history.append({"role": "user", "text": handled["log"]})
                self.emit("received", handled["log"])
                if handled.get("reply"):
                    self.notices.append(handled["reply"])
                return True
        sender = getattr(row, "sender", "")
        if self.blocked(sender):
            return False  # the owner said not to take in this person's messages at all
        self.on_heard()
        # A group member addressing her, or a private contact other than the owner: drawing (within
        # the console's rights) and, for the owner in a group, blocking and memory.
        member = sender if self.mode == "group" else ("" if self.is_owner() else self.chat)
        if member and getattr(row, "image", None) is None:
            handled = self.group_command(row.text, act=not self.replaying, sender=member)
            if handled:
                self.history.append({"role": "user", "text": handled["log"],
                                     **({"sender": sender} if self.mode == "group" else {})})
                self.emit("received", f"{sender}：{handled['log']}" if self.mode == "group" else handled["log"])
                if handled.get("reply"):
                    self.notices.append(handled["reply"])
                return True
        if getattr(row, "image", None) is not None:
            if self.own_sticker(row.image):
                return False
            row.text = self.describe(row)
        entry = {"role": "user", "text": row.text}
        if self.mode == "group" and sender:
            entry["sender"] = sender
        self.history.append(entry)
        self.unlearned += 1
        self.pending.append(row)
        self.last_incoming = time.monotonic()
        self.proactive_used = False
        self.next_proactive = NEVER
        self.muted = wants_silence(row.text, self.name)
        self.emit("received", 'SOCIAL_IMAGE_RECEIVED' if getattr(row, 'image', None) is not None else
                  (f"{sender}：{row.text}" if entry.get("sender") else row.text))
        return True

    def catch_up(self, rows):
        """重新接话: the other side's latest lines on screen that never reached her (her place was
        lost, or they came during a pause) are taken in now, drawing and commands included."""
        known = {compact(r["text"]) for r in self.history[-20:] if r["role"] == "user"}
        block = []
        for row in reversed(rows):
            if row.role == "user":
                block.append(row)
            elif block:
                break
        changed = False
        for row in reversed(block[:5]):
            # Pictures and voice read back only as a bare label; never take those in twice.
            if not row.text.startswith("[") and compact(row.text) not in known:
                changed = self.accept(row) or changed
        return changed

    def ingest(self, rows, replay=False):
        """replay: rows from a pause, kept as context; agent commands in them are not run."""
        changed = False
        self.replaying = replay
        try:
            for row in self.fresh(rows):
                changed = self.accept(row) or changed
        finally:
            self.replaying = False
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
        if self.stickers:
            # The model sometimes echoes the sticker's number into the text ("…[奸笑] 049").
            ids = {sid for sid, _ in self.stickers.catalog(1000)}
            ids |= {sid.split("_")[0] for sid in ids}
            match = re.search(r"\s*\b([0-9A-Za-z_]+)\s*$", text)
            if match and match.group(1) in ids:
                text = text[:match.start()]
        blocked = recent_wechat_emojis(self.history, EMOJI_REPEAT_REPLIES)
        text = limit_small_emojis(text, blocked)
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

    def odd_private_reply(self):
        """A private unknown message never disappears just because the model declined it."""
        return self.fallback_reply(ODD_PRIVATE_REPLIES)

    def fallback_reply(self, lines):
        """One of `lines` that is not a repeat of what she just said."""
        key = str(self.pending[-1].key) if self.pending else str(len(self.history))
        offset = sum(ord(ch) for ch in key) % len(lines)
        for i in range(len(lines)):
            candidate = lines[(offset + i) % len(lines)]
            parsed = self.parse({"speak": True, "text": candidate, "sticker": ""})
            if parsed[0]:
                return parsed
        return True, "你发的啥？", None

    def sent(self, text, proactive, notice=False):
        """Bookkeeping for one confirmed message, saved before anything else can fail.

        `notice`: not her own talk (a receipt, a report, words relayed for the owner, a drawing)."""
        self.last_send = time.monotonic()
        self.sent_times.append(self.last_send)
        self.history.append({"role": "assistant", "text": text, **({"notice": True} if notice else {})})
        self.emit("sent", text)
        self.on_spoke(self.opening_active)  # an opening counts once, even with a sticker after it
        self.opening_active = False
        if not proactive:
            low, high = self.config["proactive_after_seconds"]
            self.next_proactive = self.last_send + random.uniform(low, high)
        self.save()

    def blocked(self, sender):
        """Blocked by the owner: the sender in a group, or the other side of a private chat.

        Entries are {"name", "chat": where (None: everywhere), "until": wall-clock end (None: for
        good)}; a plain name from older settings blocks everywhere for good."""
        who = sender if self.mode == "group" else self.chat
        if not who or who == self.config.get("owner", ""):
            return False
        now = time.time()
        for entry in self.config.get("blocked") or []:
            if isinstance(entry, str):
                entry = {"name": entry}
            if entry.get("name") == who and entry.get("chat") in (None, "", self.chat) \
                    and (entry.get("until") is None or entry["until"] > now):
                return True
        return False

    def is_owner(self):
        return self.mode == "private" and self.chat == self.config.get("owner", "")

    def owner_profile(self):
        """Everything she has learned in the owner's chat, plus the library's notes about him."""
        if not self.is_owner():
            return ''
        parts = []
        for name, person in (getattr(self.memory, 'people', {}) or {}).items():
            if not isinstance(person, dict):
                continue
            facts = '；'.join(str(f) for f in (person.get('facts') or [])[:30])
            impression = person.get('impression') or ''
            parts.append(f'{name}：{facts}' + (f'（你对他的印象：{impression}）' if impression else ''))
        jokes = [f'{term}＝{(info or {}).get("meaning", "") if isinstance(info, dict) else info}'
                 for term, info in list((getattr(self.memory, 'jargon', {}) or {}).items())[:40]]
        if jokes:
            parts.append('你们之间的梗和说法：' + '；'.join(jokes))
        style = getattr(self.memory, 'style', '') or ''
        if style:
            parts.append('你们平时的聊天风格：' + style)
        library = self.history_path.parent.parent / 'owner_profile.md'
        try:
            if library.is_file():
                parts.append('图书馆里关于主人的记录：\n' + library.read_text(encoding='utf-8')[:3000])
        except OSError:
            pass
        return '\n'.join(parts)[:6000]

    def take_relay(self):
        if self.relay_queue:
            return self.relay_queue.popleft()
        relay, self.relay = self.relay, None
        return relay

    def prime(self, rows, baseline=None, unread=0, replay=False):
        """Establish a first-visit anchor and queue every identifiable new row.

        replay: the rows were waiting from before “start”; agent commands in them are not run.
        """
        if self.initialized and self.active_epoch != self.enabled.epoch:
            self.initialized = False
            self.pending.clear()
        if self.initialized:
            self.ingest(rows, replay=replay)
            return
        start = len(rows)
        replay_latest = False
        if unread:
            users = [i for i, row in enumerate(rows) if row.role == "user"]
            if len(users) < unread:
                raise RuntimeError("未读消息超出当前可见范围，请打开聊天加载完整消息后再继续")
            start = users[-unread]
        elif baseline:
            prior = compact(preview_text(baseline, self.chat))
            matches = [i for i, row in enumerate(rows) if compact(row.text) == prior
                       or (getattr(row, "sender", "") and prior in (
                           compact(row.sender + "：" + row.text), compact(row.sender + ":" + row.text)))]
            # A new contact's chat list shows "你已添加了…" (a system notice, not a row) as its last line.
            notices = [at for at, text in getattr(self.adapter, "system_marks", []) if compact(text) == prior]
            if matches:
                start = matches[-1] + 1
            elif notices:
                start = notices[-1]
            elif self.mode == "private" and any(row.role == "assistant" for row in rows):
                # Her place scrolled away (pictures sent from this account meanwhile push it up), but a
                # bubble from this side is on screen: what came after it is unanswered, and a private
                # chat is always answered.
                start = max(i for i, row in enumerate(rows) if row.role == "assistant") + 1
            else:
                raise RuntimeError("找不到上次消息的位置，保留待办；请打开聊天加载完整记录后再继续")
        elif self.reply_latest and rows and rows[-1].role == "user":
            start = len(rows) - 1
            replay_latest = True
        for row in rows:
            self.mark(row)
        self.tail = [signature(row) for row in rows][-40:]
        self.initialized = True
        self.active_epoch = self.enabled.epoch
        self.replaying = replay or replay_latest
        try:
            changed = [self.accept(row) for row in rows[start:]]
        finally:
            self.replaying = False
        if any(changed):
            self.save()

    def say(self, text, why="主人让我说的", limit=None, at=()):
        return False

    def show(self, path, label="图片"):
        return False

    def deliver(self):
        return False

    def digest(self, rows=10):
        return ""

    def sticker_label(self, sid):
        return f"[表情包：{dict(self.stickers.catalog(1000)).get(sid, sid)}]"

    def model_failed(self, exc):
        self.error_count += 1
        self.retry_after = time.monotonic()+60
        self.emit('error', 'social_model_failed')

    def resume(self, rows):
        """After any pause, messages from the pause go to context only, never answered late."""
        self.active_epoch = self.enabled.epoch
        self.ingest(rows, replay=True)
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

    def learn(self, now, force=False, timed=False):
        """Update long-term memory from recent chat while the chat is quiet.

        Runs between conversations (never with a reply pending) so it does not
        delay answers; a failure is only retried later and never pauses the bot.
        """
        self.learn_note = "记忆功能已关闭"
        if not self.config["memory_enabled"]:
            return False
        if timed and not self.unlearned:
            self.learn_note = "这段聊天没什么要记的"
            return False
        if not force and not timed:
            if self.pending or not self.unlearned:
                return False
            due = (self.unlearned >= self.config["memory_every"] and now - self.last_incoming >= 30) \
                or self.unlearned >= 40
            if not due or now - self.learn_failed_at < 120:
                return False
        # Forced (by the owner): the recent chat even after a restart, which resets `unlearned`.
        rows = self.history[-min(40, max(self.unlearned + 6, 30 if force else 12)):]
        if not rows:
            self.learn_note = "这里还没有聊天记录可整理"
            return False
        names = [self.chat] if self.mode == "private" else [r["sender"] for r in rows if r.get("sender")]
        self.status("整理记忆中…")
        try:
            update = self.brain.learn(rows, chat=self.chat, mode=self.mode, memory=self.memory)
        except Exception as exc:
            self.learn_failed_at = now
            self.learn_note = f"这次没整理好记忆：{str(exc)[:60]}"
            self.status(self.learn_note)
            return False
        changed = self.memory.apply(update, names)
        self.unlearned = 0
        self.mark_learned(rows)
        self.learn_note = f"记住了 {changed} 条新东西" if changed else "这段聊天没什么要记的"
        self.status(self.learn_note)
        return True

    def may_proactive(self, now):
        # Unprompted follow-ups go to the owner only; friends are never messaged out of the blue.
        return (self.mode == "private" and self.is_owner() and not self.owner_asleep() and self.config["proactive"] and not self.proactive_used
                and not self.muted and not self.quiet() and now >= self.next_proactive
                and now - self.last_incoming <= self.config["proactive_window_seconds"])

    def queue_balance_alert(self):
        """Reserve one owner notice per local day, including across restarts."""
        if not self.is_owner():
            return
        try:
            from social_policy import process_lock, zone, _LOCK
            snapshot = self.brain.balance()
            if not snapshot or snapshot.get('currency') != 'CNY':
                return
            amount = Decimal(snapshot['total_balance'])
            threshold = Decimal(str(self.config.get('balance_alert', 10)))
            if not amount.is_finite() or not threshold.is_finite() or amount >= threshold:
                return
            today = datetime.now(zone(self.config.get('social_timezone', 'Asia/Shanghai'))).date().isoformat()
            path = DATA / 'balance-alert.json'
            with _LOCK, process_lock(path.with_suffix('.lock')):
                state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                if not isinstance(state, dict) or state.get('date') == today:
                    return
                temp = path.with_suffix('.tmp')
                temp.write_text(json.dumps({'date': today}), encoding='utf-8')
                temp.replace(path)
                self.notices.append(f"提醒一下，我的 DeepSeek 余额只剩 ¥{amount} 了。")
        except Exception:
            self.emit('error', 'SOCIAL_BALANCE_ALERT_FAILED')

    def step(self):
        from social_policy import Budget, unsafe
        if not self.enabled.is_set() or self.stop.is_set():
            return
        rows = self.adapter.read()
        if not self.initialized:
            for row in rows:
                self.mark(row)
            self.tail = [signature(r) for r in rows][-40:]
            self.initialized = True
            self.active_epoch = self.enabled.epoch
            if self.reply_latest and rows and rows[-1].role == 'user':
                self.replaying = True
                try:
                    self.accept(rows[-1]); self.save()
                finally:
                    self.replaying = False
            return
        if self.enabled.epoch != self.active_epoch:
            self.resume(rows); return
        self.ingest(rows)
        if self.resume_once:
            self.resume_once = False
            self.replaying = True
            try:
                changed = self.catch_up(rows)
            finally:
                self.replaying = False
            if changed:
                self.save()
            if not self.pending and any(r.get('role') == 'user' for r in self.history):
                self.opening = {'resume': True}
        if self.is_owner() and not self.owner_asleep():
            self.queue_balance_alert()
        if self.notices and self.is_owner() and not self.owner_asleep():
            notice = self.notices.popleft()  # never retry an uncertain send
            epoch = self.enabled.epoch
            cancel = lambda: self.stop.is_set() or not self.enabled.is_set() or self.enabled.epoch != epoch
            try:
                if cancel(): return
                title, private = self.adapter.current()
                if title != self.chat or not private: raise RuntimeError('social_owner_notice_target_invalid')
                self.adapter.validate()
                self.adapter.send(' '.join(str(notice).split()), cancel=cancel, max_chars=400)
            except Exception:
                self.enabled.clear()
                self.emit('error', 'SOCIAL_OWNER_NOTICE_FAILED')
            return
        now = time.monotonic()
        if self.expired(now) or now < self.retry_after or self.should_yield():
            return
        pending = list(self.pending)
        if pending and now-self.last_incoming < self.config['batch_seconds']:
            return
        names = [self.name]+list(self.config.get('aliases', []))
        mentioned = any(mentions(m.text,n) for m in pending for n in names if n)
        opening, self.opening = self.opening, None
        mandatory = bool(pending) and (self.mode == 'private' or mentioned)
        proactive = not mandatory
        if proactive and self.is_owner() and self.owner_asleep():
            return
        if self.muted and not mandatory:
            return
        if not pending and not opening and not self.may_proactive(now):
            self.learn(now)
            return
        continuing = self.mode == 'group' and bool(self.partner[0]) and now-self.partner[1] < 120 and any(getattr(m,'sender','') == self.partner[0] for m in pending)
        if pending and proactive and not continuing and (now-self.last_send < self.config['group_gap_seconds'] or random.random() >= self.config['group_chime_in']):
            self.pending.clear(); return
        if not self.allowed_to_send():
            return
        epoch = self.enabled.epoch
        keys = [m.key for m in pending]
        def cancel():
            return self.stop.is_set() or not self.enabled.is_set() or self.enabled.epoch != epoch
        try:
            if opening and not mandatory and self.is_owner() and not opening.get('resume'):
                if not self.brain.opening_check(self.history, opening):
                    self.on_declined()
                    return
                self.opening_active = True
            # Retrieval reads ONLY this runner's local Memory, selecting <=5 entries.
            facts = self.memory.social_retrieve(' '.join(m.text for m in pending)[-2000:]) if self.config['memory_enabled'] else []
            group_digest = self.context() if self.is_owner() else ''
            dispatched, self.work_dispatched = getattr(self, 'work_dispatched', False), False
            try:
                result = self.brain.decide(self.history, proactive, chat=self.chat, mode=self.mode, memory=facts, mentioned=mentioned, opening=opening, group_digest=group_digest, profile=self.owner_profile())
            except Exception as exc:
                # Nothing was sent: skip this round and try again shortly instead of pausing everything.
                self.model_failed(exc)
                return
            self.proactive_used = True
            self.pending.clear()
            if not isinstance(result,dict) or not result.get('speak') or unsafe(result.get('text','')):
                if self.opening_active:
                    self.opening_active = False
                    self.on_declined()
                return
            if self.mode == 'group' and pending and not mentioned and result.get('to_me') is False:
                return
            text = result.get('text','')
            sid = result.get('sticker','')
            # Catalog resolution never accepts a model path. Resolve again at send time.
            path = self.stickers.path(sid) if self.stickers and sid else None
            # Never say the same thing twice: a near-copy of a recent line, or the same sticker
            # again so soon, reads as a bot (and an opening must not rerun the last exchange).
            recent = [compact(r.get('text', '')) for r in self.history[-12:] if r.get('role') == 'assistant']
            def repeats(old, new=compact(text)):
                if min(len(old), len(new)) < 10:
                    return old == new
                return difflib.SequenceMatcher(None, new, old).ratio() >= 0.6
            if text:
                # Judge each bubble on its own: drop only the lines she already said.
                text = '\n'.join(l for l in text.split('\n')
                                 if l.strip() and not any(repeats(old, compact(l)) for old in recent))
            # Stickers are her own call: no repeat or rate rules in code.
            if not text and not path:
                return
            if cancel() or self.ingest(self.adapter.read()) or self.should_yield():
                return
            budget = Budget(self.history_path.parent/'social-budget.json', self.config.get('social_timezone','Asia/Shanghai'))
            # The owner wants her to speak up whenever she likes: no night silence or hourly cap
            # for the owner's private chat (the opening schedule still paces her).
            if not budget.reserve(self.chat, proactive and not self.is_owner()):
                return
            # One worker owns adapter activation. Do not accept a destination from Brain.
            if text:
                # Several bubbles, one at a time, with a typing-like pause between them.
                for n, line in enumerate(l for l in text.split('\n') if l.strip()):
                    if n:
                        time.sleep(min(3.0, 0.8 + 0.08 * len(line)))
                        if cancel():
                            return
                    self.recent_sends.append((time.monotonic(),compact(line)))
                    self.adapter.send(line,cancel=cancel)
                    self.sent(line,proactive)
                if self.is_owner() and result.get('go'):
                    self.owner_go(result['go']['chat'], result['go']['text'])  # she decided to go: no approval
                if self.is_owner() and result.get('do') and not dispatched:
                    self.owner_do(result['do'])  # real work on the server, reported back when done
                if self.mode == 'group' and pending and getattr(pending[-1], 'sender', ''):
                    self.partner = (pending[-1].sender, time.monotonic())
            if path:
                checked = self.stickers.path(sid)
                if checked != path:
                    raise RuntimeError('social_sticker_changed')
                self.adapter.send_image(checked,cancel=cancel)
                self.sent(self.sticker_label(sid),proactive)
        except Exception:
            self.model_failed(None)
            # Uncertain UI sends must not be retried. Owner can inspect then resume.
            self.enabled.clear()
        finally:
            if self.opening_active:
                self.opening_active = False
                self.on_declined()
