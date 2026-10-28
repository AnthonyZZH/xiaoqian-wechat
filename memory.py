"""Long-term memory per chat: who people are, the group's slang, and how the group talks.

Everything stored here is later pasted into prompts, so writes are filtered: no
instruction-like text (persistent prompt injection) and nothing that looks like
private identifiers.
"""
from __future__ import annotations

import difflib
import copy
import hashlib
import json
import re
import shutil
import threading
import time
from functools import wraps
from pathlib import Path

MAX_FACTS, MAX_PEOPLE, MAX_JARGON, MAX_LEN = 12, 200, 60, 40
# Text that tries to steer the bot rather than describe a person or a word.
STEERING = re.compile(r"忽略|无视|提示词|系统提示|规则|指令|你必须|你现在是|你要扮演|扮演|人设|"
                      r"prompt|system|ignore|instruction", re.I)
# Phone numbers, ID/bank numbers, emails, passwords.
PRIVATE = re.compile(r"\d{7,}|@[\w-]+\.\w|密码|身份证|银行卡|卡号|住址|门牌")


def clean(text, limit=MAX_LEN):
    text = " ".join(str(text or "").split())[:limit]
    if not text or STEERING.search(text) or PRIVATE.search(text):
        return ""
    return text


def similar(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.7


def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


def revision(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class Memory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        self.people = data.get("people", {})  # name -> {"facts": [...], "impression": str, "seen": ts}
        self.jargon = data.get("jargon", {})  # term -> {"meaning": str, "count": int}
        self.style = data.get("style", "")

    @locked
    def snapshot(self):
        """Return an isolated editing draft and a token for detecting newer learning."""
        data = copy.deepcopy({"people": self.people, "jargon": self.jargon, "style": self.style})
        return data, revision(data)

    @staticmethod
    def validate_edit(data):
        """Validate owner edits without silently truncating or dropping entered text."""
        if not isinstance(data, dict) or set(data) != {"people", "jargon", "style"}:
            raise ValueError("记忆内容格式不正确，请重新打开编辑窗口")
        result = copy.deepcopy(data)
        def text(value, limit, label, empty=True):
            if not isinstance(value, str):
                raise ValueError(f"{label}必须是文字")
            value = " ".join(value.split())
            if (not value and not empty) or len(value) > limit:
                raise ValueError(f"{label}请填写 1～{limit} 字" if not empty else f"{label}最多 {limit} 字")
            if value and not clean(value, limit):
                raise ValueError(f"{label}包含隐私或指令性内容，请改为描述性的记忆")
            return value
        if not isinstance(result["people"], dict) or len(result["people"]) > MAX_PEOPLE:
            raise ValueError(f"人物最多 {MAX_PEOPLE} 个")
        people = {}
        for name, person in result["people"].items():
            name = text(name, 30, "人物昵称", False)
            if name in people or not isinstance(person, dict):
                raise ValueError("人物昵称重复或档案格式不正确")
            facts = person.get("facts", [])
            if not isinstance(facts, list) or len(facts) > MAX_FACTS:
                raise ValueError(f"「{name}」最多 {MAX_FACTS} 条记忆，每行一条")
            seen = person.get("seen", 0)
            if not isinstance(seen, (int, float)):
                raise ValueError(f"「{name}」的档案格式不正确")
            people[name] = {"facts": [text(f, MAX_LEN, f"「{name}」的记忆", False) for f in facts],
                            "impression": text(person.get("impression", ""), 60, f"「{name}」的印象"),
                            "seen": seen}
        if not isinstance(result["jargon"], dict) or len(result["jargon"]) > MAX_JARGON:
            raise ValueError(f"常用说法最多 {MAX_JARGON} 个")
        jargon = {}
        for term, entry in result["jargon"].items():
            term = text(term, 12, "常用说法", False)
            if term in jargon or not isinstance(entry, dict):
                raise ValueError("常用说法重复或格式不正确")
            count = entry.get("count", 0)
            if not isinstance(count, int) or count < 0:
                raise ValueError("常用说法的次数不正确")
            jargon[term] = {"meaning": text(entry.get("meaning", ""), MAX_LEN, f"「{term}」的含义", False),
                            "count": count}
        return {"people": people, "jargon": jargon, "style": text(result["style"], 80, "聊天风格")}

    @locked
    def replace(self, data, expected_revision):
        """Save a complete owner edit, with conflict detection and a recoverable backup."""
        current, token = self.snapshot()
        if token != expected_revision:
            raise ValueError("记忆在编辑期间已有更新。请先复制保留你的修改，再重新打开编辑窗口")
        edited = self.validate_edit(data)
        if edited == current:
            return False
        if self.path.exists():
            disk = json.loads(self.path.read_text(encoding="utf-8"))
            if revision(disk) != token:
                raise ValueError("记忆文件已被其他程序修改，请重新打开程序后再编辑")
            backups = self.path.parent / "backups"
            backups.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.path, backups / f"{self.path.stem}_{time.time_ns()}.json")
        self.people, self.jargon, self.style = edited["people"], edited["jargon"], edited["style"]
        try:
            self.save()
        except Exception:
            self.people, self.jargon, self.style = current["people"], current["jargon"], current["style"]
            raise
        return True

    @locked
    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"people": self.people, "jargon": self.jargon, "style": self.style},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    @locked
    def clear(self):
        self.people, self.jargon, self.style = {}, {}, ""
        self.save()

    @locked
    def apply(self, update: dict, known_names=()) -> int:
        """Merge a learning result. Returns how many items changed.

        Only people who actually spoke (known_names) can get a profile, so the
        model cannot invent members or be talked into profiling outsiders.
        """
        changed = 0
        known = set(known_names)
        for row in update.get("people") or []:
            name = " ".join(str(row.get("name") or "").split())[:30]
            if not name or name not in known:
                continue
            person = self.people.setdefault(name, {"facts": [], "impression": "", "seen": 0})
            for old in row.get("forget") or []:
                old = str(old)
                before = len(person["facts"])
                person["facts"] = [f for f in person["facts"] if not similar(f, old)]
                changed += before - len(person["facts"])
            for fact in row.get("facts") or []:
                fact = clean(fact)
                if not fact:
                    continue
                person["facts"] = [f for f in person["facts"] if not similar(f, fact)] + [fact]
                person["facts"] = person["facts"][-MAX_FACTS:]
                changed += 1
            impression = clean(row.get("impression"), 60)
            if impression and impression != person["impression"]:
                person["impression"] = impression
                changed += 1
            person["seen"] = time.time()
        for row in update.get("jargon") or []:
            term = clean(row.get("term"), 12)
            meaning = clean(row.get("meaning"))
            if term and meaning:
                entry = self.jargon.setdefault(term, {"meaning": meaning, "count": 0})
                entry["meaning"], entry["count"] = meaning, entry["count"] + 1
                changed += 1
        style = clean(update.get("style"), 80)
        if style and style != self.style:
            self.style = style
            changed += 1
        self.trim()
        if changed:
            self.save()
        return changed

    @locked
    def trim(self):
        if len(self.people) > MAX_PEOPLE:
            keep = sorted(self.people, key=lambda n: self.people[n]["seen"], reverse=True)[:MAX_PEOPLE]
            self.people = {n: self.people[n] for n in keep}
        if len(self.jargon) > MAX_JARGON:
            keep = sorted(self.jargon, key=lambda t: self.jargon[t]["count"], reverse=True)[:MAX_JARGON]
            self.jargon = {t: self.jargon[t] for t in keep}

    @locked
    def prompt_block(self, names, max_people=6, max_jargon=20) -> str:
        """What she 'remembers', limited to the people in the current conversation."""
        lines = []
        people = [n for n in dict.fromkeys(names) if n in self.people][:max_people]
        for name in people:
            p = self.people[name]
            facts = "；".join(p["facts"][-6:])
            text = "，".join(x for x in (p["impression"], facts) if x)
            if text:
                lines.append(f"- {name}：{text}")
        terms = sorted(self.jargon, key=lambda t: self.jargon[t]["count"], reverse=True)[:max_jargon]
        parts = []
        if lines:
            parts.append("【你记得的人（来自以前的聊天，可能过时，别生硬地复述）】\n" + "\n".join(lines))
        if terms:
            parts.append("【这里的常用说法】" + "；".join(f"{t}＝{self.jargon[t]['meaning']}" for t in terms))
        if self.style:
            parts.append(f"【这里的聊天风格】{self.style.rstrip('。.！!')}。可以自然地融入，但保持你自己的人设。")
        return "\n".join(parts)

    @locked
    def to_text(self) -> str:
        """Readable dump for the control window."""
        out = [f"聊天风格：{self.style or '（还没学到）'}", "", "人物档案："]
        for name, p in sorted(self.people.items(), key=lambda kv: -kv[1]["seen"]):
            out.append(f"· {name}：{p['impression']}")
            out.extend(f"    - {f}" for f in p["facts"])
        if not self.people:
            out.append("（还没有）")
        out += ["", "黑话 / 常用说法："]
        out.extend(f"· {t}：{e['meaning']}（{e['count']}次）"
                   for t, e in sorted(self.jargon.items(), key=lambda kv: -kv[1]["count"]))
        if not self.jargon:
            out.append("（还没有）")
        return "\n".join(out)


    @locked
    def social_retrieve(self, query):
        """At most five locally stored facts, no global or other-chat material."""
        items = []
        for name, row in self.people.items():
            items.extend(name+': '+str(v) for v in row.get('facts', []))
            if row.get('impression'):
                items.append(name+': '+str(row['impression']))
        items.extend(str(k)+': '+str(v.get('meaning','')) for k,v in self.jargon.items())
        # Chinese character overlap is deterministic and does not need packages.
        terms = set(str(query)) - set(' ，。！？的了是我你他她')
        ranked = sorted(enumerate(items), key=lambda p: (-len(terms & set(p[1])), p[0]))
        return [text[:200] for _,text in ranked if terms & set(text)][:5]
