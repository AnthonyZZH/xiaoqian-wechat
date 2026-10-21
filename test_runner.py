import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import runner as runner_module
from app import Enabled
from runner import Runner
from settings import Settings


def row(key, text='今天不想学习', role='user', sender='', image=None):
    return SimpleNamespace(key=key, text=text, role=role, sender=sender, image=image)


def stripes():
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (90, 90), (240, 240, 240))
    for x in range(0, 90, 15):
        ImageDraw.Draw(img).line((x, 0, 90 - x, 90), fill=(0, 0, 0), width=4)
    return img


def picture(color):
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (90, 90), color)
    ImageDraw.Draw(img).ellipse((20, 20, 70, 70), fill=(255, 255, 255))
    ImageDraw.Draw(img).rectangle((0, 0, 30, 90), fill=(10, 10, 10))
    return img


class FakeAdapter:
    def __init__(self):
        self.rows, self.sent = [], []
        self.during_send = None

    def read(self):
        return self.rows[:]

    def send(self, text, cancel):
        if cancel():
            raise RuntimeError("cancelled")
        if self.during_send:
            self.during_send()
            if cancel():
                raise RuntimeError("cancelled while typing")
        self.sent.append(text)
        self.rows.append(row('sent%d' % len(self.sent), text, 'assistant'))

    def send_image(self, path, cancel):
        if cancel():
            raise RuntimeError("cancelled")
        self.sent.append('IMG:' + Path(path).name)
        self.rows.append(row('sent%d' % len(self.sent), '[动画表情]', 'assistant'))


class Base(unittest.TestCase):
    chat = '示例好友'

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name) / 'settings.json')
        self.settings.update(batch_seconds=0, minimum_gap_seconds=0, group_chime_in=1.0,
                             chats={'示例好友': {'mode': 'private'}, '示例群乙': {'mode': 'group'}})
        self.adapter = FakeAdapter()
        self.adapter.rows.append(row('old', '很早以前的消息'))
        self.enabled = Enabled()
        self.enabled.set()
        self.calls = []
        self.runner = self.make()
        self.runner.step()

    def make(self, reply_latest=False, chat=None):
        def decide(history, proactive, **kw):
            self.calls.append(([r['text'] for r in history], proactive, kw, [dict(r) for r in history]))
            return dict(speak=True, text='接到了%d' % len(self.calls), seconds=0)
        self.learned = []
        def learn(rows, **kw):
            self.learned.append(([r['text'] for r in rows], kw))
            return getattr(self, 'lesson', {'people': [], 'jargon': [], 'style': ''})
        brain = SimpleNamespace(decide=decide, describe_image=lambda image: '猫猫翻白眼', learn=learn)
        runner = Runner(self.settings, self.adapter, self.enabled, threading.Event(), lambda *a: None,
                        brain, chat or self.chat, history_dir=Path(self.temp.name), reply_latest=reply_latest,
                        stickers=getattr(self, 'library', None), memory_dir=Path(self.temp.name) / 'mem')
        runner.history = []
        runner.quiet = lambda: False
        return runner

    def add(self, key='one', text='今天不想学习', role='user', sender=''):
        self.adapter.rows.append(row(key, text, role, sender))

    def ago(self, seconds):
        self.runner.last_incoming -= seconds


