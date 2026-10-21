"""Control window: pick the chat, persona and model; start/pause; close to stop completely."""
import argparse
import ctypes
import json
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parent

from brain import Brain
from lmstudio import LMStudio
from personas import TEMPLATE, Persona, PersonaStore
from settings import DATA, Settings
from stickers import StickerLibrary

FONT = "Microsoft YaHei UI"


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
        self.brain = Brain(settings, personas, self.stickers)
        self.lm = LMStudio(settings["lms_path"])
        self.enabled, self.stop = Enabled(), threading.Event()
        self.events, self.commands = queue.Queue(), queue.Queue()
        self.reply_latest = reply_latest
        self.log_lock = threading.Lock()
        self.runner = None
        self.runners = {}    # chat -> Runner, kept while switching so each chat keeps its place
        self.relays = []     # (chat, text) the owner asked her to say
        self.previews = {}   # chat -> last seen session-list preview, to notice news elsewhere
        self.since = 0.0     # when the current chat was opened
        self.scanning = None  # background sticker-labelling thread

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

    def runner_for(self, chat, reply_latest=False):
        from runner import Runner
        if chat not in self.runners:
            runner = Runner(self.settings, self.adapter, self.enabled, self.stop, self.emit,
                            self.brain, chat, reply_latest=reply_latest, stickers=self.stickers,
                            history_dir=self.data / "history", memory_dir=self.data / "memory")
            groups = lambda: [c for c, i in self.settings["chats"].items() if i["mode"] == "group"]
            # Relays need the multi-chat scheduler to carry them out.
            runner.relay_targets = lambda: groups() if self.settings["multi_chat"] else []
            runner.context = lambda: "\n".join(
                r.digest() for c, r in self.runners.items() if c in groups() and r.digest())
            self.runners[chat] = runner
        return self.runners[chat]

    def activate(self, chat, reply_latest=False):
        """Point the adapter and the active runner at `chat` (already open in WeChat)."""
        from personas import safe_name
        from senders import SenderBook
        mode = self.settings.chat_mode(chat)
        info = self.settings["chats"].get(chat, {})
        book = SenderBook(self.data / "senders" / f"{safe_name(chat)}.json", info.get("roster")) \
            if mode == "group" else None
        self.adapter.set_target(chat, mode, book, self.settings.get("group_layout"))
        self.runner = self.runner_for(chat, reply_latest)
        self.since = time.monotonic()
        if self.settings["chat"] != chat:
            self.settings.update(chat=chat)
            self.emit("chats", None)

    def go(self, chat, reply_latest=False):
        if self.runner:
            # Her own last message here changed the preview; that is not news to come back for.
            self.previews[self.runner.chat] = self.adapter.session_states().get(self.runner.chat)
        self.adapter.open_chat(chat)
        self.activate(chat, reply_latest)

    def watch(self):
        """Multi-chat mode: follow manual switches, carry out relays, visit chats with news.

        Leaves a chat only when nothing there is waiting for a reply, and after a
        short dwell so a lively chat is not abandoned mid-conversation.
        """
        chats = self.settings["chats"]
        title, _ = self.adapter.current()
        if title in chats and title != self.runner.chat:
            self.activate(title)
        relay = self.runner.take_relay()
        if relay:
            self.relays.append(relay)
        if not self.previews:
            # Baseline right away, so news arriving during the first dwell is not absorbed.
            self.previews = dict(self.adapter.session_states())
        if self.runner.pending:
            return
        if self.relays:
            chat, text = self.relays.pop(0)
            self.emit("status", f"去「{chat}」转告：{text}")
            self.go(chat)
            self.runner.step()  # read the group first so her line lands in context
            self.runner.say(text)
            return
        if time.monotonic() - self.since < 8:
            return
        states = self.adapter.session_states()
        self.previews[self.runner.chat] = states.get(self.runner.chat)
        news = [c for c in chats if c != self.runner.chat and c in states
                and states[c] != self.previews.get(c)]
        if not news:
            return
        owner = self.settings["owner"]
        chat = owner if owner in news else news[0]
        self.previews[chat] = states[chat]
        self.emit("status", f"「{chat}」有新消息，切过去看看")
        self.go(chat, reply_latest=chat not in self.runners)

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
                self.enabled.set()
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
                    if self.enabled.is_set():
                        try:
                            self.runner.step()
                            if self.settings["multi_chat"]:
                                self.watch()
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
        self.fill_chats()

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
        self.abilities = {}
        for i, (key, label) in enumerate([
                ("describe_images", "看懂对方发来的表情包和图片（本地视觉模型）"),
                ("stickers_enabled", "允许发表情包（粘贴图片发送；首次请先小范围试）"),
                ("web_search", "允许联网搜索（搜索词会发到 DuckDuckGo / Bing）"),
                ("collect_stickers", "收集别人发的表情包，放到“表情包”页待挑选"),
                ("memory_enabled", "长期记忆：记住每个人和这里的黑话")]):
            var = tk.BooleanVar(value=self.settings[key])
            ttk.Checkbutton(frame, text=label, variable=var).grid(row=n + 3 + i, column=0, columnspan=3, sticky="w")
            self.abilities[key] = var
        ttk.Button(frame, text="保存（立即生效）", command=self.save_rhythm).grid(row=n + 8, column=0, sticky="w", pady=14)

    def save_rhythm(self):
        try:
            values = {key: int(entry.get()) for key, entry in self.rhythm.items()}
            if any(v < 0 for v in values.values()) or not 1 <= values["max_reply_chars"] <= 500:
                raise ValueError("请填写合理的正整数，字数在 1~500 之间")
            low, high = int(self.pro_low.get()), int(self.pro_high.get())
            start, end = int(self.quiet_start.get()), int(self.quiet_end.get())
            if not 0 < low <= high or not (0 <= start <= 23 and 0 <= end <= 23):
                raise ValueError("主动等待需 0 < 最少 ≤ 最多；时段为 0~23 点")
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc) if "请" in str(exc) or "需" in str(exc) else "请填写整数")
            return
        self.settings.update(**values, **{k: v.get() for k, v in self.abilities.items()},
                             proactive=self.proactive.get(),
                             proactive_after_seconds=[low, high], quiet_hours=[start, end])
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
