"""Owner-facing forms for editing a snapshot of one chat's long-term memory."""
import copy
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from memory import Memory


class MemoryEditor:
    def __init__(self, parent, chat, data, token, submit):
        self.chat, self.token, self.submit = chat, token, submit
        self.original, self.draft = copy.deepcopy(data), copy.deepcopy(data)
        self.selected = {"people": None, "jargon": None}
        self.pending = False
        self.window = tk.Toplevel(parent)
        self.window.title(f"编辑记忆 · {chat}")
        self.window.geometry("760x570")
        self.window.minsize(620, 450)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        ttk.Label(self.window, text=f"正在编辑「{chat}」的记忆", font=("Microsoft YaHei UI", 12, "bold")).pack(
            anchor="w", padx=14, pady=(12, 4))
        ttk.Label(self.window, text="修改后点击保存，下一次回复会使用新记忆。小千以后仍会根据聊天继续更新。",
                  wraplength=700).pack(anchor="w", padx=14, pady=(0, 8))
        tabs = ttk.Notebook(self.window)
        tabs.pack(fill="both", expand=True, padx=14)
        self.lists, self.fields, self.keys = {}, {}, {}
        for kind, title in (("people", "人物档案"), ("jargon", "黑话 / 常用说法")):
            frame = ttk.Frame(tabs, padding=10)
            tabs.add(frame, text=title)
            left = ttk.Frame(frame)
            left.pack(side="left", fill="y", padx=(0, 12))
            listing = tk.Listbox(left, width=20, exportselection=False)
            listing.pack(fill="both", expand=True)
            self.lists[kind] = listing
            listing.bind("<<ListboxSelect>>", lambda event, k=kind: self.select(k))
            bar = ttk.Frame(left)
            bar.pack(fill="x", pady=(8, 0))
            ttk.Button(bar, text="新增", command=lambda k=kind: self.add(k)).pack(side="left")
            ttk.Button(bar, text="删除", command=lambda k=kind: self.delete(k)).pack(side="left", padx=6)
            right = ttk.Frame(frame)
            right.pack(side="left", fill="both", expand=True)
            fields = self.fields[kind] = {}
            ttk.Label(right, text="昵称（与聊天中的名字一致）" if kind == "people" else "说法（最多12字）").pack(anchor="w")
            fields["name"] = ttk.Entry(right)
            fields["name"].pack(fill="x", pady=(3, 10))
            if kind == "people":
                ttk.Label(right, text="对这个人的印象（最多60字）").pack(anchor="w")
                fields["impression"] = ttk.Entry(right)
                fields["impression"].pack(fill="x", pady=(3, 10))
                ttk.Label(right, text="记住的事情（每行一条，最多12条，每条40字）").pack(anchor="w")
                fields["facts"] = ScrolledText(right, wrap="word", height=10)
                fields["facts"].pack(fill="both", expand=True, pady=(3, 0))
            else:
                ttk.Label(right, text="含义（最多40字）").pack(anchor="w")
                fields["meaning"] = ScrolledText(right, wrap="word", height=10)
                fields["meaning"].pack(fill="both", expand=True, pady=(3, 0))
            self.refresh_list(kind)
        style_tab = ttk.Frame(tabs, padding=12)
        tabs.add(style_tab, text="聊天风格")
        ttk.Label(style_tab, text="这里通常怎样聊天（最多80字）").pack(anchor="w")
        self.style = ScrolledText(style_tab, wrap="word", height=8)
        self.style.pack(fill="both", expand=True, pady=8)
        self.style.insert("1.0", self.draft["style"])
        bar = ttk.Frame(self.window, padding=12)
        bar.pack(fill="x")
        self.message = tk.StringVar(value="尚未保存；删除条目后也需要点击保存。")
        ttk.Label(bar, textvariable=self.message, wraplength=500).pack(side="left")
        self.save_button = ttk.Button(bar, text="保存修改", command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(bar, text="取消", command=self.close).pack(side="right", padx=8)

    def refresh_list(self, kind, choose=None):
        self.keys[kind] = list(self.draft[kind])
        listing = self.lists[kind]
        listing.delete(0, "end")
        for name in self.keys[kind]:
            listing.insert("end", name)
        name = choose if choose in self.draft[kind] else next(iter(self.draft[kind]), None)
        self.selected[kind] = name
        if name is not None:
            listing.selection_set(self.keys[kind].index(name))
        self.fill(kind)

    def fill(self, kind):
        name = self.selected[kind]
        row = self.draft[kind].get(name, {})
        for key, widget in self.fields[kind].items():
            widget.configure(state="normal")
            value = (name or "") if key == "name" else "\n".join(row.get("facts", [])) if key == "facts" else row.get(key, "")
            if isinstance(widget, tk.Text):
                widget.delete("1.0", "end")
                widget.insert("1.0", value)
            else:
                widget.delete(0, "end")
                widget.insert(0, value)
            if name is None:
                widget.configure(state="disabled")

    def collect(self, kind):
        old = self.selected[kind]
        if old is None:
            return
        fields = self.fields[kind]
        name = fields["name"].get().strip()
        if not name:
            raise ValueError("昵称或说法不能为空；不需要的条目请点“删除”")
        if name != old and name in self.draft[kind]:
            raise ValueError(f"「{name}」已存在，请换一个名字")
        row = copy.deepcopy(self.draft[kind][old])
        if kind == "people":
            row["impression"] = fields["impression"].get()
            row["facts"] = [s.strip() for s in fields["facts"].get("1.0", "end-1c").splitlines() if s.strip()]
        else:
            row["meaning"] = fields["meaning"].get("1.0", "end-1c")
        if name != old:
            self.draft[kind] = {name if k == old else k: row if k == old else v for k, v in self.draft[kind].items()}
            self.selected[kind] = name
        else:
            self.draft[kind][old] = row

    def select(self, kind):
        selection = self.lists[kind].curselection()
        if not selection:
            return
        choice = self.keys[kind][selection[0]]
        if choice == self.selected[kind]:
            return
        try:
            self.collect(kind)
        except ValueError as exc:
            messagebox.showerror("请检查内容", str(exc), parent=self.window)
            self.lists[kind].selection_clear(0, "end")
            old = self.selected[kind]
            if old in self.keys[kind]:
                self.lists[kind].selection_set(self.keys[kind].index(old))
            return
        self.refresh_list(kind, choice)

    def add(self, kind):
        if self.pending:
            return
        try:
            self.collect(kind)
        except ValueError as exc:
            messagebox.showerror("请检查内容", str(exc), parent=self.window)
            return
        name = simpledialog.askstring("新增人物" if kind == "people" else "新增常用说法",
                                      "填写昵称" if kind == "people" else "填写说法", parent=self.window)
        if name is None:
            return
        name = name.strip()
        if not name or name in self.draft[kind]:
            messagebox.showerror("无法新增", "名字不能为空，也不能重复", parent=self.window)
            return
        self.draft[kind][name] = {"facts": [], "impression": "", "seen": 0} if kind == "people" else {"meaning": "", "count": 0}
        self.refresh_list(kind, name)

    def delete(self, kind):
        if self.pending:
            return
        name = self.selected[kind]
        if name is not None:
            del self.draft[kind][name]
            self.refresh_list(kind)

    def values(self):
        for kind in self.fields:
            self.collect(kind)
        self.draft["style"] = self.style.get("1.0", "end-1c")
        return self.draft

    def save(self):
        if self.pending:
            return
        try:
            data = Memory.validate_edit(self.values())
        except ValueError as exc:
            messagebox.showerror("请检查内容", str(exc), parent=self.window)
            return
        self.pending = True
        self.set_busy(True)
        self.message.set("正在保存，请稍候…")
        self.submit(self.chat, data, self.token)

    def set_busy(self, busy):
        def visit(parent):
            for widget in parent.winfo_children():
                if isinstance(widget, (ttk.Entry, ttk.Button, tk.Text, tk.Listbox)):
                    widget.configure(state="disabled" if busy else "normal")
                visit(widget)
        visit(self.window)
        if not busy:
            for kind in self.fields:
                if self.selected[kind] is None:
                    for widget in self.fields[kind].values():
                        widget.configure(state="disabled")

    def result(self, error=None):
        if error:
            self.pending = False
            self.set_busy(False)
            self.message.set("保存失败，你的修改仍保留在窗口中。")
            messagebox.showerror("记忆未保存", error, parent=self.window)
        else:
            self.window.destroy()

    def close(self):
        if self.pending:
            return
        try:
            changed = self.values() != self.original
        except ValueError:
            changed = True
        if changed and not messagebox.askyesno("放弃修改", "还有未保存的修改，要放弃吗？", parent=self.window):
            return
        self.window.destroy()