class PrivateTests(Base):
    def test_chat_must_be_allowlisted(self):
        with self.assertRaises(ValueError):
            self.make(chat='陌生人')

    def test_old_messages_not_answered_on_start(self):
        self.runner.step()
        self.assertEqual(self.adapter.sent, [])

    def test_reply_latest_answers_only_last_user_message(self):
        runner = self.make(reply_latest=True)
        runner.step(); runner.step()
        self.assertEqual(self.adapter.sent, ['接到了1'])
        self.assertEqual(self.calls[0][0], ['很早以前的消息'])

    def test_brain_gets_chat_and_mode(self):
        self.add(); self.runner.step()
        self.assertEqual(self.calls[0][2], {'chat': '示例好友', 'mode': 'private', 'mentioned': False, 'continuing': False, 'memory': '',
                                             'relay_targets': [], 'context': '', 'relay_messages': ['今天不想学习']})

    def test_outgoing_does_not_trigger_reply(self):
        self.add(role='assistant')
        self.runner.step()
        self.assertEqual(self.adapter.sent, [])

    def test_own_message_misread_as_user_does_not_loop(self):
        self.add(); self.runner.step()
        self.add('echo', '接到了1', 'user')
        self.runner.step(); self.runner.step()
        self.assertEqual(self.adapter.sent, ['接到了1'])

    def test_received_only_once(self):
        self.add()
        self.runner.step(); self.runner.step()
        self.assertEqual(self.adapter.sent, ['接到了1'])

    def test_batch_merges_burst_into_one_reply(self):
        self.settings.update(batch_seconds=4)
        self.add('a', '我跟你说'); self.runner.step()
        self.add('b', '今天又迟到了'); self.runner.step()
        self.assertEqual(self.adapter.sent, [])
        self.ago(5); self.runner.step()
        self.assertEqual(self.adapter.sent, ['接到了1'])
        self.assertEqual(self.calls[0][0][-2:], ['我跟你说', '今天又迟到了'])

    def test_silence_request(self):
        self.add(text='别回复了')
        self.runner.step()
        self.runner.next_proactive = 0
        self.runner.step()
        self.assertEqual((self.adapter.sent, self.calls), ([], []))

    def test_silence_uses_current_persona_name(self):
        self.settings.update(persona='阿温')
        self.add(text='阿温闭嘴')
        self.runner.step()
        self.assertEqual(self.adapter.sent, [])

    def test_pause_before_step_never_reads_or_sends(self):
        self.add(); self.enabled.clear()
        self.runner.step()
        self.assertEqual((self.adapter.sent, self.calls), ([], []))

    def test_pause_during_generation_discards_reply(self):
        self.add()
        def generate(*args, **kw):
            self.enabled.clear()
            return dict(speak=True, text='旧回复', seconds=0)
        self.runner.brain.decide = generate
        self.runner.step()
        self.assertEqual(self.adapter.sent, [])

    def test_resume_does_not_answer_backlog(self):
        self.add(); self.enabled.clear()
        self.add('two', '暂停时发的')
        self.enabled.set()
        self.runner.step(); self.runner.step()
        self.assertEqual(self.adapter.sent, [])
        self.assertIn('暂停时发的', [r['text'] for r in self.runner.history])
        self.add('three', '继续之后的新消息')
        self.runner.step()
        self.assertEqual(self.adapter.sent, ['接到了1'])

    def test_pause_while_typing_cancels_and_stays_paused(self):
        self.add()
        self.adapter.during_send = self.enabled.clear
        with self.assertRaises(RuntimeError):
            self.runner.step()
        self.assertEqual(self.adapter.sent, [])
        self.assertFalse(self.enabled.is_set())

    def test_new_message_during_generation_discards_reply(self):
        self.add()
        def generate(*args, **kw):
            self.add('two', '等下，我改主意了')
            return dict(speak=True, text='旧回复', seconds=0)
        self.runner.brain.decide = generate
        self.runner.step()
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.runner.pending), 2)

    def test_message_during_typing_is_answered_next(self):
        self.add()
        self.adapter.during_send = lambda: self.add('two', '打字时我又说了一句')
        self.runner.step()
        self.adapter.during_send = None
        self.assertEqual([m.key for m in self.runner.pending], ['two'])
        self.runner.step()
        self.assertEqual(self.adapter.sent, ['接到了1', '接到了2'])

    def test_rerendered_list_is_not_answered(self):
        self.add(); self.runner.step()
        self.adapter.rows = [row('new-' + r.key, r.text, r.role) for r in self.adapter.rows]
        self.runner.step(); self.runner.step()
        self.assertEqual(self.adapter.sent, ['接到了1'])

    def test_coming_back_to_a_chat_answers_only_what_is_new(self):
        self.add('a', '我们去吃火锅吧'); self.runner.step()
        # Switching away and back gives every row a new id; two new messages arrived meanwhile.
        self.adapter.rows = [row('new-' + r.key, r.text, r.role) for r in self.adapter.rows]
        self.add('b', '你怎么不说话'); self.add('c', '人呢')
        self.runner.step()
        self.assertEqual(self.adapter.sent, ['接到了1', '接到了2'])
        self.assertEqual(self.calls[-1][0][-2:], ['你怎么不说话', '人呢'])

    def test_repeated_short_messages_do_not_pull_old_rows_back(self):
        self.add('a', '哈哈'); self.add('b', '嗯'); self.runner.step()
        self.assertEqual(len(self.calls), 1)
        self.adapter.rows = [row('n' + r.key, r.text, r.role) for r in self.adapter.rows]
        self.runner.step(); self.runner.step()
        self.assertEqual(len(self.calls), 1)

    def test_empty_first_read_then_history_is_not_answered(self):
        self.adapter.rows = []
        runner = self.make()
        runner.step()
        self.add('x', '旧消息A'); self.add('y', '旧消息B')
        runner.step(); runner.step()
        self.assertEqual(self.adapter.sent, [])

    def test_scrolled_up_history_is_not_answered(self):
        self.adapter.rows.insert(0, row('older', '更早加载出来的'))
        self.runner.step()
        self.assertEqual(self.adapter.sent, [])

    def test_stale_pending_is_dropped(self):
        self.add(); self.runner.retry_after = float('inf'); self.runner.step()
        self.runner.retry_after = 0; self.ago(1000)
        self.runner.step()
        self.assertEqual(self.adapter.sent, [])

    def test_proactive_once_without_response(self):
        self.add(); self.runner.step()
        self.runner.next_proactive = 0
        for _ in range(4):
            self.runner.step()
        self.assertEqual(self.adapter.sent, ['接到了1', '接到了2'])
        self.assertEqual([c[1] for c in self.calls], [False, True])

    def test_proactive_can_be_switched_off(self):
        self.settings.update(proactive=False)
        self.add(); self.runner.step()
        self.runner.next_proactive = 0
        self.runner.step()
        self.assertEqual(self.adapter.sent, ['接到了1'])

    def test_no_proactive_after_model_chose_silence(self):
        self.runner.brain.decide = lambda *a, **kw: dict(speak=False, text='', seconds=0)
        self.add(); self.runner.step()
        self.runner.next_proactive = 0
        self.runner.step()
        self.assertEqual(self.adapter.sent, [])

    def test_duplicate_reply_text_not_sent(self):
        self.runner.brain.decide = lambda *a, **kw: dict(speak=True, text='同一句', seconds=0)
        self.add(); self.runner.step()
        self.add('two', '再说一句'); self.runner.step()
        self.assertEqual(self.adapter.sent, ['同一句'])

    def test_reworded_repeat_not_sent(self):
        texts = iter(['小公主？我平时不都是拿话扎你的吗，这形象管理也太崩了',
                      '小公主？本宫平时不都是拿话扎你的吗，别突然给我加滤镜'])
        self.runner.brain.decide = lambda *a, **kw: dict(speak=True, text=next(texts), seconds=0)
        self.add(); self.runner.step()
        self.add('two', '你永远是我的小公主'); self.runner.step()
        self.assertEqual(len(self.adapter.sent), 1)

    def test_not_addressed_to_her_stays_silent(self):
        self.runner.brain.decide = lambda *a, **kw: dict(speak=True, to_me=False, text='硬接一句', seconds=0)
        self.add(text='哈哈哈'); self.runner.step()
        self.assertEqual(self.adapter.sent, [])

    def test_cut_off_reply_not_sent(self):
        texts = iter(['你发了个', '好的', '行了', '你发了个占位图让我锐评啊'])
        self.runner.brain.decide = lambda *a, **kw: dict(speak=True, text=next(texts), seconds=0)
        for i in range(4):
            self.add(f'k{i}', f'第{i}句'); self.runner.step()
        self.assertEqual(self.adapter.sent, ['好的', '行了', '你发了个占位图让我锐评啊'])

    def test_overlong_reply_not_sent(self):
        self.settings.update(max_reply_chars=3)
        self.add(); self.runner.step()
        self.assertEqual(self.adapter.sent, [])

    def test_three_model_failures_pause_and_resume_resets(self):
        def fail(*a, **kw):
            raise RuntimeError('model down')
        self.runner.brain.decide = fail
        self.add()
        for _ in range(3):
            self.runner.retry_after = 0
            with self.assertRaises(RuntimeError):
                self.runner.step()
        self.assertFalse(self.enabled.is_set())
        self.assertEqual(self.adapter.sent, [])
        self.enabled.set(); self.runner.step()
        self.assertEqual((self.runner.error_count, self.runner.retry_after), (0, 0.0))

    def test_malformed_model_result_counts_as_failure(self):
        self.runner.brain.decide = lambda *a, **kw: {'text': '没有speak'}
        self.add()
        with self.assertRaises(ValueError):
            self.runner.step()
        self.assertEqual(self.runner.error_count, 1)
        self.assertEqual(self.adapter.sent, [])

    def test_failed_send_pauses_without_retry(self):
        self.add()
        attempts = []
        def broken(*a, **kw):
            attempts.append(a)
            raise RuntimeError('uncertain')
        self.adapter.send = broken
        with self.assertRaises(RuntimeError):
            self.runner.step()
        self.assertFalse(self.enabled.is_set())
        self.enabled.set()
        for _ in range(3):
            self.runner.step()
        self.assertEqual(len(attempts), 1)

    def test_history_saved_per_chat(self):
        self.add(); self.runner.step()
        saved = json.loads((Path(self.temp.name) / '示例好友.json').read_text(encoding='utf-8'))
        self.assertEqual([r['text'] for r in saved], ['今天不想学习', '接到了1'])

    def test_old_pilot_history_adopted_without_repeats(self):
        old = Path(self.temp.name) / 'root'
        old.mkdir()
        rows = [{'role': 'user', 'text': 'a'}, {'role': 'user', 'text': 'a'}, {'role': 'assistant', 'text': 'b'}]
        (old / 'history_示例好友.json').write_text(json.dumps(rows), encoding='utf-8')
        with mock.patch.object(runner_module, 'ROOT', old):
            history = runner_module.load_history(Path(self.temp.name) / 'missing.json', '示例好友')
        self.assertEqual([r['text'] for r in history], ['a', 'b'])


