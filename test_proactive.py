import json
import random
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from proactive import Initiative


class InitiativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "initiative.json"
        self.now = datetime(2026, 10, 22, 12).timestamp()
        self.settings = {
            "owner": "主人",
            "chats": {"主人": {"mode": "private"}, "群甲": {"mode": "group"}},
            "initiative_private": True,
            "initiative_group": True,
            "initiative_private_minutes": [30, 90],
            "initiative_group_idle_minutes": 40,
            "initiative_group_minutes": [60, 240],
            "initiative_quiet_hours": {"private": "1-8", "group": "1-8"},
            "initiative_max_unanswered": {"private": 2, "group": 2},
            "initiative_private_chats": [],
        }

    def make(self, seed=7, clock=None):
        return Initiative(self.path, self.settings, clock=clock or (lambda: self.now),
                          rng=random.Random(seed))

    def test_private_schedule_is_inside_configured_window(self):
        initiative = self.make()
        initiative.heard("主人", "private")  # uses the injected fake clock
        self.assertEqual(initiative.due(self.settings["chats"], self.now + 30 * 60 - 1), [])
        self.assertEqual(initiative.due(self.settings["chats"], self.now + 90 * 60), ["主人"])

    def test_due_chats_are_ordered_by_scheduled_time(self):
        self.settings["chats"]["朋友"] = {"mode": "private"}
        self.settings["initiative_private_chats"] = ["朋友"]
        initiative = self.make()
        initiative.heard("主人", "private", self.now)
        initiative.heard("朋友", "private", self.now)
        states = json.loads(self.path.read_text(encoding="utf-8"))["chats"]
        expected = sorted(("主人", "朋友"), key=lambda chat: states[chat]["next_at"])
        self.assertEqual(initiative.due(self.settings["chats"], self.now + 90 * 60), expected)

    def test_first_due_call_schedules_instead_of_firing_every_chat(self):
        initiative = self.make()
        self.assertEqual(initiative.due(self.settings["chats"], self.now), [])
        self.assertEqual(initiative.describe("主人", self.now), {"idle_minutes": 0, "unanswered": 0})
        self.assertEqual(initiative.describe("群甲", self.now), {"idle_minutes": 0, "unanswered": 0})

    def test_heard_resets_unanswered_and_restores_after_limit(self):
        self.settings["initiative_private_minutes"] = [1, 1]
        initiative = self.make()
        initiative.heard("主人", "private", self.now)
        first = self.now + 60
        self.assertEqual(initiative.due(self.settings["chats"], first), ["主人"])
        initiative.spoke("主人", "private", True, first)
        second = first + 60
        self.assertEqual(initiative.due(self.settings["chats"], second), ["主人"])
        initiative.spoke("主人", "private", True, second)
        self.assertEqual(initiative.describe("主人", second)["unanswered"], 2)
        self.assertEqual(initiative.due(self.settings["chats"], second + 600), [])
        initiative.heard("主人", "private", second + 600)
        self.assertEqual(initiative.describe("主人", second + 600)["unanswered"], 0)
        self.assertEqual(initiative.due(self.settings["chats"], second + 660), ["主人"])

    def test_declined_reschedules_without_counting(self):
        self.settings["initiative_private_minutes"] = [1, 1]
        initiative = self.make()
        initiative.heard("主人", "private", self.now)
        initiative.declined("主人", "private", self.now + 60)
        self.assertEqual(initiative.describe("主人", self.now + 60)["unanswered"], 0)
        self.assertEqual(initiative.due(self.settings["chats"], self.now + 119), [])
        self.assertEqual(initiative.due(self.settings["chats"], self.now + 120), ["主人"])

    def test_group_waits_for_idle_threshold_then_random_delay(self):
        self.settings["initiative_group_minutes"] = [10, 10]
        initiative = self.make()
        initiative.heard("群甲", "group", self.now)
        self.assertEqual(initiative.due(self.settings["chats"], self.now + 40 * 60), [])
        self.assertEqual(initiative.due(self.settings["chats"], self.now + 50 * 60), ["群甲"])

    def test_quiet_hours_delay_until_end(self):
        self.settings["initiative_private_minutes"] = [1, 1]
        start = datetime(2026, 10, 22, 0, 30).timestamp()
        initiative = self.make()
        initiative.heard("主人", "private", start)
        chats = {"主人": {"mode": "private"}}
        self.assertEqual(initiative.due(chats, datetime(2026, 10, 22, 1, 30).timestamp()), [])
        self.assertEqual(initiative.due(chats, datetime(2026, 10, 22, 7, 59).timestamp()), [])
        self.assertEqual(initiative.due(chats, datetime(2026, 10, 22, 8).timestamp()), ["主人"])

    def test_quiet_hours_can_cross_midnight(self):
        self.settings["initiative_private_minutes"] = [1, 1]
        self.settings["initiative_quiet_hours"]["private"] = "23-7"
        start = datetime(2026, 10, 22, 22, 55).timestamp()
        initiative = self.make()
        initiative.heard("主人", "private", start)
        chats = {"主人": {"mode": "private"}}
        self.assertEqual(initiative.due(chats, datetime(2026, 10, 22, 23, 30).timestamp()), [])
        tomorrow = datetime(2026, 10, 23)
        self.assertEqual(initiative.due(chats, (tomorrow + timedelta(hours=6, minutes=59)).timestamp()), [])
        self.assertEqual(initiative.due(chats, (tomorrow + timedelta(hours=7)).timestamp()), ["主人"])

    def test_state_recovers_after_restart_and_bad_file_is_empty(self):
        self.settings["initiative_private_minutes"] = [1, 1]
        first = self.make()
        first.heard("主人", "private", self.now)
        first.spoke("主人", "private", True, self.now + 60)
        restored = self.make(seed=8)
        self.assertEqual(restored.describe("主人", self.now + 120), {"idle_minutes": 1, "unanswered": 1})
        self.path.write_text("{broken", encoding="utf-8")
        recovered = self.make(seed=9)
        self.assertEqual(recovered.describe("主人", self.now), {"idle_minutes": 0, "unanswered": 0})
        self.assertEqual(recovered.due(self.settings["chats"], self.now), [])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["chats"].keys(),
                         self.settings["chats"].keys())

    def test_other_private_chat_requires_explicit_opt_in(self):
        self.settings["chats"]["朋友"] = {"mode": "private"}
        self.settings["initiative_private_minutes"] = [1, 1]
        initiative = self.make()
        initiative.heard("主人", "private", self.now)
        initiative.heard("朋友", "private", self.now)
        self.assertEqual(initiative.due(self.settings["chats"], self.now + 60), ["主人"])
        self.settings["initiative_private_chats"] = ["朋友"]
        self.assertEqual(initiative.due(self.settings["chats"], self.now + 60), ["主人", "朋友"])


if __name__ == "__main__":
    unittest.main()
