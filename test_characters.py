"""Named characters in drawing requests: Danbooru lookup and the prompts built from it. No network."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import characters
from brain import Brain, tidy_tags
from personas import PersonaStore
from settings import Settings

TAGS = {"yukinoshita_yukino": 2123, "hatsune_miku": 148116}
RELATED = {
    ("yukinoshita_yukino", "general"): [("long_hair", 0.9), ("black_hair", 0.84), ("blue_eyes", 0.65),
                                        ("school_uniform", 0.43), ("hair_ribbon", 0.37), ("smile", 0.1)],
    ("yukinoshita_yukino", "copyright"): [("yahari_ore_no_seishun_lovecome_wa_machigatteiru.", 1.0)],
}


def fake_get(route, params, timeout=8):
    if route == "/tags.json" and "search[name_comma]" in params:
        return [{"name": n, "post_count": TAGS[n], "category": 4}
                for n in params["search[name_comma]"].split(",") if n in TAGS]
    if route == "/tags.json":
        stem = params["search[name_matches]"].rstrip("*")
        return [{"name": n, "post_count": c} for n, c in TAGS.items() if n.startswith(stem)]
    if route == "/related_tag.json":
        return {"related_tags": [{"tag": {"name": n}, "frequency": f}
                                 for n, f in RELATED.get((params["query"], params["category"]), [])]}
    raise AssertionError(route)


class CharacterLookupTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("characters._get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_right_guess_is_confirmed_on_danbooru(self):
        self.assertEqual(characters.resolve("雪之下雪乃", "Yukinoshita Yukino"), "yukinoshita_yukino")

    def test_a_wrong_guess_is_found_through_a_web_search(self):
        search = mock.Mock(return_value=[{"url": "https://www.pixiv.net/tags/x"},
                                         {"url": "https://danbooru.donmai.us/posts?tags=yukinoshita_yukino"}])
        self.assertEqual(characters.resolve("雪之下雪乃", "yukino_(oregairu)", search), "yukinoshita_yukino")
        search.assert_called_once_with("雪之下雪乃 danbooru", 5)

    def test_a_wrong_qualifier_falls_back_to_the_name_stem(self):
        broken = mock.Mock(side_effect=OSError("offline"))
        self.assertEqual(characters.resolve("初音", "hatsune_miku_(vocaloid)", broken), "hatsune_miku")

    def test_unknown_names_give_nothing(self):
        self.assertIsNone(characters.resolve("我同事小王", "xiao_wang", mock.Mock(return_value=[])))

    def test_profile_keeps_frequent_tags_and_the_series(self):
        found = characters.profile("yukinoshita_yukino")
        self.assertEqual(found["series"], "yahari_ore_no_seishun_lovecome_wa_machigatteiru.")
        self.assertIn(("black_hair", 0.84), found["traits"])
        self.assertNotIn("smile", [n for n, _ in found["traits"]])  # too rare to be her


class DrawPromptTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.brain = Brain(Settings(root / "s.json"), PersonaStore(root / "p"), log_path=root / "calls.jsonl")
        patcher = mock.patch("characters._get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tidy_tags_spaces_dedupes_and_keeps_wanted_tags_out_of_the_negative(self):
        prompt, negative = tidy_tags("1girl, black_hair, Black hair, bikini", "black hair, school_uniform, blue eyes",
                                     keep=["blue_eyes"])
        self.assertEqual(prompt, "1girl, black hair, bikini")
        self.assertEqual(negative, "school uniform")

    def test_a_named_character_is_drawn_from_her_danbooru_profile(self):
        replies = [{"characters": [{"name": "雪之下雪乃", "tag": "yukinoshita_yukino"}]},
                   {"prompt": "1girl, long_hair, black hair, blue eyes, bikini, beach",
                    "negative": "school uniform, black hair, skirt"}]
        with mock.patch.object(self.brain, "call", side_effect=replies) as call:
            result = self.brain.draw_prompts("雪之下雪乃穿着比基尼在海边", search=mock.Mock(return_value=[]))
        self.assertEqual(result["characters"], ["yukinoshita_yukino"])
        # Her tag is put back if the model drops it; underscores become spaces.
        self.assertEqual(result["prompt"], "yukinoshita yukino, 1girl, long hair, black hair, blue eyes, bikini, beach")
        # Her own hair never ends up in the negative; her usual uniform may.
        self.assertEqual(result["negative"], "school uniform, skirt")
        refs = call.call_args_list[1].args[0][1]["content"]
        self.assertIn("yahari ore no seishun lovecome wa machigatteiru.", refs)
        self.assertIn("black_hair 0.84", refs)

    def test_no_named_character_is_a_plain_translation(self):
        with mock.patch.object(self.brain, "call", return_value={"characters": []}), \
                mock.patch.object(self.brain, "image_prompt", return_value="1girl, solo, cat ears") as plain:
            result = self.brain.draw_prompts("一张可爱的猫耳娘", "m")
        plain.assert_called_once_with("一张可爱的猫耳娘", "m")
        self.assertEqual(result, {"prompt": "1girl, solo, cat ears", "negative": "", "characters": []})

    def test_danbooru_offline_still_draws_from_the_description(self):
        with mock.patch("characters._get", side_effect=OSError("offline")), \
                mock.patch.object(self.brain, "call", return_value={"characters": [{"name": "初音", "tag": "x"}]}), \
                mock.patch.object(self.brain, "image_prompt", return_value="1girl, aqua hair"):
            self.assertEqual(self.brain.draw_prompts("初音未来")["prompt"], "1girl, aqua hair")

    def test_untranslated_output_is_refused(self):
        replies = [{"characters": [{"name": "雪之下雪乃", "tag": "yukinoshita_yukino"}]},
                   {"prompt": "雪之下雪乃，比基尼", "negative": ""}]
        with mock.patch.object(self.brain, "call", side_effect=replies):
            with self.assertRaisesRegex(ValueError, "转换失败"):
                self.brain.draw_prompts("雪之下雪乃穿着比基尼")


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("characters._get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_existing_but_wrong_guess_loses_to_the_web(self):
        search = mock.Mock(return_value=[{"url": "https://danbooru.donmai.us/wiki_pages/yukinoshita_yukino"}])
        self.assertEqual(characters.resolve("雪之下雪乃", "hatsune_miku", search), "yukinoshita_yukino")

    def test_the_guess_stands_when_the_web_agrees_or_is_silent(self):
        agree = mock.Mock(return_value=[{"url": "https://danbooru.donmai.us/posts?tags=hatsune_miku"}])
        self.assertEqual(characters.resolve("初音未来", "hatsune_miku", agree), "hatsune_miku")
        self.assertEqual(characters.resolve("初音未来", "hatsune_miku", mock.Mock(return_value=[])), "hatsune_miku")

    def test_weighted_tags_are_still_kept_out_of_the_negative(self):
        _, negative = tidy_tags("1girl, white hair", "(white_hair:1.4), ((green eyes:1.2)), [black hair]",
                                keep=["green_eyes"])
        self.assertEqual(negative, "[black hair]")

    def test_a_confirmed_tag_survives_a_failed_profile_lookup(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        brain = Brain(Settings(root / "s.json"), PersonaStore(root / "p"), log_path=root / "calls.jsonl")
        replies = [{"characters": [{"name": "初音未来", "tag": "hatsune_miku"}]},
                   {"prompt": "1girl, singing", "negative": ""}]
        with mock.patch("characters.profile", side_effect=OSError("offline")), \
                mock.patch.object(brain, "call", side_effect=replies):
            result = brain.draw_prompts("初音未来唱歌")
        self.assertEqual(result["characters"], ["hatsune_miku"])
        self.assertTrue(result["prompt"].startswith("hatsune miku, "))

    def test_only_links_on_danbooru_itself_count(self):
        search = mock.Mock(return_value=[
            {"url": "https://evil.invalid/donmai.us/posts?tags=hatsune_miku"},
            {"url": "https://danbooru.donmai.us.evil.invalid/posts?tags=hatsune_miku"},
            {"url": "https://danbooru.donmai.us/posts?tags=yukinoshita_yukino+rating%3Ag"}])
        self.assertEqual(characters.from_web("雪之下雪乃", search), ["yukinoshita_yukino"])