class MediaTests(Base):
    def setUp(self):
        from stickers import StickerLibrary
        holder = TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        folder = Path(holder.name)
        self.library = StickerLibrary(folder)
        picture((200, 40, 40)).save(folder / 'eyeroll.png')
        self.library.scan(lambda image: '翻白眼的猫')
        super().setUp()
        self.settings.update(stickers_enabled=True)

    def reply(self, **result):
        self.runner.brain.decide = lambda *a, **kw: dict(dict(speak=True, text='', sticker=''), **result)

    def test_incoming_sticker_is_described(self):
        self.add('s1', '[动画表情]')
        self.adapter.rows[-1].image = picture((0, 120, 0))
        self.runner.step()
        self.assertEqual(self.calls[0][0][-1], '[表情包：猫猫翻白眼]')

    def test_repeat_sticker_description_is_cached(self):
        import runner as r
        r.IMAGE_CACHE.clear()
        seen = []
        self.runner.brain.describe_image = lambda image: seen.append(1) or '狗头'
        for key in ('s1', 's2'):
            self.add(key, '动画表情')
            self.adapter.rows[-1].image = picture((90, 60, 200))
            self.runner.step()
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.calls[-1][0][-1], '[表情包：狗头]')

    def test_placeholder_description_is_discarded(self):
        import runner as r
        r.IMAGE_CACHE.clear()
        self.runner.brain.describe_image = lambda image: '深色背景中一个图片加载失败的占位图标'
        self.add('p1', '图片')
        self.adapter.rows[-1].image = picture((10, 200, 90))
        self.runner.step()
        self.assertEqual(self.calls[0][0][-1], '[图片]')

    def test_incoming_sticker_collected_once_not_photos(self):
        import runner as r
        r.IMAGE_CACHE.clear()
        inbox = self.library.inbox
        self.add('s1', '[动画表情]', sender='测试群友')
        self.adapter.rows[-1].image = stripes()
        self.runner.step()
        self.add('s2', '[动画表情]')
        self.adapter.rows[-1].image = stripes()
        self.runner.step()
        self.add('p1', '[图片]')
        self.adapter.rows[-1].image = picture((120, 0, 120))
        self.runner.step()
        self.assertEqual([i['desc'] for i in inbox.items.values()], ['猫猫翻白眼'])

    def test_sticker_already_in_library_not_collected(self):
        import runner as r
        r.IMAGE_CACHE.clear()
        self.add('s1', '[动画表情]')
        self.adapter.rows[-1].image = picture((200, 40, 40))  # same picture as eyeroll.png
        self.runner.step()
        self.assertEqual(self.library.inbox.items, {})

    def test_collection_can_be_switched_off(self):
        import runner as r
        r.IMAGE_CACHE.clear()
        self.settings.update(collect_stickers=False)
        self.add('s1', '[动画表情]')
        self.adapter.rows[-1].image = picture((0, 120, 0))
        self.runner.step()
        self.assertEqual(self.library.inbox.items, {})

    def test_description_can_be_switched_off(self):
        self.settings.update(describe_images=False)
        self.add('s1', '[动画表情]')
        self.adapter.rows[-1].image = picture((0, 120, 0))
        self.runner.step()
        self.assertEqual(self.calls[0][0][-1], '[动画表情]')

    def test_sends_text_then_sticker(self):
        self.reply(text='行吧', sticker='eyeroll')
        self.add(); self.runner.step()
        self.assertEqual(self.adapter.sent, ['行吧', 'IMG:eyeroll.png'])
        self.assertEqual([r['text'] for r in self.runner.history][-2:], ['行吧', '[表情包：翻白眼的猫]'])

    def test_sticker_only_reply(self):
        self.reply(sticker='eyeroll')
        self.add(); self.runner.step()
        self.assertEqual(self.adapter.sent, ['IMG:eyeroll.png'])

    def test_sticker_ignored_when_disabled_or_unknown(self):
        self.settings.update(stickers_enabled=False)
        self.reply(sticker='eyeroll')
        self.add(); self.runner.step()
        self.settings.update(stickers_enabled=True)
        self.reply(sticker='nope')
        self.add('two', '再来'); self.runner.step()
        self.assertEqual(self.adapter.sent, [])

    def test_same_sticker_not_repeated_back_to_back(self):
        self.reply(sticker='eyeroll')
        self.add(); self.runner.step()
        self.add('two', '哈哈'); self.runner.step()
        self.assertEqual(self.adapter.sent, ['IMG:eyeroll.png'])

    def test_own_sticker_misread_as_incoming_is_ignored(self):
        self.reply(sticker='eyeroll')
        self.add(); self.runner.step()
        self.add('echo', '[动画表情]')
        self.adapter.rows[-1].image = picture((200, 40, 40))
        self.runner.step(); self.runner.step()
        self.assertEqual(self.adapter.sent, ['IMG:eyeroll.png'])
        self.assertEqual(len(self.calls), 0)


