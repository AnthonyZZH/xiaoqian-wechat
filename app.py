"""Control window: pick the chat, persona and model; start/pause; close to stop completely."""
import argparse
import ctypes
import json
import queue
import re
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))

from agents import DEFAULT_ROUTES, AgentJobs, default_configs, parse_command
from brain import Brain
from lmstudio import LMStudio
from personas import TEMPLATE, Persona, PersonaStore
from settings import DATA, Settings
from stickers import StickerLibrary
from routing import ChatQueue, preview_text

FONT = "Microsoft YaHei UI"
AGENT_NAMES = {"claude": "Claude", "codex": "Codex", "deepseek": "DeepSeek", "draw": "画图"}
AGENT_ALIASES = {key[1:].casefold(): agent for key, agent in DEFAULT_ROUTES.items()}  # "停掉codex"
JOB_STATES = {"running": "进行中", "done": "做完了", "failed": "失败了", "timeout": "超时被停掉了",
              "stopped": "已经停掉了"}
# "给示例联系人私聊发：晚安" from the owner: the exact text, to exactly that chat, no model involved.
OWNER_SEND = re.compile(r"(?:你)?(?:去|帮我|再)?(?:给|跟|向|对)\s*(?P<chat>[^\s:：,，]{1,40}?)\s*(?:私聊)?\s*"
                        r"(?:发消息|发个消息|发一句|发|说)\s*(?:说)?\s*[:：]\s*(?P<text>\S.*)")
# "在测试群里艾特小王说：…" / "去测试群说：…": a group (short names allowed), optionally @someone.
OWNER_SAY_IN = re.compile(r"(?:你)?(?:帮我|再)?(?:在|到|去|往)\s*(?P<chat>[^\s:：,，]{1,40}?)\s*(?:里面|里|中)?\s*"
                          r"(?:(?:艾特|爱特|@|＠)\s*(?P<at>[^\s:：,，]{1,30}?)\s*[,，]?\s*)?"
                          r"(?:发消息|发个消息|发一句|发|说)\s*(?:说)?\s*[:：]\s*(?P<text>\S.*)")
NOT_A_CHAT = {"我", "你", "他", "她", "它", "大家", "我们", "你们", "他们"}
# The owner's low-risk commands, also accepted in a group when they @ her.
UNBLOCK = re.compile(r"(?:解除拉黑|取消拉黑|解除屏蔽|取消屏蔽|恢复接收|可以接收|可以理|放出)\s*(?P<name>.+?)\s*(?:的消息|的话|了)?[。.!！]*")
BLOCK = re.compile(r"(?:(?:不允许|不准|不许|不要|别)再?(?:接收|接受|理|回复|回|搭理|听)|拉黑|屏蔽)\s*"
                   r"(?P<name>.+?)\s*(?:的消息|的话|发的消息)?[。.!！]*")
BLOCKLIST = re.compile(r"(?:看看|查看)?(?:黑名单|拉黑名单|屏蔽名单)[?？]*")
# "画一只橘猫" / "帮我画：…": a picture by Codex's image tool, sent back where it was asked for.
DRAW = re.compile(r"(?P<ask>请|你|帮我|给我|请你|你帮我|你给我)?画(?:一下)?\s*(?P<colon>[:：])?\s*(?P<what>.+)")
LEARN_NOW = re.compile(r"(?:总结|整理|更新|保存|存)一?下?(?:你的|一下)?记忆[。.!！~～]*|记一下(?:刚才的?)?(?:聊天)?[。.!！]*")
# Worth asking the local model whether the owner wants something sent (free wording only).
SEND_HINT = re.compile(r"艾特|爱特|@|＠|转告|告诉|通知|招呼|问问|(?:给|跟|向|对|在|去|到).{0,20}?(?:发|说|讲|问)")


class Enabled:
    def __init__(self):
        self.event = threading.Event()
        self.epoch = 0

    def is_set(self):
        return self.event.is_set()

    def set(self):
        if self.event.is_set():
            return
        self.epoch += 1
        self.event.set()

    def clear(self):
        self.epoch += 1
        self.event.clear()


