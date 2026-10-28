"""Fail-closed social boundaries; no model text is a destination or a path."""
import hashlib
import json
import re
import threading
import time
import os
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

_LOCK = threading.RLock()
FORBIDDEN = re.compile(r'(?:[a-zA-Z]:|\\\\|file:|(?:^|[\s\[（(])(?:/|\.\.?[\\/]|~[\\/])|[\w.\-]+[\\/][\w.\-]+|[\[【]\s*(?:图片|表情|文件|视频|语音|image|file)\s*[:：]|!\[[^\]]*\]\(|\bhttps?://)', re.I)


def unsafe(text):
    return not isinstance(text, str) or bool(FORBIDDEN.search(text)) or '__SKIP__' in text


def chat_key(chat, mode):
    """Sanitizing/truncating display names can collide across private/groups."""
    return hashlib.sha256((str(mode)+'\0'+chat).encode()).hexdigest()


def zone(name):
    if name == 'Asia/Shanghai':
        return timezone(timedelta(hours=8))
    if name == 'UTC':
        return timezone.utc
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


@contextmanager
def process_lock(path):
    """Serialize budget read/reserve across accidental duplicate processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if not path.stat().st_size:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Budget:
    """Durable reservation immediately before one logical send (text + sticker).

    Reservations survive uncertain sends; do not retry. Thread and OS file locks
    protect the budget across processes. Invalid state denies sending.
    """
    def __init__(self, path, timezone_name='Asia/Shanghai'):
        self.path = Path(path)
        self.tz = zone(timezone_name)

    def reserve(self, chat, proactive, now=None):
        now = time.time() if now is None else now
        key = hashlib.sha256(chat.encode()).hexdigest()
        try:
            with _LOCK, process_lock(self.path.with_suffix('.lock')):
                state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
                events = state.get(key, [])
                if not isinstance(state, dict) or not isinstance(events, list):
                    return False
                if any(type(t) not in (float, int) for t in events):
                    return False
                if any(t > now for t in events):
                    return False
                events = [t for t in events if now - t < 3600]
                if proactive:
                    hour = datetime.fromtimestamp(now, self.tz).hour
                    if 1 <= hour < 8 or len(events) >= 12 or events and now-events[-1] < 180:
                        return False
                    state[key] = events + [now]
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    temp = self.path.with_suffix('.tmp')
                    temp.write_text(json.dumps(state, separators=(',', ':')), encoding='utf-8')
                    temp.replace(self.path)
                return True
        except (OSError, ValueError, TypeError, AttributeError):
            return False


def selected_sticker(library, sid):
    if not isinstance(sid, str) or not re.fullmatch(r'[\w\- ]{1,80}', sid):
        return None
    row = library.index.get(sid)
    if not isinstance(row, dict):
        return None
    name = row.get('file')
    if not isinstance(name, str) or Path(name).name != name or ':' in name or '\\' in name:
        return None
    root = library.folder.resolve()
    path = library.folder / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
        return None
    if path.suffix.lower() not in ('.png', '.gif', '.jpg', '.jpeg', '.webp') or path.stat().st_size > 5*1024*1024:
        return None
    return path


def disabled(*args, **kwargs):
    raise RuntimeError('social_capability_disabled')