class MemoryTests(Base):
    chat = '示例群乙'

    def chat_a_while(self, n):
        self.settings.update(group_gap_seconds=0, max_messages_per_10_minutes=100)
        for i in range(n):
            self.add(f'm{i}', f'第{i}句', sender='阿杰' if i % 2 else 'momo')
            self.runner.step()
            self.runner.sent_times.clear()

    def test_learns_when_quiet_after_enough_messages(self):
        self.lesson = {'people': [{'name': '阿杰', 'facts': ['在读研，研究机器人'], 'forget': [],
                                   'impression': '话少但很冷'}],
                       'jargon': [{'term': 'yyds', 'meaning': '永远的神'}], 'style': '爱用缩写'}
        self.chat_a_while(12)
        self.runner.step()
        self.assertEqual(self.learned, [], 'must wait for the chat to go quiet')
        self.runner.last_incoming -= 31
        self.runner.step()
        self.assertEqual(len(self.learned), 1)
        self.assertEqual(self.runner.memory.people['阿杰']['facts'], ['在读研，研究机器人'])
        self.assertEqual(self.runner.unlearned, 0)
        self.assertTrue((Path(self.temp.name) / 'mem' / '示例群乙.json').exists())

    def test_memory_reaches_the_prompt_for_current_speakers(self):
        self.runner.memory.apply({'people': [{'name': '阿杰', 'facts': ['养了一只柯基'], 'forget': [],
                                              'impression': ''}]}, ['阿杰'])
        self.add('g1', '我家狗又拆家了', sender='阿杰'); self.runner.step()
        self.assertIn('阿杰：养了一只柯基', self.calls[-1][2]['memory'])

    def test_failed_learning_never_pauses(self):
        def broken(*a, **kw):
            raise RuntimeError('model down')
        self.runner.brain.learn = broken
        self.chat_a_while(12)
        self.runner.last_incoming -= 31
        self.runner.step()
        self.assertTrue(self.enabled.is_set())
        self.assertEqual(self.runner.unlearned, 12)

    def test_switch_off(self):
        self.settings.update(memory_enabled=False)
        self.chat_a_while(12)
        self.runner.last_incoming -= 31
        self.runner.step()
        self.assertEqual(self.learned, [])
        self.assertEqual(self.calls[-1][2]['memory'], '')