class Bot:
    """Owns WeChat and the runner on one worker thread; the window only sends commands."""

    def __init__(self, settings, personas, reply_latest, log_dir=DATA / "logs", data_dir=DATA):
        self.settings, self.personas, self.log_dir = settings, personas, Path(log_dir)
        self.data = Path(data_dir)  # tests point this at a temp folder, never at real chats
        self.stickers = StickerLibrary(self.data / "stickers")
        self.brain = Brain(settings, personas, self.stickers, log_path=self.log_dir / "model_calls.jsonl")
        self.lm = LMStudio(settings["lms_path"])
        self.enabled, self.stop = Enabled(), threading.Event()
        self.events, self.commands = queue.Queue(), queue.Queue()
        self.reply_latest = reply_latest
        self.log_lock = threading.Lock()
        self.runner = None
        self.runners = {}    # chat -> Runner, kept while switching so each chat keeps its place
        self.relays = []     # (chat, text) the owner asked her to say
        self.drawings = {}   # job id -> (chat, what): pictures being drawn and where they go
        self.pictures = []   # (chat, image path or None, what): finished, waiting to be sent
        self.gallery_lock = threading.Lock()  # images/index.json: the worker writes, the console reads
        self.previews = {}   # chat -> last seen session-list preview, to notice news elsewhere
        self.incoming = ChatQueue()
        self.route_retry = {}
        self.since = 0.0     # when the current chat was opened
        self.scanning = None  # background sticker-labelling thread
        # Agent jobs finish on their own threads and only drop into `finished`;
        # the worker turns them into one-line `reports` for the owner's private chat.
        self.finished = queue.Queue()
        self.reports = []
        self.report_retry_at = 0.0
        self.agents = AgentJobs(default_configs(), self.data / "agent_logs", self.finished.put)
        # When she may open a topic on her own (group bubbles, a message to the owner).
        from proactive import Initiative
        self.initiative = Initiative(self.data / "initiative.json", settings)
        self.initiative_check_at = 0.0

    def emit(self, kind, payload):
        self.events.put((kind, payload))
        if kind in ("sessions", "models", "stickers", "chats", "memory", "memory_edit_result", "inbox"):
            return
        record = {"time": datetime.now().astimezone().isoformat(), "kind": kind,
                  "chat": self.settings["chat"], "text": payload}
        with self.log_lock:
            logs = self.log_dir
            logs.mkdir(parents=True, exist_ok=True)
            with (logs / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            (logs / "status.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    def command(self, *args):
        self.commands.put(args)

    def make_runner(self):
        """(Re)create the current chat's runner from scratch: old messages are not answered."""
        chat = self.settings["chat"]
        self.runners.pop(chat, None)
        self.activate(chat, reply_latest=self.reply_latest)
        self.reply_latest = False

    def runner_for(self, chat, reply_latest=False, outbound_mode=None):
        from runner import Runner
        if chat not in self.runners:
            runner = Runner(self.settings, self.adapter, self.enabled, self.stop, self.emit,
                            self.brain, chat, reply_latest=reply_latest, stickers=self.stickers,
                            history_dir=self.data / "history", memory_dir=self.data / "memory",
                            outbound_mode=outbound_mode)
            groups = lambda: [c for c, i in self.settings["chats"].items() if i["mode"] == "group"]
            # Relays need the multi-chat scheduler to carry them out.
            runner.relay_targets = lambda: [c for c in self.settings["chats"] if c != self.settings["owner"]]
            runner.should_yield = lambda r=runner: self.higher_priority(r)
            runner.agent_command = self.agent_command
            runner.group_command = self.group_command
            runner.hand_off = self.hand_off
            runner.on_heard = lambda r=runner: self.initiative.heard(r.chat, r.mode)
            runner.on_spoke = lambda opening, r=runner: self.initiative.spoke(r.chat, r.mode, opening)
            runner.on_declined = lambda r=runner: self.initiative.declined(r.chat, r.mode)
            runner.context = lambda: "\n".join(
                r.digest() for c, r in self.runners.items() if c in groups() and r.digest())
            self.runners[chat] = runner
        return self.runners[chat]

    def activate(self, chat, reply_latest=False, outbound_mode=None):
        """Point the adapter and the active runner at `chat` (already open in WeChat)."""
        from personas import safe_name
        from senders import SenderBook
        mode = self.settings.chat_mode(chat) or outbound_mode
        info = self.settings["chats"].get(chat, {})
        book = SenderBook(self.data / "senders" / f"{safe_name(chat)}.json", info.get("roster")) \
            if mode == "group" else None
        self.adapter.set_target(chat, mode, book, self.settings.get("group_layout"))
        self.runner = self.runner_for(chat, reply_latest, outbound_mode)
        self.since = time.monotonic()
        if chat in self.settings["chats"] and self.settings["chat"] != chat:
            self.settings.update(chat=chat)
            self.emit("chats", None)

    def go(self, chat, reply_latest=False, outbound=False):
        previous_runner = self.runner
        if previous_runner and previous_runner.chat != chat and previous_runner.pending:
            previous_runner.suspended_at = getattr(previous_runner, "suspended_at", None) or time.monotonic()
        self.adapter.open_chat(chat)
        title, single = self.adapter.current()
        if title != chat:
            raise RuntimeError(f"没能核对收件人「{chat}」，不会向当前聊天发送")
        mode = "private" if single else "group"
        if chat not in self.settings["chats"] and not outbound:
            raise RuntimeError(f"「{chat}」未加入监听列表")
        if chat in self.settings["chats"] and self.settings.chat_mode(chat) != mode:
            raise RuntimeError(f"「{chat}」的聊天类型不符，请在控制台更新")
        self.activate(chat, reply_latest, outbound_mode=mode if outbound else None)
        suspended = getattr(self.runner, "suspended_at", None)
        if suspended is not None:
            self.runner.last_incoming += time.monotonic() - suspended
            self.runner.suspended_at = None

    def observe_chats(self):
        states = self.adapter.session_details() if hasattr(self.adapter, "session_details") else self.adapter.session_states()
        self.incoming.observe(states, self.settings["chats"])
        self.previews = self.incoming.observed
        return states

    def higher_priority(self, runner):
        if not self.settings["multi_chat"]:
            return False
        self.observe_chats()
        chats, owner = self.settings["chats"], self.settings["owner"]
        next_chat = self.incoming.next(chats, owner, self.runners)
        return next_chat is not None and self.incoming.priority(next_chat, chats, owner, self.runners.get(next_chat)) \
            < self.incoming.priority(runner.chat, chats, owner, runner)

    def send_picture(self, chat, path, what):
        """A finished drawing (or word that it failed) to the chat that asked; never anywhere else."""
        from runner import NOTICE_CHARS
        source = self.runner.chat
        try:
            self.go(chat, outbound=True)
            if path:
                self.runner.show(path, f"画的{what[:20]}")
            else:
                self.runner.say(f"「{what[:20]}」没画出来，可能是图片生成出错了，换个说法再试试", "画图", NOTICE_CHARS)
        finally:
            try:
                self.go(source)
            except Exception:
                self.enabled.clear()
                self.emit("error", "无法返回原聊天，已暂停；不会向其他聊天继续发送")

    def send_relay(self, chat, text, why="主人让我说的", limit=None, at=()):
        """One exact recipient, no model call in the destination, no fallback chat.

        Returns the `at` members WeChat could not pick (sent as plain "@name")."""
        source = self.runner.chat
        try:
            self.go(chat, outbound=True)
            missing = self.runner.say(text, why, limit, at) or []
            self.emit("status", f"已向「{chat}」转告" if limit is None else f"已在「{chat}」发出{why}")
        finally:
            # Reading or model inference in the recipient chat must not accidentally
            # send an unrelated reply or leave later owner work in the wrong chat.
            try:
                self.go(source)
            except Exception:
                self.enabled.clear()
                self.emit("error", "无法返回原聊天，已暂停；不会向其他聊天继续发送")
        return missing

    def watch(self):
        """Preserve every changed session; owner, then private chats, then groups."""
        chats = self.settings["chats"]
        title, _ = self.adapter.current()
        if title in chats and title != self.runner.chat:
            self.activate(title)
        while (relay := self.runner.take_relay()):
            self.relays.append(relay)
        states = self.observe_chats()
        current, runner = self.runner.chat, self.runner
        job = self.incoming.waiting.get(current)
        if job and job.get("active") and runner.initialized and not runner.pending:
            state = states.get(current)
            if state == job.get("active_snapshot") or runner.own_echo(preview_text(state, current)):
                self.incoming.waiting.pop(current, None)
        owner = self.settings["owner"]
        excluded = [c for c, at in self.route_retry.items() if time.monotonic() < at]
        chat = self.incoming.next(chats, owner, self.runners, excluded)
        priority = lambda c: self.incoming.priority(c, chats, owner, self.runners.get(c))
        if runner.pending and (chat is None or priority(chat) >= priority(current)):
            if job:
                job.update(active=True, active_snapshot=states.get(current))
            return
        # Agent reports: in the owner's chat step() sends them; with multi-chat go
        # there; a single-chat bot elsewhere keeps them until the owner's chat is back.
        owner_runner = self.runners.get(owner)
        if current != owner and owner_runner and owner_runner.notices:
            self.reports[0:0] = list(owner_runner.notices)
            owner_runner.notices.clear()
        if current != owner and runner.notices:
            return  # a reply to the owner's command in this group: step() sends it here
        if current == owner:
            if self.reports:
                runner.notices.extend(self.reports)
                self.reports.clear()
                self.report_retry_at = 0.0
            if runner.notices:
                return
        elif self.reports and self.settings["multi_chat"] \
                and time.monotonic() >= self.report_retry_at \
                and (chat is None or priority(chat) > 0):
            from runner import NOTICE_CHARS
            from wechat_adapter import SendUncertain
            text = self.reports[0]
            try:
                self.send_relay(owner, text, "任务汇报", NOTICE_CHARS)
            except (SendUncertain, ValueError) as exc:
                self.reports.pop(0)
                self.report_retry_at = 0.0
                self.emit("error", f"任务汇报没发出去且不会重试：{exc}")
            except Exception as exc:
                self.report_retry_at = time.monotonic() + 10
                self.emit("error", f"任务汇报没发出去：{exc}")
            else:
                self.reports.pop(0)
                self.report_retry_at = 0.0
            return
        if self.pictures and (chat is None or chat == current or priority(chat) > 0):
            target, path, what = self.pictures.pop(0)
            try:
                self.send_picture(target, path, what)
            except Exception as exc:
                self.emit("error", f"画好的图没发到「{target}」：{exc}")
            return
        # The open chat's own waiting entry must not hold back what it asked for (single-chat mode).
        if self.relays and (chat is None or chat == current or priority(chat) > 0):
            # Owner sends carry a third item, the members to @: the exact text, any chat,
            # and a truthful report back.
            target, text, *owner_send = self.relays.pop(0)
            at = list(owner_send[0]) if owner_send else []
            from runner import NOTICE_CHARS
            from wechat_adapter import SendUncertain
            try:
                missing = self.send_relay(target, text, *(("主人让我发的", NOTICE_CHARS, at) if owner_send else ()))
            except Exception as exc:
                self.emit("error", f"向「{target}」转告失败：{exc}；没有改发到其他聊天")
                if owner_send:
                    self.reports.append(f"发给「{target}」的结果不确定，没有重发，请看一下微信" if isinstance(exc, SendUncertain)
                                        else f"没发给「{target}」：{exc}")
            else:
                if owner_send:
                    shown = "".join(f"@{m} " for m in at) + text
                    note = f"（群成员里没找到 {'、'.join(missing)}，按文字写的 @）" if missing else ""
                    self.reports.append(f"已发给「{target}」：{shown[:60] + '…' if len(shown) > 60 else shown}{note}")
            return
        # Only when nothing is waiting (single-chat mode keeps its own chat in the queue).
        idle = chat is None or (chat == current and not self.settings["multi_chat"])
        if not runner.pending and idle and self.open_topic(current):
            return
        if not self.settings["multi_chat"] or chat is None:
            if current in excluded and not runner.initialized:
                return False
            return
        job = self.incoming.waiting[chat]
        try:
            if chat != current:
                self.go(chat)
            if not self.runner.initialized or self.runner.active_epoch != self.enabled.epoch:
                latest = job["latest"]
                unread = latest.get("unread", 0) if isinstance(latest, dict) else 0
                self.runner.prime(self.adapter.read(), job["baseline"], unread, replay=job.get("replay", False))
            job.update(active=True, active_snapshot=states.get(chat))
            self.runner.status(f"处理「{chat}」 · 待处理聊天 {len(self.incoming.waiting)} 个")
        except Exception as exc:
            self.route_retry[chat] = time.monotonic() + 10
            self.emit("error", f"「{chat}」待办已保留：{exc}")
            return False

    def open_topic(self, current):
        """Nothing waiting: if a chat is due for her to speak up on her own, go there and let
        the next step() ask the model, which may still decide there is nothing worth saying."""
        if time.monotonic() < self.initiative_check_at:
            return False
        self.initiative_check_at = time.monotonic() + 15
        chats = self.settings["chats"]
        due = [c for c in self.initiative.due(chats) if c in chats and (self.settings["multi_chat"] or c == current)]
        if not due:
            return False
        target = due[0]
        try:
            if target != current:
                self.go(target)
            if not self.runner.initialized or self.runner.active_epoch != self.enabled.epoch:
                self.runner.prime(self.adapter.read(), replay=True)  # an anchor only: nothing old is answered
            self.runner.opening = self.initiative.describe(target)
        except Exception as exc:
            self.initiative.declined(target, chats[target]["mode"])
            self.emit("error", f"想去「{target}」冒个泡，没切过去：{exc}")
            return False
        return True

    def cycle(self):
        if self.watch() is False:
            return
        self.runner.step()
        self.watch()

    # ---- owner commands in the owner's private chat: sends and local agents ----
    def agent_command(self, text, act=True):
        """None for ordinary chat; else {"log": passphrase-free text, "reply": receipt}.

        With act=False (messages from a pause) nothing is sent, started or stopped.
        """
        line = " ".join(str(text).split())
        handled = self.owner_command(line, act, here=None)
        if handled:
            return handled
        send = OWNER_SEND.fullmatch(line) or OWNER_SAY_IN.fullmatch(line)
        if send and send["chat"] not in NOT_A_CHAT and send["chat"].casefold() not in AGENT_ALIASES:
            if not act:
                return {"log": line, "reply": None}
            at = send.groupdict().get("at")
            at = [self.settings["owner"] if at == "我" else at] if at else []
            if send["chat"] in ("群", "群聊"):  # "在群里艾特我，说：…": fine only while there is one group
                groups = [c for c, info in self.settings["chats"].items() if info["mode"] == "group"]
                if len(groups) != 1:
                    return {"log": line, "reply": "要发到哪个群？请写群名，例如“在示例群甲里说：…”"}
                self.queue_owner_send(groups[0], send["text"], at)
            else:
                self.queue_owner_send(self.resolve_chat(send["chat"]), send["text"], at, send["chat"])
            return {"log": line, "reply": None}
        handled = self.agent_task(text, line, act) if self.settings["agents_enabled"] else None
        if handled or not act or not SEND_HINT.search(line):
            return handled
        # Free wording ("在群里艾特我，说：测试一下"): the local model only extracts the
        # request; the recipient is still checked here and the result reported truthfully.
        try:
            wish = self.brain.parse_send(line, chats=self.settings["chats"], owner=self.settings["owner"],
                                         members=self.chat_members, jargon=self.chat_jargon)
        except Exception as exc:
            self.emit("error", f"没能理解这句是不是要代发：{exc}")
            return None
        if not (isinstance(wish, dict) and wish.get("send") is True):
            return None
        return {"log": line, "reply": self.owner_send_from(line, wish)}

    def group_command(self, text, act=True, sender=""):
        """Someone in a group addressing her. The owner: blocking, memory and drawing; anyone else:
        drawing within a daily allowance. Never sends elsewhere or other agent tasks from a group,
        since group sender names come from OCR and avatars."""
        names = [self.runner.name] + [a for a in self.settings.get("aliases") or [] if a]
        line = " ".join(str(text).replace(" ", " ").split())
        called = [n for n in names if f"@{n}" in line]
        if not called:
            return None
        for n in called:
            line = line.replace(f"@{n}", " ")
        line, log = " ".join(line.split()), " ".join(str(text).split())
        if sender == self.settings["owner"]:
            return self.owner_command(line, act, here=self.runner, log=log)
        what = self.draw_wish(line)
        if what is None or not self.settings.get("draw_for_members", True):
            return None
        if not act:
            return {"log": log, "reply": None}
        limit = int(self.settings.get("draw_daily_limit", 3))
        used = self.draw_quota(sender)
        if used >= limit:
            return {"log": log, "reply": f"{sender} 今天已经画了 {used} 张啦，明天再来"}
        reply = self.draw(what, self.runner)
        if reply.startswith("好"):
            self.draw_quota(sender, add=True)
            reply = f"{sender} {reply}（今天还能画 {limit - used - 1} 张）"
        return {"log": log, "reply": reply}

    def draw_wish(self, line):
        """What to draw, or None: "画画好难" is chat; a request needs 帮我画/画：/画一只…."""
        draw = DRAW.fullmatch(line)
        if not draw or not self.settings["agents_enabled"]:
            return None
        if draw["ask"] or draw["colon"] or re.match(r"[一两几个张幅只条朵位副]", draw["what"]):
            return draw["what"]
        return None

    def draw_quota(self, sender, add=False):
        """Pictures `sender` had drawn today (per group member name); `add` counts one more."""
        path, today = self.data / "draw_quota.json", datetime.now().strftime("%Y-%m-%d")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        counts = data.get("counts", {}) if data.get("date") == today else {}
        if add:
            counts[sender] = counts.get(sender, 0) + 1
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps({"date": today, "counts": counts}, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
        return counts.get(sender, 0)

    def owner_command(self, line, act, here=None, log=None):
        """拉黑 / 解除拉黑 / 黑名单 / 总结记忆. `here`: the group it was said in, else the owner's chat."""
        log = log or line
        what = self.draw_wish(line)
        if what is not None:  # the owner: no daily allowance
            return {"log": log, "reply": self.draw(what, here) if act else None}
        if BLOCKLIST.fullmatch(line):
            blocked = self.settings.get("blocked") or []
            return {"log": log, "reply": ("黑名单：" + "、".join(blocked) if blocked else "黑名单是空的") if act else None}
        if LEARN_NOW.fullmatch(line):
            return {"log": log, "reply": self.learn_now(here) if act else None}
        match = UNBLOCK.fullmatch(line) or BLOCK.fullmatch(line)
        if not match:
            return None
        name = match["name"].strip(" @＠「」“”\"'")
        bot = [self.runner.name] + list(self.settings.get("aliases") or [])
        if not name or name in NOT_A_CHAT or len(name) > 40:
            return None
        seen = name in self.settings["chats"] or any(name in self.chat_members(c) for c in self.settings["chats"])
        if match.re is BLOCK and not (seen or "@" in match["name"] or "拉黑" in line or "屏蔽" in line):
            return None  # "不要回复太长的消息" is chat, not a block: an unknown name needs 拉黑 or @
        if not act:
            return {"log": log, "reply": None}
        blocked = list(self.settings.get("blocked") or [])
        if match.re is UNBLOCK:
            if name not in blocked:
                return {"log": log, "reply": f"「{name}」本来就不在黑名单里"}
            self.settings.update(blocked=[n for n in blocked if n != name])
            return {"log": log, "reply": f"好，又开始接收「{name}」的消息了"}
        if name == self.settings["owner"] or name in bot:
            return {"log": log, "reply": "这个不能拉黑"}
        if name not in blocked:
            self.settings.update(blocked=blocked + [name])
        for runner in self.runners.values():  # already waiting for a reply: drop those too
            runner.pending[:] = [m for m in runner.pending if not runner.blocked(getattr(m, "sender", ""))]
        return {"log": log, "reply": f"好，之后不再接收「{name}」的消息"
                + ("" if seen else "（还没见过这个名字，按原样拉黑；名字要和群里显示的一致）")}

    def hand_off(self, kind, task):
        """The chat model heard the owner ask for real work. Drawing goes to the draw agent, anything
        else to Codex; the local agent only ever runs when the owner names it ("给本机 …")."""
        if not self.settings["agents_enabled"]:
            return None
        if kind == "draw":
            return self.draw(task)
        if kind == "codex":
            return self.dispatch("codex", task)
        return None

    def draw(self, what, here=None):
        """Hand a picture to Codex's image tool; it is sent to `here` (a group) or the owner when done."""
        import agents
        chat = here.chat if here else self.settings["owner"]
        what = " ".join(what.split()).strip("。.!！~～")[:200]
        if any(j.agent == "draw" and j.status == "running" for j in self.agents.jobs()):
            return "上一张还在画，画完再说"
        self.agents.configs = self.agent_configs()
        try:
            job_id = self.agents.start("draw", agents.draw_task(what))
        except Exception as exc:
            return f"没法画：{exc}"
        self.drawings[job_id] = (chat, what)
        self.emit("status", f"开始画：{what[:40]}")
        return "好，画好了发出来，大概要一分钟"

    def draw_folder(self):
        """Where finished drawings are kept (shown on the console's 画图 page)."""
        return Path(self.agent_configs()["draw"]["workdir"]) / "images"

    def gallery_notes(self):
        try:
            return json.loads((self.draw_folder() / "index.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_gallery_notes(self, data):
        index = self.draw_folder() / "index.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        temp = index.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(index)

    def note_drawing(self, path, what, chat):
        """Remember what a picture was and who it was for, next to the pictures (index.json)."""
        with self.gallery_lock:
            data = self.gallery_notes()
            data[Path(path).name] = {"what": what, "chat": chat, "time": datetime.now().strftime("%m-%d %H:%M")}
            self.save_gallery_notes(data)

    def drawings_list(self):
        """Every picture in the folder, newest first: {name, path, what, chat, time}."""
        folder = self.draw_folder()
        with self.gallery_lock:
            notes = self.gallery_notes()
        files = [f for f in folder.glob("*") if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")] \
            if folder.is_dir() else []
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return [{"what": "", "chat": "", "time": datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M"),
                 **notes.get(f.name, {}), "name": f.name, "path": f} for f in files]

    def delete_drawing(self, name):
        """Delete one picture and its note; only a plain file name inside the drawings folder."""
        folder = self.draw_folder()
        path = folder / Path(name).name
        if Path(name).name != name or path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            raise ValueError("只能删除画图文件夹里的图片")
        path.unlink(missing_ok=True)
        with self.gallery_lock:
            data = self.gallery_notes()
            if data.pop(name, None) is not None:
                self.save_gallery_notes(data)

    def drawn(self, job):
        """The finished picture's path, or None when nothing usable came out."""
        import agents
        if job.status != "done":
            return None
        try:
            return agents.find_image(job, Path(self.agent_configs()["draw"]["workdir"]))
        except Exception as exc:
            self.emit("error", f"找不到画好的图：{exc}")
            return None

    def learn_now(self, here=None):
        """The owner asked for memory to be updated now: this group, or every chat she has open."""
        runners = [here] if here else [r for r in self.runners.values() if r.history]
        notes = []
        for runner in runners:
            runner.learn(time.monotonic(), force=True)
            notes.append(runner.learn_note if here else f"「{runner.chat}」{runner.learn_note}")
        return "；".join(notes) or "还没有聊天记录可整理"

    def agent_task(self, text, line, act):
        """“任务”, “停掉 codex”, “给claude …”; None for anything else."""
        if line.rstrip("?？。!！") in ("任务", "任务列表"):
            return {"log": line, "reply": self.job_list() if act else None}
        match = re.fullmatch(r"(?:停掉|停止|停下)\s*[:：,，]?\s*([^\s的]+)(?:\s*的?任务)?", line)
        agent = AGENT_ALIASES.get(match.group(1).casefold()) if match else None
        if agent:
            return {"log": line, "reply": self.stop_agent(agent) if act else None}
        parsed = parse_command(text, DEFAULT_ROUTES, self.settings["agent_passphrase"])
        if not parsed:
            return None
        agent, task = parsed
        return {"log": f"给{agent} {' '.join(task.split())}", "reply": self.dispatch(agent, task) if act else None}

    def queue_owner_send(self, chat, text, at, asked=None):
        """Queue one owner send; it is reported once really tried, never claimed early."""
        if chat is None:
            self.reports.append(f"「{asked}」能对上好几个聊天，没发。请写完整的聊天名")
            return
        self.relays.append((chat, text, tuple(m for m in at if m)))

    def owner_send_from(self, line, wish):
        """Check what the model read out of free wording before anything is sent.

        The chat must be a watched chat or a name the owner actually typed; @ targets must be
        typed by the owner, be the owner ("我"), or be a member already seen in that group.
        Returns a question for the owner when it is not clear, else None (a report follows).
        """
        from runner import NOTICE_CHARS
        chats = self.settings["chats"]
        named = " ".join(str(wish.get("chat") or "").split())
        groups = [c for c, info in chats.items() if info["mode"] == "group"]
        # Only a chat the owner named (in full or as a short "X群"), or the only group there is.
        shorts = {line[i - k:i + 1] for i, ch in enumerate(line) if ch == "群" for k in range(2, 11) if i >= k}
        core = named[:-1] if named.endswith("群") else named
        typed = named in line or (len(core) >= 2 and core in line) \
            or any(self.resolve_chat(short) == named for short in shorts)
        chat = self.resolve_chat(named) if named and typed else None
        if chat is None and len(groups) == 1 and (not named or named == groups[0]) \
                and re.search(r"群|艾特|爱特|@|＠", line):
            chat = groups[0]
        text = " ".join(str(wish.get("text") or "").split())
        if not chat:
            return "要发到哪个聊天？请写完整的聊天名，例如“在示例测试群里说：…”"
        if not text or len(text) > NOTICE_CHARS:
            return f"要在「{chat}」说什么？请把原话写在冒号后面"
        known = set(self.chat_members(chat))
        at, unsure = [], []
        for name in wish.get("at") or []:
            name = " ".join(str(name).split()).lstrip("@＠")
            ok = name and (name in line or name in known or (name == self.settings["owner"] and "我" in line))
            (at if ok else unsure).append(name)
        if unsure:
            return f"没认出要艾特的「{'、'.join(unsure)}」是群里的谁，没发。请写群里显示的名字"
        self.queue_owner_send(chat, text, at)
        return None

    def chat_members(self, chat):
        """Names already seen in a group (sender book, roster), for checking @ targets."""
        from personas import safe_name
        names = list((self.settings["chats"].get(chat) or {}).get("roster") or [])
        try:
            book = json.loads((self.data / "senders" / f"{safe_name(chat)}.json").read_text(encoding="utf-8"))
            names += [n for entry in book for n in (entry.get("names") or {})]
        except Exception:
            pass
        return list(dict.fromkeys(n for n in names if n))

    def chat_jargon(self, chat):
        """The group's nicknames and slang from memory ("小王 / 老赵：…对应 小李"), as lines."""
        from personas import safe_name
        try:
            data = json.loads((self.data / "memory" / f"{safe_name(chat)}.json").read_text(encoding="utf-8"))
            return [f"{term}：{entry.get('meaning', '')}" for term, entry in (data.get("jargon") or {}).items()][:60]
        except Exception:
            return []

    def resolve_chat(self, name):
        """A watched chat the owner named in short ("测试群" → "示例测试群"), else `name` as typed.

        None when the short name fits several watched chats. Unwatched names go to WeChat's
        exact search unchanged, so nothing is ever sent to a merely similar chat.
        """
        chats = list(self.settings["chats"])
        if name in chats:
            return name
        core = name[:-1] if name.endswith("群") and len(name) > 2 else name
        hits = [c for c in chats if core in c] if len(core) >= 2 else []
        if len(hits) > 1:
            return None
        return hits[0] if hits else name

    def agent_configs(self):
        configs = default_configs()
        for name, config in configs.items():  # work and pictures live with the rest of her data
            config["workdir"] = str(self.data / "agent_work" / name)
        for name, extra in (self.settings.get("agent_overrides") or {}).items():
            if name in configs and isinstance(extra, dict):
                configs[name].update(extra)
        return configs

    @staticmethod
    def job_label(job):
        task = " ".join(job.task.split())
        return f"{AGENT_NAMES.get(job.agent, job.agent)}「{task[:20] + '…' if len(task) > 20 else task}」"

    def dispatch(self, agent, task):
        name = AGENT_NAMES.get(agent, agent)
        if any(j.agent == agent and j.status == "running" for j in self.agents.jobs()):
            return f"{name} 手上还有任务没做完，这个没派出去。发“任务”看进度，要换就先说“停掉{agent}”"
        self.agents.configs = self.agent_configs()  # overrides apply from the next task on
        try:
            self.agents.start(agent, task)
        except Exception as exc:
            return f"没派出去：{exc}"
        self.emit("status", f"已派给 {name}：{' '.join(task.split())[:60]}")
        return f"收到，交给 {name} 了，做完告诉你"

    def job_list(self):
        from runner import NOTICE_CHARS
        jobs = self.agents.jobs()
        running, recent = [], []
        for job in jobs:
            status = job.status
            if status == "running":
                running.append(job)
            elif len(recent) < 3:
                recent.append((job, status, job.finished or time.time()))
        if not jobs:
            return "现在没有任务，最近也没有做完的"
        parts = [f"{self.job_label(j)}进行中，已 {int((time.time() - j.started) // 60)} 分钟" for j in running]
        parts += [f"{self.job_label(j)}{JOB_STATES.get(status, status)}（{datetime.fromtimestamp(finished):%H:%M}）"
                  for j, status, finished in recent]
        return "；".join(parts)[:NOTICE_CHARS]

    def stop_agent(self, agent):
        name = AGENT_NAMES.get(agent, agent)
        running = [j for j in self.agents.jobs() if j.agent == agent and j.status == "running"]
        if not running:
            return f"{name} 现在没有在跑的任务"
        stopped = sum(bool(self.agents.stop(job.id)) for job in running)
        if not stopped:
            return f"{name} 刚好已经结束了，汇报马上到"
        return f"好，让 {name} 停下了"

    def report(self, job):
        """One line for the owner: the local model's summary, else status plus the last lines."""
        from runner import NOTICE_CHARS
        head = self.job_label(job) + JOB_STATES.get(job.status, job.status)
        if job.status == "stopped":
            return head
        try:
            if not job.tail.strip():
                raise ValueError("没有输出")
            body = self.brain.summarize_job(job)
        except Exception:
            lines = [line.strip() for line in job.tail.splitlines() if line.strip()][-4:]
            body = f"退出码 {job.returncode}，最后几行：{' / '.join(lines)[-300:]}" if lines else \
                f"退出码 {job.returncode}，没有输出"
        return " ".join(f"{head}：{body} （日志 {job.log_path.name}）".split())[:NOTICE_CHARS]

    def collect_reports(self):
        while True:
            try:
                job = self.finished.get_nowait()
            except queue.Empty:
                return
            if job.id in self.drawings:
                chat, what = self.drawings.pop(job.id)
                path = self.drawn(job)
                if path:
                    self.note_drawing(path, what, chat)
                self.pictures.append((chat, path, what))
                continue
            self.reports.append(self.report(job))
            self.emit("status", f"{self.job_label(job)}{JOB_STATES.get(job.status, job.status)}，汇报会发到主人私聊")

    def ensure_model(self):
        model = self.settings["model"]
        if self.lm.instances().get(model["identifier"]) != model["key"]:
            self.emit("status", f"正在加载 {model['key']}（全部放到显卡）…")
            self.lm.ensure(model["key"], model["identifier"], model["context"])
            self.emit("status", f"模型已就绪：{model['key']}")

    def handle(self, args):
        name = args[0]
        if name == "start":
            ticket = args[1]
            if self.settings.chat_mode(self.settings["chat"]) is None:
                raise RuntimeError("请先在“聊天”页添加并选择一个聊天")
            self.adapter.open_chat(self.settings["chat"])
            self.adapter.validate()
            self.ensure_model()
            if not self.stop.is_set() and self.enabled.epoch == ticket:
                rows = self.adapter.read()
                if self.runner.initialized:
                    self.runner.ingest(rows, replay=True)  # context only, no agent commands
                    self.runner.pending.clear()
                else:
                    self.runner.prime(rows)
                self.incoming = ChatQueue()
                self.route_retry.clear()
                self.observe_chats()
                for waiting in self.incoming.waiting.values():
                    waiting["replay"] = True  # unread from the pause: no agent commands
                self.enabled.set()
                self.runner.active_epoch = self.enabled.epoch
        elif name == "switch":
            chat, allow = args[1], args[2]
            self.enabled.clear()
            self.adapter.open_chat(chat)
            title, single = self.adapter.current()
            if title != chat:
                raise RuntimeError(f"没能打开「{chat}」")
            chats = self.settings["chats"]
            if chat not in chats and not allow:
                raise RuntimeError(f"「{chat}」不在允许列表里")
            # The title bar tells private from group; trust it over a stale setting.
            chats[chat] = {**chats.get(chat, {}), "mode": "private" if single else "group"}
            self.settings.update(chat=chat, chats=chats)
            self.make_runner()
            self.emit("chats", None)
            self.emit("status", f"已切换到「{chat}」（{'私聊' if single else '群聊'}），旧消息不会回复。"
                                "点“开始 / 继续”开始接话")
        elif name == "sessions":
            self.emit("sessions", self.adapter.sessions())
        elif name == "inspect":
            from inspect_group import inspect
            self.emit("status", inspect(self.adapter, DATA / "debug"))
        elif name == "roster":
            self.make_runner()
            self.emit("status", "成员名单已更新")
        elif name == "stickers":
            if self.scanning and self.scanning.is_alive():
                self.emit("status", "表情包还在整理中，等这一轮完成")
                return
            self.ensure_model()
            # On its own thread: a big folder takes many minutes and chats must not wait.
            self.scanning = threading.Thread(target=self.scan_stickers, daemon=True)
            self.scanning.start()
        elif name == "learn":
            if not self.runner.learn(time.monotonic(), force=True):
                self.emit("status", "没有新的聊天可整理，或记忆功能已关闭")
            self.emit("memory", None)
        elif name == "save_memory":
            chat, data, token = args[1:]
            try:
                runner = self.runners.get(chat)
                if runner is None:
                    raise ValueError("这个聊天已不在当前会话中，请重新打开编辑窗口")
                changed = runner.memory.replace(data, token)
                # Old messages already reviewed by the owner must not immediately
                # recreate a deleted or corrected fact in the next learning pass.
                if changed:
                    runner.unlearned = 0
            except Exception as exc:
                self.emit("memory_edit_result", {"chat": chat, "error": str(exc)})
            else:
                self.emit("memory_edit_result", {"chat": chat, "error": None})
                self.emit("status", f"已保存「{chat}」的记忆，下一次回复生效")
                self.emit("memory", None)
        elif name == "forget":
            self.runner.memory.clear()
            self.emit("status", f"已清空「{self.runner.chat}」的记忆")
            self.emit("memory", None)
        elif name == "clear":
            self.runner.clear_history()
            self.emit("status", f"已清空「{self.runner.chat}」的上下文")
        elif name == "model":
            self.enabled.clear()
            key, context = args[1], args[2]
            identifier = self.settings["model"]["identifier"]
            self.emit("status", f"正在切换到 {key}，加载期间暂停…")
            self.lm.ensure(key, identifier, context, force=True)
            self.settings.update(model={"key": key, "identifier": identifier, "context": context})
            self.emit("status", f"模型已切换为 {key}。点“开始 / 继续”恢复")

    def scan_stickers(self):
        self.emit("status", "正在后台让模型看一遍新表情包，聊天照常进行…")
        try:
            count = self.stickers.scan(self.brain.describe_image,
                                       lambda done, total: self.emit("status", f"已整理 {done}/{total} 张"))
        except Exception as exc:
            self.emit("error", f"整理表情包出错：{exc}")
            return
        try:
            from lmstudio import release_memory
            release_memory()  # describing many images leaves caches behind in RAM
        except Exception:
            pass
        skipped = self.stickers.skipped
        self.emit("status", f"表情包库整理完成，新增 {count} 张，共 {len(self.stickers.catalog(1000))} 张"
                  + (f"；跳过 {len(skipped)} 张（找不到或打不开，例如 {skipped[0]}）" if skipped else ""))
        self.emit("stickers", self.stickers.catalog(1000))

    def worker(self):
        try:
            from wechat_adapter import WeChatAdapter, auto
            with auto.UIAutomationInitializerInThread():
                chat = self.settings["chat"]
                self.adapter = WeChatAdapter(chat, self.settings.chat_mode(chat) or "private",
                                             self.settings["max_reply_chars"])
                self.make_runner()
                try:
                    self.emit("sessions", self.adapter.sessions())
                except Exception:
                    pass  # WeChat not ready yet; the refresh button retries.
                while not self.stop.is_set():
                    try:
                        while True:
                            self.handle(self.commands.get_nowait())
                    except queue.Empty:
                        pass
                    except Exception as exc:
                        self.emit("error", str(exc))
                    self.adapter.max_chars = self.settings["max_reply_chars"]
                    try:
                        self.collect_reports()
                    except Exception as exc:
                        self.emit("error", f"整理任务汇报出错：{exc}")
                    if self.enabled.is_set():
                        try:
                            self.cycle()
                        except Exception as exc:
                            self.runner.status(str(exc))
                    else:
                        self.runner.status("已暂停")
                    self.stop.wait(self.settings["poll_seconds"])
        except Exception as exc:
            self.emit("error", "启动失败：" + str(exc))

    def fetch_models(self):
        def job():
            try:
                self.emit("models", {"models": self.lm.models(), "loaded": self.lm.instances()})
            except Exception as exc:
                self.emit("error", f"读取模型列表失败：{exc}")
        threading.Thread(target=job, daemon=True).start()


class App:
    SKIP = {"文件传输助手", "WeChat团队", "微信团队", "微信支付"}

    def __init__(self, bot: Bot):
        self.bot, self.settings, self.personas = bot, bot.settings, bot.personas
        self.window = tk.Tk()
        self.window.title("AI 群友控制台")
        self.window.geometry("760x620")
        self.window.option_add("*Font", (FONT, 10))
        self.status = tk.StringVar(value="已暂停")
        self.header = tk.StringVar()
        notebook = ttk.Notebook(self.window)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        self.build_run(self.tab(notebook, "运行"))
        self.build_chats(self.tab(notebook, "聊天"))
        self.build_persona(self.tab(notebook, "人设"))
        self.build_model(self.tab(notebook, "模型"))
        self.build_stickers(self.tab(notebook, "表情包"))
        self.build_gallery(self.tab(notebook, "画图"))
        self.build_memory(self.tab(notebook, "记忆"))
        self.build_rhythm(self.tab(notebook, "节奏"))
        self.refresh_header()
        self.window.protocol("WM_DELETE_WINDOW", self.quit)
        self.window.after(100, self.pump)

    @staticmethod
    def tab(notebook, title):
        frame = ttk.Frame(notebook, padding=14)
        notebook.add(frame, text=title)
        return frame

    # ---- 运行 ----
    def build_run(self, frame):
        ttk.Label(frame, textvariable=self.header, font=(FONT, 16, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.status, wraplength=700, foreground="#555").pack(anchor="w", pady=(4, 10))
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        ttk.Label(bar, text="聊天：").pack(side="left")
        self.run_chat = ttk.Combobox(bar, state="readonly", width=24)
        self.run_chat.pack(side="left")
        ttk.Button(bar, text="切换到这个聊天", command=self.switch_chat).pack(side="left", padx=6)
        ttk.Button(bar, text="刷新微信会话", command=lambda: self.bot.command("sessions")).pack(side="left")
        self.multi = tk.BooleanVar(value=self.settings["multi_chat"])
        ttk.Checkbutton(frame, variable=self.multi, command=self.toggle_multi, text=(
            "同时照看所有已允许的聊天（有新消息就自动切过去；主人私聊里可以让她去群里说话）")
                        ).pack(anchor="w", pady=(8, 0))
        bar2 = ttk.Frame(frame)
        bar2.pack(fill="x", pady=10)
        ttk.Button(bar2, text="开始 / 继续", command=self.start).pack(side="left")
        ttk.Button(bar2, text="暂停", command=self.pause).pack(side="left", padx=8)
        ttk.Button(bar2, text="清空当前聊天上下文", command=self.clear_history).pack(side="left", padx=8)
        ttk.Button(bar2, text="退出", command=self.quit).pack(side="right")
        ttk.Label(frame, wraplength=700, foreground="#777", text=(
            "保持微信窗口可见（可在副屏）。切换聊天、遮挡或最小化时自动暂停收发；"
            "暂停期间的消息不会补回复。发送结果不确定时会暂停，请先看微信再继续。")).pack(anchor="w")
        self.log = tk.Text(frame, height=16, wrap="word", state="disabled", font=(FONT, 10))
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self.fill_chats()

    def refresh_header(self):
        model = self.settings["model"]["key"]
        chat = self.settings["chat"]
        mode = {"private": "私聊", "group": "群聊"}.get(self.settings.chat_mode(chat), "未授权")
        self.header.set(f"{self.settings['persona']}  ·  {chat}（{mode}）  ·  {model}")

    def start(self):
        self.bot.command("start", self.bot.enabled.epoch)

    def pause(self):
        self.bot.enabled.clear()
        self.status.set("已暂停")

    def switch_chat(self):
        chat = self.run_chat.get().strip()
        if not chat or chat == self.settings["chat"]:
            return
        allow = chat in self.settings["chats"]
        if not allow:
            allow = messagebox.askyesno("切换聊天", f"「{chat}」还不在允许列表里。\n加入并切换过去吗？私聊还是群聊会自动识别。")
            if not allow:
                return
        self.bot.command("switch", chat, allow)

    def toggle_multi(self):
        self.settings.update(multi_chat=self.multi.get())
        self.status.set("已开启多聊天：她会在已允许的聊天之间自动切换" if self.multi.get()
                        else "已关闭多聊天：只在当前聊天里接话")

    def clear_history(self):
        if messagebox.askyesno("清空上下文", f"清空「{self.settings['chat']}」保存的聊天上下文？\n换了人设后建议清空。"):
            self.bot.command("clear")

    # ---- 聊天 ----
    def build_chats(self, frame):
        ttk.Label(frame, wraplength=700, text=(
            "只有加入这个列表的聊天才会被读取和回复。名字必须和微信聊天标题完全一致；"
            "群聊标题后的人数会自动忽略。")).pack(anchor="w")
        self.chat_list = tk.Listbox(frame, height=8)
        self.chat_list.pack(fill="x", pady=8)
        add = ttk.Frame(frame)
        add.pack(fill="x")
        ttk.Label(add, text="名字：").pack(side="left")
        self.new_chat = ttk.Combobox(add, width=24)
        self.new_chat.pack(side="left")
        ttk.Button(add, text="读取微信会话列表", command=lambda: self.bot.command("sessions")).pack(side="left", padx=6)
        self.new_mode = tk.StringVar(value="private")
        ttk.Radiobutton(add, text="私聊", value="private", variable=self.new_mode).pack(side="left", padx=(10, 0))
        ttk.Radiobutton(add, text="群聊（试验）", value="group", variable=self.new_mode).pack(side="left")
        roster = ttk.Frame(frame)
        roster.pack(fill="x", pady=(8, 0))
        ttk.Label(roster, text="群成员名单（可空，用逗号分隔，用来纠正昵称识别错字）：").pack(anchor="w")
        self.new_roster = ttk.Entry(roster)
        self.new_roster.pack(fill="x")
        self.chat_list.bind("<<ListboxSelect>>", lambda e: self.show_chat())
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=8)
        ttk.Button(buttons, text="添加 / 修改", command=self.add_chat).pack(side="left")
        ttk.Button(buttons, text="移除选中", command=self.remove_chat).pack(side="left", padx=8)
        ttk.Button(buttons, text="校准群聊识别（只读）", command=lambda: self.bot.command("inspect")).pack(side="left", padx=8)
        ttk.Label(frame, wraplength=700, foreground="#777", text=(
            "群聊（试验）：按头像右侧的昵称做本机 OCR，并按头像记住是谁；认不出时标为“群友N”。"
            "默认只在被点名或很适合时插话，不主动起话题，未被点名时两次插话至少间隔“节奏”页设置的秒数。"
            "先在微信里打开要接入的群，点“校准群聊识别”，看日志里认出的名字是否正确，再开始。")).pack(anchor="w")
        alias = ttk.Frame(frame)
        alias.pack(fill="x", pady=(14, 0))
        ttk.Label(alias, text="机器人微信号的昵称（群里@她用的名字，多个用逗号分隔）：").pack(side="left")
        self.aliases = ttk.Entry(alias, width=20)
        self.aliases.insert(0, "，".join(self.settings.get("aliases", [])))
        self.aliases.pack(side="left")
        ttk.Button(alias, text="保存", command=self.save_aliases).pack(side="left", padx=6)
        owner = ttk.Frame(frame)
        owner.pack(fill="x", pady=(8, 0))
        ttk.Label(owner, text="主人（谁的私聊可以指挥她去群里说话）：").pack(side="left")
        self.owner = ttk.Combobox(owner, width=18, values=[c for c, i in self.settings["chats"].items()
                                                          if i["mode"] == "private"])
        self.owner.set(self.settings["owner"])
        self.owner.pack(side="left")
        ttk.Button(owner, text="保存", command=lambda: (self.settings.update(owner=self.owner.get().strip()),
                                                       self.status.set(f"主人设为「{self.owner.get().strip()}」"))
                   ).pack(side="left", padx=6)
        agent = ttk.Frame(frame)
        agent.pack(fill="x", pady=(8, 0))
        ttk.Label(agent, text="派任务口令（主人私聊发“给claude 口令 任务”，留空则不需要）：").pack(side="left")
        self.passphrase = ttk.Entry(agent, width=14, show="*")
        self.passphrase.insert(0, self.settings["agent_passphrase"])
        self.passphrase.pack(side="left")
        self.agents_on = tk.BooleanVar(value=self.settings["agents_enabled"])
        ttk.Checkbutton(agent, text="允许派任务", variable=self.agents_on).pack(side="left", padx=6)
        ttk.Button(agent, text="保存", command=self.save_agents).pack(side="left")
        self.fill_chats()

    def save_agents(self):
        phrase = self.passphrase.get().strip()
        self.settings.update(agent_passphrase=phrase, agents_enabled=self.agents_on.get())
        # Never echo the passphrase into the status line or the log.
        self.status.set(("已保存派任务口令" if phrase else "已清空口令：派任务不需要口令")
                        if self.agents_on.get() else "已关闭派任务：这类消息当普通聊天")

    def save_aliases(self):
        import re
        names = [n.strip().lstrip("@") for n in re.split(r"[,，、\s]+", self.aliases.get()) if n.strip()]
        self.settings.update(aliases=names)
        self.status.set(f"已保存：群里@{'、@'.join(names) or '（无）'} 都算在叫她")

    def show_chat(self):
        selected = self.chat_list.curselection()
        if not selected:
            return
        name = list(self.settings["chats"])[selected[0]]
        info = self.settings["chats"][name]
        self.new_chat.set(name)
        self.new_mode.set(info["mode"])
        self.new_roster.delete(0, "end")
        self.new_roster.insert(0, "，".join(info.get("roster", [])))

    def fill_chats(self):
        chats = self.settings["chats"]
        others = [s for s in getattr(self, "sessions", []) if s not in chats and s not in self.SKIP]
        self.run_chat["values"] = list(chats) + others
        self.run_chat.set(self.settings["chat"])
        if hasattr(self, "chat_list"):
            self.chat_list.delete(0, "end")
            for name, info in chats.items():
                self.chat_list.insert("end", f"{name}    （{'群聊' if info['mode'] == 'group' else '私聊'}）")

    def add_chat(self):
        name = self.new_chat.get().strip()
        if not name:
            return
        import re
        chats = self.settings["chats"]
        roster = [r.strip() for r in re.split(r"[,，、\n]", self.new_roster.get()) if r.strip()]
        chats[name] = {"mode": self.new_mode.get(), "roster": roster}
        self.settings.update(chats=chats)
        self.fill_chats()
        if name == self.settings["chat"]:
            self.bot.command("roster")

    def remove_chat(self):
        selected = self.chat_list.curselection()
        if not selected:
            return
        name = list(self.settings["chats"])[selected[0]]
        if name == self.settings["chat"]:
            messagebox.showinfo("移除聊天", "不能移除正在使用的聊天，请先切换到别的聊天。")
            return
        chats = self.settings["chats"]
        chats.pop(name)
        self.settings.update(chats=chats)
        self.fill_chats()

    # ---- 人设 ----
    def build_persona(self, frame):
        left = ttk.Frame(frame)
        left.pack(side="left", fill="y")
        self.persona_list = tk.Listbox(left, width=14, exportselection=False)
        self.persona_list.pack(fill="y", expand=True)
        self.persona_list.bind("<<ListboxSelect>>", lambda e: self.show_persona())
        ttk.Button(left, text="新建", command=self.new_persona).pack(fill="x", pady=(6, 0))
        ttk.Button(left, text="删除", command=self.delete_persona).pack(fill="x", pady=4)
        right = ttk.Frame(frame)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        row = ttk.Frame(right)
        row.pack(fill="x")
        ttk.Label(row, text="名字：").pack(side="left")
        self.persona_name = ttk.Entry(row, width=14)
        self.persona_name.pack(side="left")
        ttk.Label(row, text="  活跃度（温度 0~1.5）：").pack(side="left")
        self.persona_temp = ttk.Spinbox(row, from_=0, to=1.5, increment=0.05, width=6)
        self.persona_temp.pack(side="left")
        ttk.Label(right, foreground="#777", wraplength=520, text=(
            "写清身份、性格、说话方式、爱聊什么、不做什么。名字会用来识别群里的点名，"
            "以及“<名字>暂停”这类静音指令。格式和安全规则由程序统一追加，不用写。")).pack(anchor="w", pady=6)
        self.persona_text = tk.Text(right, wrap="word", height=16, font=(FONT, 10))
        self.persona_text.pack(fill="both", expand=True)
        buttons = ttk.Frame(right)
        buttons.pack(fill="x", pady=6)
        ttk.Button(buttons, text="保存", command=self.save_persona).pack(side="left")
        ttk.Button(buttons, text="保存并设为当前人设", command=lambda: self.save_persona(activate=True)).pack(side="left", padx=8)
        self.fill_personas(self.settings["persona"])

    def fill_personas(self, select=None):
        self.persona_list.delete(0, "end")
        names = self.personas.names()
        for name in names:
            self.persona_list.insert("end", ("● " if name == self.settings["persona"] else "   ") + name)
        if select in names:
            index = names.index(select)
            self.persona_list.selection_clear(0, "end")
            self.persona_list.selection_set(index)
            self.show_persona()

    def selected_persona(self):
        selected = self.persona_list.curselection()
        return self.personas.names()[selected[0]] if selected else None

    def show_persona(self):
        name = self.selected_persona()
        if not name:
            return
        persona = self.personas.load(name)
        self.editing = name
        self.persona_name.delete(0, "end")
        self.persona_name.insert(0, persona.name)
        self.persona_temp.set(persona.temperature)
        self.persona_text.delete("1.0", "end")
        self.persona_text.insert("1.0", persona.prompt)

    def new_persona(self):
        self.editing = None
        self.persona_list.selection_clear(0, "end")
        self.persona_name.delete(0, "end")
        self.persona_name.insert(0, "新角色")
        self.persona_temp.set(0.85)
        self.persona_text.delete("1.0", "end")
        self.persona_text.insert("1.0", TEMPLATE.format(name="新角色"))

    def save_persona(self, activate=False):
        try:
            persona = Persona(self.persona_name.get(), self.persona_text.get("1.0", "end").strip(),
                              float(self.persona_temp.get()))
            if persona.name.strip() != getattr(self, "editing", None) and persona.name.strip() in self.personas.names():
                if not messagebox.askyesno("覆盖人设", f"已有名为「{persona.name}」的人设，覆盖它？"):
                    return
            self.personas.save(persona)
            old = getattr(self, "editing", None)
            if old and old != persona.name:
                self.personas.delete(old)
                if self.settings["persona"] == old:
                    activate = True
            if activate:
                self.settings.update(persona=persona.name)
                self.status.set(f"已换成「{persona.name}」，下一句开始生效。可在“运行”页清空旧上下文")
            self.editing = persona.name
            self.fill_personas(persona.name)
            self.refresh_header()
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc))

    def delete_persona(self):
        name = self.selected_persona()
        if not name:
            return
        if name == self.settings["persona"]:
            messagebox.showinfo("删除人设", "不能删除正在使用的人设，请先把别的人设设为当前。")
            return
        if messagebox.askyesno("删除人设", f"删除「{name}」？"):
            self.personas.delete(name)
            self.fill_personas(self.settings["persona"])

    # ---- 模型 ----
    def build_model(self, frame):
        self.model_info = tk.StringVar(value="读取中…")
        ttk.Label(frame, textvariable=self.model_info, wraplength=700).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=10)
        ttk.Label(row, text="模型：").pack(side="left")
        self.model_box = ttk.Combobox(row, state="readonly", width=70)
        self.model_box.pack(side="left")
        row2 = ttk.Frame(frame)
        row2.pack(fill="x")
        ttk.Label(row2, text="上下文长度：").pack(side="left")
        self.model_ctx = ttk.Combobox(row2, values=["4096", "8192", "16384"], width=8)
        self.model_ctx.set(str(self.settings["model"]["context"]))
        self.model_ctx.pack(side="left")
        ttk.Button(row2, text="刷新列表", command=self.bot.fetch_models).pack(side="left", padx=8)
        ttk.Button(row2, text="估算显存", command=self.estimate_model).pack(side="left")
        ttk.Button(row2, text="切换并加载", command=self.switch_model).pack(side="left", padx=8)
        ttk.Label(frame, foreground="#777", wraplength=700, text=(
            "模型总是整份放进显卡（RTX 5090 32GB）。如果显存不够，LM Studio 会拒绝加载，"
            "而不是像之前那样一半放 CPU、让电脑变卡。切换只会卸载机器人自己的那份模型，"
            "不动你在 LM Studio 里另外加载的模型。")).pack(anchor="w", pady=10)
        self.models = []
        self.bot.fetch_models()

    def chosen_model(self):
        index = self.model_box.current()
        return self.models[index]["key"] if index >= 0 else None

    def context(self):
        value = int(self.model_ctx.get())
        if not 2048 <= value <= 65536:
            raise ValueError("上下文长度应在 2048 到 65536 之间")
        return value

    def estimate_model(self):
        key = self.chosen_model()
        if not key:
            return
        def job():
            try:
                self.bot.emit("status", f"{key}：{self.bot.lm.estimate(key, self.context())}")
            except Exception as exc:
                self.bot.emit("error", f"估算失败：{exc}")
        threading.Thread(target=job, daemon=True).start()

    def switch_model(self):
        key = self.chosen_model()
        if not key:
            return
        try:
            context = self.context()
        except ValueError as exc:
            messagebox.showerror("切换模型", str(exc))
            return
        if messagebox.askyesno("切换模型", f"暂停机器人，卸载当前模型，并把 {key} 整份加载到显卡？"):
            self.bot.command("model", key, context)
            self.window.after(3000, self.bot.fetch_models)

    def show_models(self, data):
        self.models = data["models"]
        self.model_box["values"] = [m["label"] for m in self.models]
        current = self.settings["model"]
        keys = [m["key"] for m in self.models]
        if current["key"] in keys:
            self.model_box.current(keys.index(current["key"]))
        loaded = data["loaded"].get(current["identifier"])
        others = [f"{k}（{v}）" for k, v in data["loaded"].items() if k != current["identifier"]]
        self.model_info.set(
            f"机器人使用：{current['key']}　当前加载：{loaded or '未加载（开始时自动加载）'}"
            + (f"\nLM Studio 里另外加载着：{'，'.join(others)}" if others else ""))
        self.refresh_header()

    # ---- 表情包 ----
    def build_stickers(self, frame):
        ttk.Label(frame, wraplength=700, text=(
            "把表情包图片（png / jpg / gif / webp）放进文件夹，再点“整理表情包库”，本地模型会给每张新图写描述；"
            "聊天时她按描述挑合适的发。选中任意一张可以看预览、改描述。")).pack(anchor="w")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text="打开文件夹", command=lambda: __import__("os").startfile(self.bot.stickers.folder)).pack(side="left")
        ttk.Button(bar, text="整理表情包库", command=lambda: self.bot.command("stickers")).pack(side="left", padx=8)
        ttk.Button(bar, text="刷新", command=lambda: (self.show_stickers(), self.show_inbox())).pack(side="right")

        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        lists = ttk.Frame(body)
        lists.pack(side="left", fill="both", expand=True)
        ttk.Label(lists, text="表情包库（她会发的）：").pack(anchor="w")
        self.sticker_list = tk.Listbox(lists, height=7, exportselection=False)
        self.sticker_list.pack(fill="both", expand=True)
        self.sticker_list.bind("<<ListboxSelect>>", lambda e: self.select_sticker("library"))
        ttk.Label(lists, text="收集到的（别人发的表情包截图，动图会变静态；挑好的收进库）：").pack(anchor="w", pady=(8, 0))
        self.inbox_list = tk.Listbox(lists, selectmode="extended", height=7, exportselection=False)
        self.inbox_list.pack(fill="both", expand=True)
        self.inbox_list.bind("<<ListboxSelect>>", lambda e: self.select_sticker("inbox"))
        self.sticker_preview = ttk.Label(body, text="选中一张看预览", width=24, anchor="center")
        self.sticker_preview.pack(side="left", padx=10)

        edit = ttk.Frame(frame)
        edit.pack(fill="x", pady=(8, 0))
        ttk.Label(edit, text="描述（她按这句话判断什么时候发）：").pack(side="left")
        self.sticker_desc = ttk.Entry(edit)
        self.sticker_desc.pack(side="left", fill="x", expand=True)
        self.sticker_desc.bind("<Return>", lambda e: self.save_sticker_desc())
        ttk.Button(edit, text="保存描述", command=self.save_sticker_desc).pack(side="left", padx=6)
        bar2 = ttk.Frame(frame)
        bar2.pack(fill="x", pady=6)
        ttk.Button(bar2, text="收进表情包库", command=self.keep_inbox).pack(side="left")
        ttk.Button(bar2, text="丢弃选中", command=self.discard_inbox).pack(side="left", padx=8)
        ttk.Button(bar2, text="全部丢弃", command=lambda: self.discard_inbox(everything=True)).pack(side="left")
        ttk.Button(bar2, text="从库里删除", command=self.remove_sticker).pack(side="right")
        self.sticker_target = None  # ("library", sid) or ("inbox", iid) being edited
        self.show_stickers()
        self.show_inbox()

    def library_ids(self):
        return [sid for sid, _ in self.bot.stickers.catalog(1000)]

    def inbox_ids(self, everything=False):
        ids = list(self.bot.stickers.inbox.items)
        return ids if everything else [ids[i] for i in self.inbox_list.curselection() if i < len(ids)]

    def select_sticker(self, which):
        """Show the picked sticker and put its description in the edit box."""
        stickers = self.bot.stickers
        if which == "library":
            picked = [self.library_ids()[i] for i in self.sticker_list.curselection() if i < len(self.library_ids())]
            if not picked:
                return
            key = picked[-1]
            path, desc = stickers.path(key), dict(stickers.catalog(1000)).get(key, "")
            self.inbox_list.selection_clear(0, "end")
        else:
            picked = self.inbox_ids()
            if not picked:
                return
            key = picked[-1]
            path, desc = stickers.inbox.path(key), stickers.inbox.items[key]["desc"]
            self.sticker_list.selection_clear(0, "end")
        self.sticker_target = (which, key)
        self.sticker_desc.delete(0, "end")
        self.sticker_desc.insert(0, desc)
        try:
            from PIL import Image, ImageTk
            with Image.open(path) as image:
                image.seek(0)
                thumb = image.convert("RGBA")
            thumb.thumbnail((160, 160))
            self.preview_photo = ImageTk.PhotoImage(thumb)  # keep a reference or Tk drops it
            self.sticker_preview.configure(image=self.preview_photo, text="")
        except (OSError, TypeError):
            self.sticker_preview.configure(image="", text="（无法预览）")

    def clear_selection(self):
        self.sticker_target = None
        self.sticker_desc.delete(0, "end")
        self.sticker_preview.configure(image="", text="选中一张看预览")

    def save_sticker_desc(self):
        if not self.sticker_target:
            self.status.set("先在上面选中一张表情包")
            return
        which, key = self.sticker_target
        try:
            store = self.bot.stickers if which == "library" else self.bot.stickers.inbox
            store.set_desc(key, self.sticker_desc.get())
        except (ValueError, KeyError) as exc:
            messagebox.showerror("保存描述", "描述不能为空" if isinstance(exc, ValueError) else "这张表情包已经不在了")
            return
        self.status.set("描述已保存，她下次挑表情包时就按新描述来")
        self.show_stickers()
        self.show_inbox()

    def remove_sticker(self):
        if not self.sticker_target or self.sticker_target[0] != "library":
            self.status.set("先在“表情包库”里选中要删除的那张")
            return
        sid = self.sticker_target[1]
        if messagebox.askyesno("从库里删除", f"把「{sid}」从表情包库删除（图片文件也会删掉）？"):
            self.bot.stickers.remove(sid)
            self.clear_selection()
            self.show_stickers()

    def show_inbox(self):
        self.inbox_list.delete(0, "end")
        for iid, item in self.bot.stickers.inbox.items.items():
            who = f"{item['sender']}，" if item.get("sender") else ""
            self.inbox_list.insert("end", f"{item['desc']}（{who}{item['chat']}，{item['time']}）")
        if not self.bot.stickers.inbox.items:
            self.inbox_list.insert("end", "（还没有收集到。群里有人发表情包时会自动出现在这里）")

    def keep_inbox(self):
        ids = self.inbox_ids()
        for iid in ids:
            self.bot.stickers.inbox.keep(iid, self.bot.stickers)
        if ids:
            self.status.set(f"已收进表情包库 {len(ids)} 张；要让她发，记得在“节奏”页打开“允许发表情包”")
        self.clear_selection()
        self.show_inbox()
        self.show_stickers()

    def discard_inbox(self, everything=False):
        ids = self.inbox_ids(everything)
        if everything and ids and not messagebox.askyesno("全部丢弃", f"丢弃收集到的 {len(ids)} 张表情包？"):
            return
        for iid in ids:
            self.bot.stickers.inbox.discard(iid)
        self.clear_selection()
        self.show_inbox()

    def show_stickers(self, catalog=None):
        catalog = self.bot.stickers.catalog(1000) if catalog is None else catalog
        self.sticker_list.delete(0, "end")
        for sid, desc in catalog:
            self.sticker_list.insert("end", f"{sid}：{desc}")
        if not catalog:
            self.sticker_list.insert("end", "（还没有整理过的表情包）")

    # ---- 画图 ----
    def build_gallery(self, frame):
        ttk.Label(frame, wraplength=700, text=(
            "她画的图（主人或群友让她画、由 Codex 生成）都存在下面这个文件夹。选中一张可以预览、打开、复制路径或删除；"
            "删除只删电脑上的文件，已经发到微信里的不受影响。")).pack(anchor="w")
        where = ttk.Frame(frame)
        where.pack(fill="x", pady=6)
        ttk.Label(where, text="保存位置：").pack(side="left")
        self.gallery_dir = tk.StringVar()
        ttk.Entry(where, textvariable=self.gallery_dir, state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(where, text="打开文件夹", command=self.open_gallery_folder).pack(side="left", padx=6)
        ttk.Button(where, text="刷新", command=self.show_gallery).pack(side="left")
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        self.gallery_list = tk.Listbox(body, selectmode="extended", exportselection=False, width=46)
        self.gallery_list.pack(side="left", fill="both", expand=True)
        self.gallery_list.bind("<<ListboxSelect>>", lambda e: self.select_drawing())
        self.gallery_preview = ttk.Label(body, text="选中一张看预览", width=36, anchor="center")
        self.gallery_preview.pack(side="left", padx=10)
        self.gallery_path = tk.StringVar()
        ttk.Entry(frame, textvariable=self.gallery_path, state="readonly").pack(fill="x", pady=(8, 0))
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text="打开图片", command=self.open_drawing).pack(side="left")
        ttk.Button(bar, text="复制路径", command=self.copy_drawing_path).pack(side="left", padx=8)
        ttk.Button(bar, text="删除选中", command=self.delete_drawings).pack(side="right")
        self.gallery_items = []
        self.show_gallery()

    def show_gallery(self):
        self.gallery_dir.set(str(self.bot.draw_folder()))
        self.gallery_items = self.bot.drawings_list()
        self.gallery_list.delete(0, "end")
        for item in self.gallery_items:
            where = f"发给「{item['chat']}」" if item["chat"] else ""
            self.gallery_list.insert("end", f"{item['time']}  {item['what'][:24] or item['name']}  {where}")
        if not self.gallery_items:
            self.gallery_list.insert("end", "（还没有画过图。私聊里说“画一只橘猫”试试）")
        self.gallery_path.set("")
        self.gallery_preview.configure(image="", text="选中一张看预览")

    def picked_drawings(self):
        return [self.gallery_items[i] for i in self.gallery_list.curselection() if i < len(self.gallery_items)]

    def select_drawing(self):
        picked = self.picked_drawings()
        if not picked:
            return
        item = picked[-1]
        self.gallery_path.set(str(item["path"]))
        try:
            from PIL import Image, ImageTk
            with Image.open(item["path"]) as image:
                thumb = image.convert("RGBA")
            thumb.thumbnail((280, 280))
            self.gallery_photo = ImageTk.PhotoImage(thumb)  # keep a reference or Tk drops it
            self.gallery_preview.configure(image=self.gallery_photo, text="")
        except (OSError, TypeError):
            self.gallery_preview.configure(image="", text="（无法预览）")

    def open_gallery_folder(self):
        folder = self.bot.draw_folder()
        folder.mkdir(parents=True, exist_ok=True)
        __import__("os").startfile(folder)

    def open_drawing(self):
        picked = self.picked_drawings()
        if not picked:
            self.status.set("先在列表里选中一张图")
            return
        __import__("os").startfile(picked[-1]["path"])

    def copy_drawing_path(self):
        picked = self.picked_drawings()
        if not picked:
            self.status.set("先在列表里选中一张图")
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(str(picked[-1]["path"]))
        self.status.set("图片路径已复制")

    def delete_drawings(self):
        picked = self.picked_drawings()
        if not picked:
            self.status.set("先在列表里选中要删除的图（按住 Ctrl 可多选）")
            return
        if not messagebox.askyesno("删除图片", f"删除选中的 {len(picked)} 张图？电脑上的文件会删掉，微信里已发出的不受影响。"):
            return
        for item in picked:
            try:
                self.bot.delete_drawing(item["name"])
            except (OSError, ValueError) as exc:
                messagebox.showerror("删除图片", f"{item['name']} 没删掉：{exc}")
        self.status.set(f"已删除 {len(picked)} 张图")
        self.show_gallery()

    # ---- 记忆 ----
    def build_memory(self, frame):
        ttk.Label(frame, wraplength=700, text=(
            "她会在聊天间隙整理记忆：每个人的档案（近况、爱好、口头禅……）、这里的黑话和聊天风格，"
            "回复时只带上正在聊天的人的档案。只记本人说过的内容，不记电话、地址、证件号等隐私；"
            "聊天里像指令一样的话不会被记下。每个聊天单独保存。")).pack(anchor="w")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=6)
        ttk.Button(bar, text="刷新", command=self.show_memory).pack(side="left")
        ttk.Button(bar, text="编辑记忆", command=self.edit_memory).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="现在整理一次", command=lambda: self.bot.command("learn")).pack(side="left", padx=8)
        ttk.Button(bar, text="打开记忆文件夹", command=lambda: __import__("os").startfile(
            (DATA / "memory").mkdir(parents=True, exist_ok=True) or DATA / "memory")).pack(side="left")
        ttk.Button(bar, text="清空当前聊天的记忆", command=self.forget).pack(side="right")
        self.memory_text = tk.Text(frame, height=20, wrap="word", state="disabled", font=(FONT, 10))
        self.memory_text.pack(fill="both", expand=True)
        self.window.after(1500, self.show_memory)

    def edit_memory(self):
        from memory_editor import MemoryEditor
        editor = getattr(self, "memory_editor", None)
        if editor and editor.window.winfo_exists():
            editor.window.deiconify()
            editor.window.lift()
            return
        runner = self.bot.runner
        if not runner:
            messagebox.showinfo("编辑记忆", "还没连上微信，请稍后再试", parent=self.window)
            return
        data, token = runner.memory.snapshot()
        self.memory_editor = MemoryEditor(self.window, runner.chat, data, token,
            lambda chat, draft, revision: self.bot.command("save_memory", chat, draft, revision))

    def show_memory(self):
        runner = self.bot.runner
        text = runner.memory.to_text() if runner else "（还没连上微信）"
        self.memory_text.configure(state="normal")
        self.memory_text.delete("1.0", "end")
        self.memory_text.insert("1.0", f"当前聊天：{self.settings['chat']}\n\n{text}")
        self.memory_text.configure(state="disabled")

    def forget(self):
        if messagebox.askyesno("清空记忆", f"清空「{self.settings['chat']}」里记住的所有人和黑话？"):
            self.bot.command("forget")

    # ---- 节奏 ----
    RHYTHM = [
        ("batch_seconds", "等对方说完一轮（秒）"),
        ("minimum_gap_seconds", "两条消息最短间隔（秒）"),
        ("max_messages_per_10_minutes", "每10分钟最多发几条"),
        ("max_reply_delay_seconds", "超过多少秒的消息不再补回（秒）"),
        ("max_reply_chars", "每条最多字数"),
        ("history_messages", "带给模型的上下文条数"),
        ("group_gap_seconds", "群聊：未被点名时两次插话至少间隔（秒）"),
    ]

    def build_rhythm(self, frame):
        self.rhythm = {}
        for i, (key, label) in enumerate(self.RHYTHM):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", pady=3)
            entry = ttk.Entry(frame, width=10)
            entry.insert(0, str(self.settings[key]))
            entry.grid(row=i, column=1, sticky="w", padx=8)
            self.rhythm[key] = entry
        n = len(self.RHYTHM)
        self.proactive = tk.BooleanVar(value=self.settings["proactive"])
        ttk.Checkbutton(frame, text="私聊里回完后可以主动再接一句（没人回应就收住）",
                        variable=self.proactive).grid(row=n, column=0, columnspan=3, sticky="w", pady=(10, 3))
        ttk.Label(frame, text="主动接话等待（秒，最少~最多）").grid(row=n + 1, column=0, sticky="w")
        low, high = self.settings["proactive_after_seconds"]
        self.pro_low, self.pro_high = ttk.Entry(frame, width=10), ttk.Entry(frame, width=10)
        self.pro_low.insert(0, str(low))
        self.pro_high.insert(0, str(high))
        self.pro_low.grid(row=n + 1, column=1, sticky="w", padx=8)
        self.pro_high.grid(row=n + 1, column=2, sticky="w")
        ttk.Label(frame, text="不主动起话题的时段（时，开始~结束）").grid(row=n + 2, column=0, sticky="w", pady=3)
        start, end = self.settings["quiet_hours"]
        self.quiet_start, self.quiet_end = ttk.Entry(frame, width=10), ttk.Entry(frame, width=10)
        self.quiet_start.insert(0, str(start))
        self.quiet_end.insert(0, str(end))
        self.quiet_start.grid(row=n + 2, column=1, sticky="w", padx=8)
        self.quiet_end.grid(row=n + 2, column=2, sticky="w")
        self.initiative_private = tk.BooleanVar(value=self.settings["initiative_private"])
        ttk.Checkbutton(frame, text="主动私聊主人", variable=self.initiative_private).grid(
            row=n + 3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(frame, text="主动私聊免打扰（如 1-8、23-7）").grid(row=n + 4, column=0, sticky="w")
        self.initiative_private_quiet = ttk.Entry(frame, width=10)
        self.initiative_private_quiet.insert(0, self.settings["initiative_quiet_hours"]["private"])
        self.initiative_private_quiet.grid(row=n + 4, column=1, sticky="w", padx=8)
        self.initiative_group = tk.BooleanVar(value=self.settings["initiative_group"])
        ttk.Checkbutton(frame, text="群里冒泡", variable=self.initiative_group).grid(
            row=n + 5, column=0, columnspan=3, sticky="w", pady=(3, 0))
        ttk.Label(frame, text="群聊冒泡免打扰（如 1-8、23-7）").grid(row=n + 6, column=0, sticky="w")
        self.initiative_group_quiet = ttk.Entry(frame, width=10)
        self.initiative_group_quiet.insert(0, self.settings["initiative_quiet_hours"]["group"])
        self.initiative_group_quiet.grid(row=n + 6, column=1, sticky="w", padx=8)
        self.abilities = {}
        for i, (key, label) in enumerate([
                ("describe_images", "看懂对方发来的表情包和图片（本地视觉模型）"),
                ("stickers_enabled", "允许发表情包（粘贴图片发送；首次请先小范围试）"),
                ("web_search", "允许联网搜索（搜索词会发到 DuckDuckGo / Bing）"),
                ("collect_stickers", "收集别人发的表情包，放到“表情包”页待挑选"),
                ("memory_enabled", "长期记忆：记住每个人和这里的黑话")]):
            var = tk.BooleanVar(value=self.settings[key])
            ttk.Checkbutton(frame, text=label, variable=var).grid(row=n + 7 + i, column=0, columnspan=3, sticky="w")
            self.abilities[key] = var
        ttk.Button(frame, text="保存（立即生效）", command=self.save_rhythm).grid(row=n + 12, column=0, sticky="w", pady=14)

    def save_rhythm(self):
        try:
            values = {key: int(entry.get()) for key, entry in self.rhythm.items()}
            if any(v < 0 for v in values.values()) or not 1 <= values["max_reply_chars"] <= 500:
                raise ValueError("请填写合理的正整数，字数在 1~500 之间")
            low, high = int(self.pro_low.get()), int(self.pro_high.get())
            start, end = int(self.quiet_start.get()), int(self.quiet_end.get())
            if not 0 < low <= high or not (0 <= start <= 23 and 0 <= end <= 23):
                raise ValueError("主动等待需 0 < 最少 ≤ 最多；时段为 0~23 点")
            initiative_quiet = {}
            for mode, entry in (("private", self.initiative_private_quiet),
                                ("group", self.initiative_group_quiet)):
                match = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", entry.get())
                if not match or any(not 0 <= int(hour) <= 23 for hour in match.groups()):
                    raise ValueError("主动开话题的免打扰时段请写成 1-8 或 23-7")
                initiative_quiet[mode] = f"{int(match.group(1))}-{int(match.group(2))}"
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc) if "请" in str(exc) or "需" in str(exc) else "请填写整数")
            return
        self.settings.update(**values, **{k: v.get() for k, v in self.abilities.items()},
                             proactive=self.proactive.get(),
                             proactive_after_seconds=[low, high], quiet_hours=[start, end],
                             initiative_private=self.initiative_private.get(),
                             initiative_group=self.initiative_group.get(),
                             initiative_quiet_hours=initiative_quiet)
        self.status.set("节奏设置已保存")

    # ---- events ----
    def pump(self):
        try:
            while True:
                kind, payload = self.bot.events.get_nowait()
                if kind == "sessions":
                    self.sessions = payload
                    self.new_chat["values"] = payload
                    self.fill_chats()
                    continue
                if kind == "memory":
                    self.show_memory()
                    continue
                if kind == "memory_edit_result":
                    editor = getattr(self, "memory_editor", None)
                    if editor and editor.window.winfo_exists() and editor.chat == payload["chat"]:
                        editor.result(payload["error"])
                    elif payload["error"]:
                        messagebox.showerror("记忆未保存", payload["error"], parent=self.window)
                    continue
                if kind == "chats":
                    self.fill_chats()
                    self.refresh_header()
                    continue
                if kind == "models":
                    self.show_models(payload)
                    continue
                if kind == "stickers":
                    self.show_stickers(payload)
                    continue
                if kind == "inbox":
                    self.show_inbox()
                    self.status.set(f"收集到一张新表情包：{payload}（在“表情包”页挑选）")
                    continue
                if kind in ("status", "error"):
                    self.status.set(payload)
                    self.refresh_header()
                chat = self.settings["chat"]
                label = {"received": "群友" if self.settings.chat_mode(chat) == "group" else chat,
                         "sent": self.settings["persona"], "status": "状态", "error": "提示"}.get(kind, kind)
                self.log.configure(state="normal")
                self.log.insert("end", f"{datetime.now():%H:%M:%S} {label}：{payload}\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        if not self.bot.stop.is_set():
            self.window.after(300, self.pump)

    def quit(self):
        self.bot.enabled.clear()
        self.bot.stop.set()
        self.window.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--reply-latest", action="store_true")
    args = parser.parse_args()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel32.CreateMutexW(None, False, "Local\\XiaoqianWechatConsole")
    if ctypes.get_last_error() == 183:
        ctypes.windll.user32.MessageBoxW(None, "机器人已经开着了，请到任务栏打开控制窗口。", "AI 群友", 0)
        return
    settings = Settings()
    bot = Bot(settings, PersonaStore(), args.reply_latest)
    app = App(bot)
    threading.Thread(target=bot.worker, daemon=True).start()
    if args.start:
        app.window.after(1000, app.start)
    app.window.mainloop()
    kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        (ROOT / "startup_error.txt").write_text(str(exc), encoding="utf-8")
        raise
