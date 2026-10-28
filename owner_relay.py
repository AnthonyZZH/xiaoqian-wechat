"""Owner-only, explicit destination relay. No agent or drawing capability."""
import json
import math
import re
import time
from pathlib import Path
from social_policy import _LOCK, process_lock, unsafe, chat_key

COMMAND = re.compile(r'^(?:(?:小千|你|请|请你|麻烦你|帮我|我让你|我让她)\s*)?(?:去|到|在)\s*(.{1,80}?)\s*(?:里|里面|中)?\s*(?:说|讲|发)\s*[:：]?\s*(.+)', re.S)
EVERYONE = re.compile(r'[@＠]\s*(?:所有人|全体成员|everyone|all)|全体成员|所有人', re.I)
# Owner lines worth asking the model about: anything that sounds like "say/send/tell over there".
def people_directory(chats, owner, data_dir):
    """Who she knows and where: group members from sender books and memory, plus short facts.

    Lets the model map a nickname the owner uses ("张哥") onto a real member to @.
    """
    data_dir = Path(data_dir)
    groups, facts, nicknames = {}, {}, {}
    for chat, info in chats.items():
        if not isinstance(info, dict) or chat == owner:
            continue
        mode = info.get('mode')
        names = []
        if mode == 'group':
            names += [r for r in info.get('roster', []) if isinstance(r, str)]
            try:
                book = json.loads((data_dir / 'senders' / (chat_key(chat, mode) + '.json')).read_text(encoding='utf-8'))
                for person in book if isinstance(book, list) else []:
                    names += [person.get('label')] if person.get('label') else []
                    names += [n for n in (person.get('names') or {}) if isinstance(n, str)]
            except (OSError, ValueError, AttributeError):
                pass
            try:
                # Whoever spoke recently, newest first: the sender book may not know them yet.
                rows = json.loads((data_dir / 'history' / (chat_key(chat, mode) + '.json')).read_text(encoding='utf-8'))
                rows = rows.get('history', rows) if isinstance(rows, dict) else rows
                names = [r.get('sender') for r in reversed(rows) if isinstance(r, dict) and r.get('role') == 'user'
                         and isinstance(r.get('sender'), str)] + names
            except (OSError, ValueError, AttributeError):
                pass
        try:
            memory = json.loads((data_dir / 'memory' / (chat_key(chat, mode) + '.json')).read_text(encoding='utf-8'))
            people = memory.get('people', {}) if isinstance(memory, dict) else {}
            jargon = memory.get('jargon', {}) if isinstance(memory, dict) else {}
        except (OSError, ValueError):
            people, jargon = {}, {}
        # Group slang often records nicknames ("张哥: 对Alex的昵称").
        for term, info in (jargon.items() if isinstance(jargon, dict) else []):
            meaning = info.get('meaning') if isinstance(info, dict) else info
            if isinstance(term, str) and isinstance(meaning, str):
                nicknames.setdefault(chat, {})[term[:20]] = meaning[:60]
        for name, person in people.items():
            if not isinstance(name, str) or not name:
                continue
            if mode == 'group':
                names.append(name)
            known = [str(f)[:30] for f in (person.get('facts') or [])[:12]] if isinstance(person, dict) else []
            if isinstance(person, dict) and person.get('impression'):
                known.append(str(person['impression'])[:40])
            if known:
                facts.setdefault(name, known)
        if mode == 'group':
            # The owner is never the one she is sent to find.
            names = [n for n in dict.fromkeys(n.strip() for n in names if n) if n and n != '群友' and n != owner][:80]
            if names:
                groups[chat] = names
    return {'groups': groups, 'facts': dict(list(facts.items())[:120]),
            'nicknames': {c: dict(list(n.items())[:40]) for c, n in nicknames.items()}}


def guess(line, choices):
    """The one chat whose name shares the longest run (2+ chars, '群' aside) with the line."""
    def overlap(name):
        name = re.sub(r'[群聊\s]', '', name)
        return max((n for n in range(len(name), 1, -1) for i in range(len(name) - n + 1) if name[i:i + n] in line), default=0)
    scores = {c: overlap(c) for c in choices}
    best = max(scores.values(), default=0)
    hits = [c for c, s in scores.items() if s == best]
    return hits[0] if best >= 2 and len(hits) == 1 else None

RELAY_HINT = re.compile(r'说|讲|发|告诉|转告|带.{0,2}话|传话|打招呼|问候|通知|问问')
FREE_REQUEST = re.compile(r'(?:请你|请|帮我|替我|麻烦你|麻烦|小千|让你|让她|你去).*(?:转告|带句话|带个话|传话|打招呼|问候|说|告诉|发|讲)', re.S)

def candidates(line, chats, owner):
    found = {}
    for chat, info in chats.items():
        if not isinstance(info,dict): continue
        names = [chat] + (info.get('aliases',[]) if isinstance(info.get('aliases',[]),list) else [])
        for name in names:
            if isinstance(name,str) and name and name in line:
                found.setdefault(chat,[]).append(name)
    if not found and re.search(r'群里|群聊|去群|到群', line):
        for chat,info in chats.items():
            if isinstance(info,dict) and info.get('mode') == 'group': found.setdefault(chat,[]).append('群')
    if len(found) != 1 or owner in found: return []
    return list(dict.fromkeys(next(iter(found.values()))))

def parse(line):
    if not isinstance(line, str): return None
    match = COMMAND.match(line.strip())
    if not match: return None
    return match.group(1).strip(' 「」“”\"'), match.group(2).strip()

def resolve(name, chats, owner):
    matches = set()
    if name in ('群', '群聊'):
        matches = {chat for chat, info in chats.items() if isinstance(info, dict) and info.get('mode') == 'group'}
        return next(iter(matches)) if len(matches) == 1 and owner not in matches else None
    for chat, info in chats.items():
        if not isinstance(info, dict): continue
        aliases = info.get('aliases', [])
        if not isinstance(aliases, list): aliases = []
        if name == chat or name in [a for a in aliases if isinstance(a, str)]:
            matches.add(chat)
    return next(iter(matches)) if len(matches) == 1 and owner not in matches else None

def valid_text(text):
    return isinstance(text, str) and bool(text.strip()) and len(text) <= 100 and not unsafe(text) and not EVERYONE.search(text) and '@' not in text and '＠' not in text

class RelayBudget:
    def __init__(self, path): self.path = Path(path)
    def reserve(self, now=None):
        now = time.time() if now is None else now
        try:
            with _LOCK, process_lock(self.path.with_suffix('.lock')):
                events = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else []
                if not isinstance(events, list) or any(type(t) not in (int, float) or not math.isfinite(t) or t > now or t < 0 for t in events): return False
                events = [t for t in events if now-t < 86400]
                if len(events) >= 40 or sum(now-t < 3600 for t in events) >= 10: return False
                events.append(now)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(json.dumps(events), encoding='utf-8')
                tmp.replace(self.path)
                return True
        except (OSError, ValueError, TypeError):
            return False