class GroupTests(Base):
    chat = '示例群乙'

    def test_sender_kept_in_history(self):
        self.add('g1', '今晚吃啥', sender='阿杰')
        self.runner.step()
        self.assertEqual(self.calls[0][3][-1], {'role': 'user', 'text': '今晚吃啥', 'sender': '阿杰'})
        self.assertEqual(self.calls[0][2]['mode'], 'group')

    def test_unmentioned_chatter_right_after_speaking_is_skipped(self):
        self.add('g1', '今晚吃啥', sender='阿杰'); self.runner.step()
        self.add('g2', '火锅吧', sender='momo'); self.runner.step()
        self.assertEqual(len(self.adapter.sent), 1)

    def test_mention_overrides_group_gap(self):
        self.add('g1', '今晚吃啥', sender='阿杰'); self.runner.step()
        self.add('g2', '小千你说呢', sender='momo'); self.runner.step()
        self.assertEqual(len(self.adapter.sent), 2)
        self.assertTrue(self.calls[1][2]['mentioned'])

    def test_person_she_answered_can_keep_talking(self):
        self.add('g1', '今晚吃啥', sender='阿杰'); self.runner.step()
        self.add('g2', '那火锅还是烤肉', sender='阿杰'); self.runner.step()
        self.assertEqual(len(self.adapter.sent), 2)
        self.add('g3', '我也想吃', sender='momo'); self.runner.step()
        self.assertEqual(len(self.adapter.sent), 2)

    def test_fatigue_after_a_run_of_messages(self):
        self.settings.update(group_gap_seconds=0)
        for i in range(6):
            self.add(f'g{i}', f'随便聊聊{i}', sender=f'人{i}'); self.runner.step()
        self.assertEqual(len(self.adapter.sent), 4)
        self.add('gm', '小千你还在吗', sender='人9'); self.runner.step()
        self.assertEqual(len(self.adapter.sent), 5)

    def test_partner_talking_about_her_is_not_answered(self):
        self.add('g1', '小千你好', sender='示例好友'); self.runner.step()
        self.runner.brain.decide = lambda *a, **kw: self.calls.append(kw) or dict(
            speak=True, to_me=False, text='硬接', seconds=0)
        self.add('g2', '笑死我了，识别图片有问题，不理你', sender='示例好友'); self.runner.step()
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertTrue(self.calls[-1]['continuing'])
        self.assertFalse(self.calls[-1]['mentioned'])

    def test_chime_in_filter_skips_the_model(self):
        self.settings.update(group_chime_in=0)
        self.add('g1', '今晚吃啥', sender='阿杰'); self.runner.step()
        self.assertEqual(self.calls, [])
        self.add('g2', '小千你说呢', sender='阿杰'); self.runner.step()
        self.assertEqual(len(self.adapter.sent), 1)

    def test_at_wechat_nickname_counts_as_mention(self):
        self.settings.update(aliases=['机器人昵称'])
        self.add('g1', '今晚吃啥', sender='阿杰'); self.runner.step()
        self.add('g2', '@机器人昵称 ', sender='测试群友'); self.runner.step()
        self.assertEqual(len(self.adapter.sent), 2)
        self.assertTrue(self.calls[1][2]['mentioned'])

    def test_no_proactive_in_group(self):
        self.add('g1', '今晚吃啥', sender='阿杰'); self.runner.step()
        self.runner.next_proactive = 0
        self.runner.step(); self.runner.step()
        self.assertEqual(len(self.adapter.sent), 1)


if __name__ == '__main__':
    unittest.main()
