"""Exact owner-directed recipients and a stable, per-chat incoming work queue."""
import re


def preview_text(value, chat=""):
    """Extract the message preview, excluding a session title and timestamp."""
    if isinstance(value, dict):
        value = value.get("preview", "")
    lines = str(value or "").splitlines()
    if len(lines) > 1 and lines[0].strip() == chat:
        lines = lines[1:]
    lines = [s.strip() for s in lines if s.strip() and not re.fullmatch(r"\d{1,2}:\d{2}|昨天|星期[一二三四五六日天]", s.strip())]
    return "\n".join(lines)


def recipients(messages, known=(), groups=()):
    """Only extract explicitly named recipients of a send/relay request.

    Known names help resolve punctuation-free commands; arbitrary quoted or
    delimited names work too. Pronouns and ambiguous 'the group' never become a
    guessed recipient. This function never substitutes a different contact.
    """
    out = []
    verbs = r"(?:发(?:送)?(?:个|一条|条)?(?:消息|信息|微信|句话)|发一句|发送|说(?:一声|一下)?|转告|通知)"
    for text in messages:
        # Negated requests must not authorize contact with another person.
        if re.search(r"(?:不要|别再?|不用|不许|不能).{0,20}(?:发|说|转告|通知)", text):
            continue
        found = []
        for name in sorted(known, key=len, reverse=True):
            if re.search(r"(?:给|跟|向|对|去)\s*[「“\"']?" + re.escape(name) + r"[」”\"']?\s*" + verbs, text) \
                    or re.search(r"(?:转告|通知)\s*[「“\"']?" + re.escape(name) + r"(?:[」”\"']|[，,:：\s]|说)", text):
                found.append(name)
        pattern = r"(?:给|跟|向|对|去)\s*(?:[「“\"']([^」”\"'\n]{1,80})[」”\"']|([^，。！？,:：\n]{1,40}?))\s*" + verbs
        for match in re.finditer(pattern, text):
            name = (match.group(1) or match.group(2)).strip()
            if any(name.endswith(n) for n in found):
                continue
            name = re.sub(r"^(?:联系人|好友|群聊)\s*", "", name).strip()
            if name in ("群里", "群", "那个群", "这个群"):
                if len(groups) == 1:
                    found.append(next(iter(groups)))
            elif name and name not in ("他", "她", "他们", "她们", "对方", "我", "自己", "那个人") \
                    and not re.search(r"[、]|(?:帮我|让你|请你)", name):
                found.append(name)
        for match in re.finditer(r"(?:转告|通知)\s*[「“\"']([^」”\"'\n]{1,80})[」”\"']", text):
            found.append(match.group(1).strip())
        for name in found:
            if name not in out:
                out.append(name)
    return out


class ChatQueue:
    """Keep every changed chat until its work is acknowledged, with FIFO ties."""
    def __init__(self):
        self.observed = {}
        self.waiting = {}
        self.sequence = 0

    def observe(self, states, chats):
        for chat, value in states.items():
            if chat not in chats:
                continue
            old = self.observed.get(chat)
            if old is not None and value != old:
                self.enqueue(chat, old, value)
            elif old is None and isinstance(value, dict) and value.get("unread", 0):
                self.enqueue(chat, None, value)
            self.observed[chat] = value

    def enqueue(self, chat, baseline, latest):
        if chat not in self.waiting:
            self.sequence += 1
            self.waiting[chat] = {"baseline": baseline, "latest": latest, "sequence": self.sequence}
        else:
            self.waiting[chat]["latest"] = latest

    def priority(self, chat, chats, owner, runner=None):
        job = self.waiting.get(chat, {})
        preview = preview_text(job.get("latest", ""), chat)
        owner_in_group = bool(owner) and preview.startswith((owner + "：", owner + ":"))
        if runner is not None:
            owner_in_group |= any(getattr(row, "sender", "") == owner for row in runner.pending)
        if chat == owner or owner_in_group:
            return 0
        return 1 if chats.get(chat, {}).get("mode") == "private" else 2

    def next(self, chats, owner, runners=None, excluded=()):
        runners = runners or {}
        eligible = [chat for chat in self.waiting if chat in chats and chat not in excluded]
        return min(eligible, key=lambda c: (self.priority(c, chats, owner, runners.get(c)),
                                          self.waiting[c]["sequence"]), default=None)
