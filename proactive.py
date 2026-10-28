"""Persistent pacing for model-decided proactive conversations."""
from __future__ import annotations

import json
import math
import random
import threading
import time
from datetime import datetime, time as local_time, timedelta
from pathlib import Path
from social_policy import zone

# 2026 State Council holiday dates; weekends are always off-peak separately.
HOLIDAYS_2026 = frozenset(['2026-01-01','2026-01-02',
    *[f'2026-02-{d:02d}' for d in range(15,24)], '2026-04-06',
    *[f'2026-05-{d:02d}' for d in range(1,6)], '2026-06-19',
    '2026-09-25', *[f'2026-10-{d:02d}' for d in range(1,8)]])
SLEEP_WORDS = ('睡了','去睡','睡觉','晚安','困了','睡啦','睡咯','睡一会','睡会儿','上床','休息了')


class Initiative:
    """Keep per-chat initiative timers and unanswered limits."""

    def __init__(self, path, settings, clock=time.time, rng=None):
        self.path = Path(path)
        self.settings = settings
        self.clock = clock
        self.rng = rng or random.Random()
        self.lock = threading.RLock()
        self.sleep_until = 0
        self.states = self._load()

    def _setting(self, key, default=None):
        getter = getattr(self.settings, "get", None)
        return getter(key, default) if getter else default

    def _listed(self, chat):
        chats = self._setting("chats", {})
        return isinstance(chats, dict) and chat in chats

    @staticmethod
    def _mode(mode):
        return mode if mode in {"private", "group"} else None

    @staticmethod
    def _number(value):
        if isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.sleep_until = max(0, self._number(raw.get('sleep_until', 0)) or 0)
            raw = raw.get("chats", {})
        except (OSError, ValueError, TypeError, AttributeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        states = {}
        for chat, state in raw.items():
            if not isinstance(chat, str) or not isinstance(state, dict):
                continue
            mode = self._mode(state.get("mode"))
            last = self._number(state.get("last_activity"))
            due = self._number(state.get("next_at"))
            unanswered = state.get("unanswered")
            if mode and last is not None and due is not None and isinstance(unanswered, int) and unanswered >= 0:
                states[chat] = {"mode": mode, "last_activity": last,
                                "next_at": due, "unanswered": unanswered}
        return states

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(f"{self.path}.tmp")
        temp.write_text(json.dumps({"chats": self.states, "sleep_until": self.sleep_until}, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def _now(self, now):
        value = self.clock() if now is None else now
        value = self._number(value)
        if value is None:
            raise ValueError("now must be a finite timestamp")
        return value

    def _minutes(self, mode):
        key = "initiative_private_minutes" if mode == "private" else "initiative_group_minutes"
        default = [60, 480] if mode == "private" else [60, 240]
        value = self._setting(key, default)
        try:
            low, high = self._number(value[0]), self._number(value[1])
        except (TypeError, IndexError):
            low, high = default
        if low is None or high is None:
            low, high = default
        low, high = max(0.0, low), max(0.0, high)
        if low > high:
            low, high = high, low
        return low, high

    def _delay(self, mode):
        low, high = self._minutes(mode)
        return self.rng.uniform(low, high) * 60

    def _group_idle(self):
        value = self._number(self._setting("initiative_group_idle_minutes", 40))
        return max(0.0, value if value is not None else 40.0) * 60

    def _schedule(self, mode, now, after_activity=True, chat=None):
        if mode == 'private' and chat == self._setting('owner'):
            dt = datetime.fromtimestamp(now, zone('Asia/Shanghai'))
            periods = self._setting('initiative_peak_periods', [['09:00','12:00'],['14:00','18:00']])
            holidays = self._setting('initiative_peak_holidays', list(HOLIDAYS_2026))
            peak = dt.weekday() < 5 and dt.date().isoformat() not in holidays
            try:
                minute = dt.hour * 60 + dt.minute
                def minutes(s):
                    h, m = map(int, s.split(':'))
                    if not 0 <= h <= 23 or not 0 <= m <= 59: raise ValueError()
                    return h * 60 + m
                peak = peak and any(minutes(start) <= minute < minutes(end) for start, end in periods)
            except (TypeError, ValueError, AttributeError):
                peak = True
            key, default = ('initiative_peak_minutes', [15,30]) if peak else ('initiative_offpeak_minutes', [5,5])
            try:
                value = self._setting(key, default); low, high = self._number(value[0]), self._number(value[1])
                if low is None or high is None or low < 0 or high < low: raise ValueError()
            except (TypeError, ValueError, IndexError):
                low, high = default
            delay = self.rng.uniform(low, high) * 60
        else:
            delay = self._delay(mode)
        if mode == "group" and after_activity:
            delay += self._group_idle()
        return now + delay

    def _record_activity(self, chat, mode, now, unanswered=None, opening=False):
        if not self._listed(chat) or not self._mode(mode):
            return
        previous = self.states.get(chat, {})
        if previous.get("mode") != mode:
            previous = {}
        count = previous.get("unanswered", 0) if unanswered is None else unanswered
        if opening:
            count += 1
        self.states[chat] = {
            "mode": mode,
            "last_activity": now,
            "next_at": self._schedule(mode, now, chat=chat),
            "unanswered": count,
        }
        self._save()

    def heard(self, chat, mode, now=None):
        """Record somebody else's message and clear the unanswered count."""
        with self.lock:
            self._record_activity(chat, mode, self._now(now), unanswered=0)

    def owner_message(self, text, now=None):
        with self.lock:
            moment = self._now(now)
            self.sleep_until = moment + 5 * 3600 if any(word in str(text) for word in SLEEP_WORDS) else 0
            self._save()

    def owner_asleep(self, now=None):
        with self.lock:
            return self._now(now) < self.sleep_until

    def spoke(self, chat, mode, opening, now=None):
        """Record her own message; proactive openings increase unanswered."""
        with self.lock:
            self._record_activity(chat, mode, self._now(now), opening=bool(opening))

    def declined(self, chat, mode, now=None):
        """Reschedule a model-declined opening without increasing unanswered."""
        with self.lock:
            if not self._listed(chat) or not self._mode(mode):
                return
            current = self.states.get(chat)
            moment = self._now(now)
            if not current or current.get("mode") != mode:
                self._record_activity(chat, mode, moment, unanswered=0)
                return
            current["next_at"] = self._schedule(mode, moment, after_activity=False, chat=chat)
            self._save()

    def _eligible(self, chat, mode):
        if mode == "group":
            return bool(self._setting("initiative_group", True))
        if mode != "private" or not self._setting("initiative_private", True):
            return False
        extras = self._setting("initiative_private_chats", [])
        return chat == self._setting("owner") or (isinstance(extras, list) and chat in extras)

    def _maximum(self, mode):
        limits = self._setting("initiative_max_unanswered", {"private": 2, "group": 2})
        value = limits.get(mode, 2) if isinstance(limits, dict) else 2
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 2

    def _quiet_end(self, mode, now):
        quiet = self._setting("initiative_quiet_hours", {})
        value = quiet.get(mode, "") if isinstance(quiet, dict) else ""
        try:
            start_text, end_text = str(value).split("-", 1)
            start, end = int(start_text.strip()), int(end_text.strip())
        except (TypeError, ValueError):
            return None
        if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
            return None
        current = datetime.fromtimestamp(now)
        hour = current.hour
        inside = start <= hour < end if start < end else hour >= start or hour < end
        if not inside:
            return None
        end_date = current.date()
        if start > end and hour >= start:
            end_date += timedelta(days=1)
        return datetime.combine(end_date, local_time(end)).timestamp()

    def due(self, chats, now=None):
        """Return eligible chats whose opening time has arrived, oldest first."""
        with self.lock:
            moment = self._now(now)
            changed = False
            ready = []
            for chat, config in chats.items() if isinstance(chats, dict) else ():
                mode = self._mode(config.get("mode")) if isinstance(config, dict) else None
                if not mode or not self._listed(chat) or not self._eligible(chat, mode):
                    continue
                state = self.states.get(chat)
                if not state or state.get("mode") != mode:
                    self.states[chat] = state = {
                        "mode": mode,
                        "last_activity": moment,
                        "next_at": self._schedule(mode, moment, chat=chat),
                        "unanswered": 0,
                    }
                    changed = True
                    continue
                if mode == 'private' and chat == self._setting('owner') and moment < self.sleep_until:
                    continue
                if state["unanswered"] >= self._maximum(mode):
                    continue
                if mode == "group":
                    earliest = state["last_activity"] + self._group_idle()
                    if state["next_at"] < earliest:
                        state["next_at"] = earliest
                        changed = True
                if state["next_at"] > moment:
                    continue
                quiet_end = self._quiet_end(mode, moment)
                if quiet_end is not None:
                    state["next_at"] = max(state["next_at"], quiet_end)
                    changed = True
                    continue
                ready.append((state["next_at"], chat))
            if changed:
                self._save()
            return [chat for _, chat in sorted(ready, key=lambda item: (item[0], item[1]))]

    def describe(self, chat, now=None):
        """Describe the chat's current idle time and unanswered openings."""
        with self.lock:
            state = self.states.get(chat)
            if not state:
                return {"idle_minutes": 0, "unanswered": 0}
            idle = max(0, int((self._now(now) - state["last_activity"]) // 60))
            return {"idle_minutes": idle, "unanswered": state["unanswered"]}
