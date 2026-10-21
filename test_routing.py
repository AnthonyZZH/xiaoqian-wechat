"""Offline regression coverage for exact recipients and multi-chat scheduling."""
import tempfile
import time
import unittest
from pathlib import Path

from app import Bot
from personas import PersonaStore
from routing import ChatQueue, recipients
from settings import Settings
from test_core import FakeWeChat


class RoutingTests(unittest.TestCase):
    def test_exact_private_or_unknown_name_not_substituted_with_only_group(self):
        for text in ['给测试联系人发消息说你好', '帮我给「测试联系人」发一条消息：你好', '请向测试联系人说一声你好']:
            self.assertEqual(recipients([text], ['测试群'], ['测试群']), ['测试联系人'])

    def test_no_pronoun_or_negated_authorization(self):
        for text in ['给他发消息', '不要给测试联系人发消息', '测试联系人刚才发消息了', '群里在聊什么']:
            self.assertEqual(recipients([text], ['测试联系人'], ['测试群']), [])
        self.assertEqual(recipients(['去群里说今晚八点'], [], ['群甲', '群乙']), [])

    def test_multiple_requests_in_same_owner_batch_are_preserved(self):
        self.assertEqual(recipients(['给小王发消息说你好', '给小李发消息说晚安']), ['小王', '小李'])

    def test_queue_fifo_and_original_anchor_survive_more_arrivals(self):
        q = ChatQueue()
        chats = {'群': {'mode': 'group'}, '私聊甲': {'mode': 'private'}, '私聊乙': {'mode': 'private'}, '主人': {'mode': 'private'}}
        q.observe({c: '旧消息' for c in chats}, chats)
        q.observe({'群': '新群消息', '私聊乙': '新私聊', '私聊甲': '新私聊', '主人': '新指令'}, chats)
        q.observe({'群': '第二条群消息'}, chats)
        self.assertEqual(q.waiting['群']['baseline'], '旧消息')
        order = []
        while (chat := q.next(chats, '主人')):
            order.append(chat)
            q.waiting.pop(chat)
        self.assertEqual(order, ['主人', '私聊乙', '私聊甲', '群'])


class MultiChatTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        modes = {'测试群': 'group', '私聊甲': 'private', '私聊乙': 'private', '主人': 'private'}
        settings = Settings(root / 'settings.json')
        settings.update(chat='测试群', owner='主人', chats={c: {'mode': m} for c, m in modes.items()},
                        multi_chat=True, batch_seconds=0, minimum_gap_seconds=0, group_gap_seconds=0,
                        group_chime_in=1, memory_enabled=False, proactive=False)
        self.bot = Bot(settings, PersonaStore(root / 'personas'), False, root / 'logs', root / 'data')
        self.wx = self.bot.adapter = FakeWeChat(modes)
        for chat in modes:
            self.wx.post(chat, '一条很久以前的旧消息')
        self.prompts = []
        self.responses = []
        def decide(history, proactive=False, **kw):
            self.prompts.append((kw['chat'], [r['text'] for r in history]))
            if self.responses:
                return self.responses.pop(0)
            return {'speak': True, 'to_me': True, 'text': f'消息已经收到{len(self.prompts)}。'}
        self.bot.brain = type('Brain', (), {'decide': staticmethod(decide)})()
        self.bot.enabled.set()
        self.bot.make_runner()
        self.bot.runner.step()
        self.bot.observe_chats()

    def cycles(self, count=8):
        for _ in range(count):
            self.bot.cycle()

    def test_simultaneous_chats_all_processed_owner_private_group(self):
        for chat in ['测试群', '私聊乙', '主人', '私聊甲']:
            self.wx.post(chat, f'{chat}的新问题', sender='群友' if chat == '测试群' else '')
        self.cycles()
        self.assertEqual([c for c, _ in self.wx.sent], ['主人', '私聊甲', '私聊乙', '测试群'])
        self.assertEqual(self.bot.incoming.waiting, {})

    def test_all_first_visit_messages_reach_model_not_just_last(self):
        self.wx.post('私聊甲', '先问第一个问题')
        self.wx.post('私聊甲', '再问第二个问题')
        self.wx.post('私聊甲', '最后补充第三点')
        self.cycles()
        prompt = next(rows for chat, rows in self.prompts if chat == '私聊甲')
        self.assertEqual(prompt[-3:], ['先问第一个问题', '再问第二个问题', '最后补充第三点'])
        self.assertNotIn('一条很久以前的旧消息', prompt)

    def test_arrival_while_another_chat_generates_is_not_lost(self):
        original = self.bot.brain.decide
        def generate(history, proactive=False, **kw):
            if kw['chat'] == '私聊甲':
                self.wx.post('私聊乙', '生成期间又来一条')
                self.bot.brain.decide = original
            return original(history, proactive, **kw)
        self.bot.brain.decide = generate
        self.wx.post('私聊甲', '先来的私聊问题')
        self.wx.post('测试群', '群里的问题', sender='群友')
        self.cycles()
        self.assertEqual([c for c, _ in self.wx.sent], ['私聊甲', '私聊乙', '测试群'])

    def test_owner_arriving_during_generation_preempts_group_send(self):
        original = self.bot.brain.decide
        def generate(history, proactive=False, **kw):
            result = original(history, proactive, **kw)
            if kw['chat'] == '测试群':
                self.wx.post('主人', '优先处理这个请求')
                self.bot.brain.decide = original
            return result
        self.bot.brain.decide = generate
        self.wx.post('测试群', '群里的问题', sender='群友')
        self.cycles()
        self.assertEqual([c for c, _ in self.wx.sent], ['主人', '测试群'])

    def test_owner_can_relay_to_private_chat_without_replying_to_its_history(self):
        self.responses = [{'speak': True, 'text': '我去转告', 'relays': [('私聊乙', '主人让你早点休息')]}]
        self.wx.post('主人', '给私聊乙发消息说早点休息')
        self.cycles()
        self.assertIn(('私聊乙', '主人让你早点休息'), self.wx.sent)
        self.assertEqual([c for c, _ in self.prompts], ['主人'])

    def test_unknown_outbound_contact_does_not_enable_unsolicited_monitoring(self):
        self.wx.chats['新联系人'] = []
        self.wx.modes['新联系人'] = 'private'
        self.responses = [{'speak': True, 'text': '我试着转告', 'relays': [('新联系人', '你好呀')]}]
        self.wx.post('主人', '给新联系人发消息说你好')
        self.cycles()
        self.assertIn(('新联系人', '你好呀'), self.wx.sent)
        self.assertNotIn('新联系人', self.bot.settings['chats'])

    def test_wrong_model_target_is_rejected_not_sent_to_group(self):
        self.responses = [{'speak': True, 'text': '好的', 'relay': ('测试群', '不该发出的内容')}]
        self.wx.post('主人', '给私聊乙发消息说你好')
        self.cycles()
        self.assertNotIn(('测试群', '不该发出的内容'), self.wx.sent)

    def test_wrong_opened_title_never_sends(self):
        original = self.wx.open_chat
        def wrong(name):
            original('测试群' if name == '私聊乙' else name)
        self.wx.open_chat = wrong
        with self.assertRaisesRegex(RuntimeError, '核对收件人'):
            self.bot.send_relay('私聊乙', '不该发出')
        self.assertEqual(self.wx.sent, [])


if __name__ == '__main__':
    unittest.main()
