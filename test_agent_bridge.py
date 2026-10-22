"""Owner-chat agent commands wired through Bot/Runner, with fake agents and a fake WeChat.

Never runs claude/codex/deepseek, never touches data\\: every path is a temp folder.

    python -m unittest test_agent_bridge -v
"""
import json
import sys
import time
import textwrap
from types import SimpleNamespace
from unittest import mock

from personas import PersonaStore
from settings import Settings
from test_core import FakeWeChat, Temp

AGENT = textwrap.dedent("""
    import sys, time
    task = sys.argv[1]
    if "慢" in task:
        print("started", flush=True)
        time.sleep(30)
    print("改好了 C:\\\\work\\\\报告.md", flush=True)
    if "坏" in task:
        print("出错了：找不到表格", file=sys.stderr, flush=True)
        sys.exit(3)
""")
SUMMARY = "Claude 把报告整理好了，放在 C:\\work\\报告.md，" + "表格和结论都写进去了，" * 8 + "没有出错"


class WeChat(FakeWeChat):
    """Enforces the real adapter's rules: one line, 100 chars unless raised for this send."""

    def send(self, text, cancel, max_chars=None):
        assert "\n" not in text and len(text) <= (max_chars or 100), text
        super().send(text, cancel)

    def validate(self):
        pass


class AgentBridgeTests(Temp):
    def make(self, replies=(), passphrase="", multi=True, summary=SUMMARY, reply_latest=False):
        from app import Bot
        settings = Settings(self.dir / 's.json')
        settings.update(multi_chat=multi, batch_seconds=0, minimum_gap_seconds=0, group_chime_in=1.0,
                        agent_passphrase=passphrase,
                        chats={'示例好友': {'mode': 'private'}, '示例群甲': {'mode': 'group'}},
                        agent_overrides={name: {"cmd": [sys.executable, "-c", AGENT, "{task}"],
                                                "workdir": str(self.dir / 'work' / name), "timeout": 20,
                                                "env": {"PYTHONIOENCODING": "utf-8"}}
                                         for name in ("claude", "codex", "deepseek")})
        bot = Bot(settings, PersonaStore(self.dir / 'p'), reply_latest,
                  log_dir=self.dir / 'logs', data_dir=self.dir / 'data')
        self.addCleanup(self.stop_all, bot)
        wx = WeChat({'示例好友': 'private', '示例群甲': 'group'})
        wx.post('示例好友', '很久以前的私聊')
        wx.post('示例群甲', '很久以前的群消息', sender='阿杰')
        bot.adapter = wx
        self.prompts, self.summaries, replies = [], [], list(replies)

        def decide(history, proactive=False, **kw):
            self.prompts.append(dict(kw, history=list(history)))
            return replies.pop(0) if replies else {'speak': False, 'text': ''}

        def summarize_job(job):
            self.summaries.append(job)
            if summary is None:
                raise RuntimeError("连不上 LM Studio")
            return summary

        bot.brain = type('B', (), {'decide': staticmethod(decide), 'learn': staticmethod(lambda *a, **k: {}),
                                   'summarize_job': staticmethod(summarize_job)})()
        bot.enabled.set()
        bot.make_runner()
        return bot, wx

    @staticmethod
    def stop_all(bot):
        for job in bot.agents.jobs():
            if job.status == "running":
                bot.agents.stop(job.id)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if all(job.finished is not None for job in bot.agents.jobs()):
                return
            time.sleep(0.02)
        raise AssertionError("fake agent did not finish during cleanup")

    def tick(self, bot, n=1):
        for _ in range(n):
            bot.collect_reports()
            bot.cycle()
            bot.since -= 10

    def wait_jobs(self, bot, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if bot.agents.jobs() and all(j.finished is not None for j in bot.agents.jobs()):
                time.sleep(0.05)  # on_done runs right after `finished` is set
                return bot.agents.jobs()
            time.sleep(0.02)
        self.fail("fake agent did not finish")

    def wait_started(self, bot, timeout=10):
        log = bot.agents.jobs()[0].log_path
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if log.exists() and "started" in log.read_text(encoding="utf-8", errors="replace"):
                return
            time.sleep(0.02)
        self.fail("slow fake agent never started")

    def owner_says(self, bot, wx, text, ticks=1):
        wx.post('示例好友', text)
        self.tick(bot, ticks)

    def finish_slow_job_in_group(self, bot, wx):
        """Leave a completed job report queued while the group chat is open."""
        self.owner_says(bot, wx, '给claude 慢慢写报告')
        self.wait_started(bot)
        wx.post('示例群甲', '小千在吗', sender='momo')
        self.tick(bot, 3)
        self.assertEqual(wx.open, '示例群甲')
        job, = bot.agents.jobs()
        self.assertTrue(bot.agents.stop(job.id))
        self.wait_jobs(bot)
        bot.collect_reports()
        self.assertEqual(len(bot.reports), 1)

    # ---- dispatch and receipts ----
    def test_command_dispatches_and_answers_without_the_model(self):
        bot, wx = self.make()
        self.tick(bot)
        self.owner_says(bot, wx, '给claude 把桌面报告整理成表格')
        self.assertEqual(wx.sent, [('示例好友', '收到，交给 Claude 了，做完告诉你')])
        self.assertEqual(self.prompts, [])
        job, = bot.agents.jobs()
        self.assertEqual((job.agent, job.task), ('claude', '把桌面报告整理成表格'))
        self.assertTrue(str(job.log_path).startswith(str(self.dir / 'data' / 'agent_logs')))

    def test_passphrase_never_reaches_history_logs_or_agent(self):
        bot, wx = self.make(passphrase='芝麻开门')
        self.tick(bot)
        self.owner_says(bot, wx, '给codex：芝麻开门：写个周报')
        self.assertEqual(wx.sent, [('示例好友', '收到，交给 Codex 了，做完告诉你')])
        self.assertEqual(bot.agents.jobs()[0].task, '写个周报')
        history = bot.runners['示例好友'].history
        self.assertIn({'role': 'user', 'text': '给codex 写个周报'}, history)
        self.wait_jobs(bot)
        self.tick(bot)
        for path in (self.dir / 'logs' / 'events.jsonl', self.dir / 'data' / 'history' / '示例好友.json'):
            self.assertNotIn('芝麻开门', path.read_text(encoding='utf-8'), path)
        self.assertNotIn('芝麻开门', json.dumps([vars(j) for j in self.summaries], default=str, ensure_ascii=False))

    def test_wrong_or_missing_passphrase_is_ordinary_chat(self):
        bot, wx = self.make([{'speak': True, 'to_me': True, 'text': '啥？'},
                             {'speak': True, 'to_me': True, 'text': '你说啥'}], passphrase='芝麻开门')
        self.tick(bot)
        self.owner_says(bot, wx, '给claude 西瓜开门 写报告')
        self.owner_says(bot, wx, '给claude 写报告')
        self.assertEqual(bot.agents.jobs(), [])
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(wx.sent, [('示例好友', '啥？'), ('示例好友', '你说啥')])

    def test_command_in_a_group_does_nothing_even_from_the_owner(self):
        bot, wx = self.make([{'speak': True, 'to_me': True, 'text': '啥任务'}])
        self.tick(bot)
        wx.post('示例群甲', '给claude 写报告', sender='示例好友')
        self.tick(bot, 3)
        self.assertEqual(bot.agents.jobs(), [])
        self.assertEqual(wx.sent, [('示例群甲', '啥任务')])

    def test_busy_agent_is_not_given_a_second_task(self):
        bot, wx = self.make()
        self.tick(bot)
        self.owner_says(bot, wx, '给claude 慢慢写报告')
        self.owner_says(bot, wx, '给claude 再写一个')
        self.assertEqual(len(bot.agents.jobs()), 1)
        self.assertIn('没派出去', wx.sent[-1][1])
        self.owner_says(bot, wx, '给codex 写周报')  # another agent is free
        self.assertEqual(wx.sent[-1], ('示例好友', '收到，交给 Codex 了，做完告诉你'))

    def test_task_list_and_stop(self):
        bot, wx = self.make()
        self.tick(bot)
        self.owner_says(bot, wx, '任务')
        self.assertEqual(wx.sent[-1], ('示例好友', '现在没有任务，最近也没有做完的'))
        self.owner_says(bot, wx, '给deepseek 慢慢整理')
        self.wait_started(bot)
        self.owner_says(bot, wx, '任务列表')
        self.assertIn('DeepSeek「慢慢整理」进行中', wx.sent[-1][1])
        self.owner_says(bot, wx, '停掉 本机')
        self.assertEqual(wx.sent[-1], ('示例好友', '好，让 DeepSeek 停下了'))
        job, = self.wait_jobs(bot)
        self.assertEqual(job.status, 'stopped')
        self.tick(bot, 2)
        self.assertEqual(wx.sent[-1], ('示例好友', 'DeepSeek「慢慢整理」已经停掉了'))
        self.assertEqual(self.summaries, [])  # nothing to summarize for a stop
        self.owner_says(bot, wx, '停掉codex')
        self.assertEqual(wx.sent[-1], ('示例好友', 'Codex 现在没有在跑的任务'))
        self.assertEqual(self.prompts, [])

    def test_job_list_reads_each_status_once(self):
        bot, _ = self.make()

        class FinishingJob:
            agent, task, started, finished = 'claude', '竞态任务', time.time(), None

            def __init__(self):
                self.reads = 0

            @property
            def status(self):
                self.reads += 1
                return 'running' if self.reads == 1 else 'done'

        job = FinishingJob()
        with mock.patch.object(bot.agents, 'jobs', return_value=[job]):
            listing = bot.job_list()
        self.assertEqual(job.reads, 1)
        self.assertEqual(listing.count('Claude「竞态任务」'), 1)
        self.assertIn('进行中', listing)

    def test_job_list_tolerates_a_missing_finished_time(self):
        bot, _ = self.make()
        job = SimpleNamespace(agent='claude', task='刚结束', status='done',
                              started=time.time(), finished=None)
        with mock.patch.object(bot.agents, 'jobs', return_value=[job]):
            listing = bot.job_list()
        self.assertIn('Claude「刚结束」做完了', listing)

    def test_stop_reports_a_job_that_finished_during_the_request(self):
        bot, _ = self.make()
        job = SimpleNamespace(id='done-now', agent='claude', status='running')
        with mock.patch.object(bot.agents, 'jobs', return_value=[job]), \
                mock.patch.object(bot.agents, 'stop', return_value=False):
            reply = bot.stop_agent('claude')
        self.assertEqual(reply, 'Claude 刚好已经结束了，汇报马上到')

    # ---- reports ----
    def test_report_in_the_owner_chat_may_be_longer_than_a_reply(self):
        bot, wx = self.make()
        self.tick(bot)
        self.owner_says(bot, wx, '给claude 写报告')
        self.wait_jobs(bot)
        self.tick(bot, 2)
        report = wx.sent[-1][1]
        self.assertEqual(wx.sent[-1][0], '示例好友')
        self.assertTrue(report.startswith('Claude「写报告」做完了：Claude 把报告整理好了'), report)
        self.assertGreater(len(report), 100)
        self.assertIn('改好了', self.summaries[0].tail)
        self.assertEqual(bot.runners['示例好友'].history[-1], {'role': 'assistant', 'text': report})

    def test_report_switches_to_the_owner_chat_in_multi_chat(self):
        bot, wx = self.make([{'speak': True, 'to_me': True, 'text': '来了'}])
        self.tick(bot)
        self.owner_says(bot, wx, '给claude 写报告')
        wx.post('示例群甲', '小千在吗', sender='momo')
        self.tick(bot, 3)
        self.assertEqual(wx.open, '示例群甲')
        self.wait_jobs(bot)
        self.tick(bot, 2)
        self.assertEqual(wx.sent[-1][0], '示例好友')
        self.assertIn('做完了', wx.sent[-1][1])
        self.assertEqual(bot.reports, [])
        self.assertEqual(len([s for s in wx.sent if '做完了' in s[1]]), 1)

    def test_report_sends_before_switching_away_from_the_owner(self):
        bot, wx = self.make()
        self.tick(bot)
        self.owner_says(bot, wx, '给claude 写报告')
        self.wait_jobs(bot)
        wx.post('示例群甲', '小千在吗', sender='momo')
        self.tick(bot, 3)
        sent = [item for item in wx.sent if '做完了' in item[1]]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], '示例好友')
        self.assertEqual(list(bot.runners['示例好友'].notices), [])

    def test_owner_notices_return_to_reports_after_an_external_switch(self):
        bot, wx = self.make()
        self.tick(bot)
        bot.runners['示例好友'].notices.extend(['第一条任务消息', '第二条任务消息'])
        bot.go('示例群甲')
        self.tick(bot, 2)
        sent = [text for chat, text in wx.sent if chat == '示例好友']
        self.assertEqual(sent, ['第一条任务消息', '第二条任务消息'])
        self.assertEqual(bot.reports, [])
        self.assertEqual(list(bot.runners['示例好友'].notices), [])

    def test_report_retries_after_a_definite_presend_failure(self):
        bot, wx = self.make()
        self.tick(bot)
        self.finish_slow_job_in_group(bot, wx)
        original_send, attempts = wx.send, 0

        def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError('发送前校验失败')
            return original_send(*args, **kwargs)

        with mock.patch.object(wx, 'send', side_effect=fail_once):
            bot.cycle()
            self.assertEqual(len(bot.reports), 1)
            self.assertFalse(bot.enabled.is_set())
            self.assertGreater(bot.report_retry_at, time.monotonic())
            bot.enabled.set()
            bot.cycle()
            self.assertEqual(attempts, 1)  # backoff suppresses a tight retry loop
            self.assertEqual(len(bot.reports), 1)
            bot.report_retry_at = 0
            bot.cycle()
        reports = [item for item in wx.sent if '已经停掉了' in item[1]]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0][0], '示例好友')
        self.assertEqual(bot.reports, [])

    def test_uncertain_report_is_not_retried(self):
        from wechat_adapter import SendUncertain

        bot, wx = self.make()
        self.tick(bot)
        self.finish_slow_job_in_group(bot, wx)
        with mock.patch.object(wx, 'send', side_effect=SendUncertain('结果不确定')):
            bot.cycle()
        self.assertEqual(bot.reports, [])
        self.assertFalse(bot.enabled.is_set())
        bot.enabled.set()
        bot.report_retry_at = 0
        self.tick(bot, 2)
        self.assertEqual([item for item in wx.sent if '已经停掉了' in item[1]], [])

    def test_owner_notice_retries_after_a_definite_presend_failure(self):
        bot, wx = self.make()
        self.tick(bot)
        bot.runner.notices.append('确定没发的任务消息')
        original_send, attempts = wx.send, 0

        def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError('发送前校验失败')
            return original_send(*args, **kwargs)

        with mock.patch.object(wx, 'send', side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, '发送前校验失败'):
                bot.cycle()
            self.assertEqual(list(bot.runner.notices), ['确定没发的任务消息'])
            bot.enabled.set()
            bot.cycle()  # resume the runner after the pause
            bot.cycle()  # retry the retained notice
        self.assertEqual(list(bot.runner.notices), [])
        self.assertEqual(wx.sent.count(('示例好友', '确定没发的任务消息')), 1)

    def test_uncertain_owner_notice_is_not_retried(self):
        from wechat_adapter import SendUncertain

        bot, wx = self.make()
        self.tick(bot)
        bot.runner.notices.append('结果不确定的任务消息')
        with mock.patch.object(wx, 'send', side_effect=SendUncertain('结果不确定')):
            with self.assertRaises(SendUncertain):
                bot.cycle()
        self.assertEqual(list(bot.runner.notices), [])
        self.assertFalse(bot.enabled.is_set())
        bot.enabled.set()
        self.tick(bot, 2)
        self.assertNotIn(('示例好友', '结果不确定的任务消息'), wx.sent)

    def test_report_waits_for_the_owner_chat_in_single_chat_mode(self):
        bot, wx = self.make(multi=False)
        self.tick(bot)
        self.owner_says(bot, wx, '给claude 写报告')
        bot.go('示例群甲')
        self.tick(bot)
        self.wait_jobs(bot)
        self.tick(bot, 3)
        self.assertEqual([s for s in wx.sent if '做完了' in s[1]], [])
        self.assertEqual(len(bot.reports), 1)
        wx.open = '示例好友'  # the owner opens their chat again
        self.tick(bot, 2)
        self.assertEqual(wx.sent[-1][0], '示例好友')
        self.assertIn('做完了', wx.sent[-1][1])
        self.assertEqual(bot.reports, [])

    def test_failed_summary_falls_back_to_status_and_last_lines(self):
        bot, wx = self.make(summary=None)
        self.tick(bot)
        self.owner_says(bot, wx, '给codex 坏任务')
        job, = self.wait_jobs(bot)
        self.assertEqual(job.status, 'failed')
        self.tick(bot, 2)
        report = wx.sent[-1][1]
        self.assertTrue(report.startswith('Codex「坏任务」失败了：退出码 3'), report)
        self.assertIn('出错了：找不到表格', report)
        self.assertIn(job.log_path.name, report)

    def test_reports_wait_while_paused(self):
        bot, wx = self.make()
        self.tick(bot)
        self.owner_says(bot, wx, '给claude 写报告')
        bot.enabled.clear()
        self.wait_jobs(bot)
        bot.collect_reports()
        self.assertEqual(len(bot.reports), 1)
        self.assertEqual(len(wx.sent), 1)
        bot.enabled.set()
        self.tick(bot, 3)
        self.assertIn('做完了', wx.sent[-1][1])

    # ---- what must not trigger ----
    def test_command_sent_during_a_pause_is_not_run(self):
        for multi in (True, False):
            with self.subTest(multi=multi):
                bot, wx = self.make(multi=multi)
                self.tick(bot)
                bot.enabled.clear()
                wx.post('示例好友', '给claude 写报告')
                with mock.patch.object(bot, 'ensure_model'):
                    bot.handle(('start', bot.enabled.epoch))  # what “开始 / 继续” does
                self.tick(bot, 2)
                self.assertEqual(bot.agents.jobs(), [])
                self.assertEqual(wx.sent, [])
                self.assertIn({'role': 'user', 'text': '给claude 写报告'}, bot.runners['示例好友'].history)
                if multi:
                    continue  # the scheduler only resumes through “start”, which resets its queue
                # A step-level resume (epoch changed under the runner) is context only too.
                bot.enabled.clear()
                wx.post('示例好友', '给codex 写周报')
                bot.enabled.set()
                self.tick(bot, 2)
                self.assertEqual(bot.agents.jobs(), [])

    def test_unread_command_from_the_pause_is_not_run_after_start(self):
        """Multi-chat “start” queues chats with an unread badge; their commands are context only."""
        bot, wx = self.make([{'speak': True, 'to_me': True, 'text': '来了'}])
        unread = {}
        wx.session_details = lambda: {c: {'preview': rows[-1].text if rows else '', 'unread': unread.get(c, 0)}
                                      for c, rows in wx.chats.items()}
        self.tick(bot)
        wx.post('示例群甲', '小千在吗', sender='momo')
        self.tick(bot, 3)
        self.assertEqual(wx.open, '示例群甲')
        bot.enabled.clear()
        wx.post('示例好友', '给claude 写报告')
        unread['示例好友'] = 1
        with mock.patch.object(bot, 'ensure_model'):
            bot.handle(('start', bot.enabled.epoch))
        self.tick(bot, 3)
        unread['示例好友'] = 0
        self.assertEqual(bot.agents.jobs(), [])
        self.assertIn({'role': 'user', 'text': '给claude 写报告'}, bot.runners['示例好友'].history)
        self.assertEqual(wx.open, '示例好友')
        self.owner_says(bot, wx, '给claude 写周报')  # a fresh command after start still runs
        self.assertEqual([j.task for j in bot.agents.jobs()], ['写周报'])

    def assert_reply_latest_command_is_replayed(self, entry):
        bot, wx = self.make(reply_latest=True)
        wx.post('示例好友', '给claude 写启动前的报告')
        if entry == 'prime':
            bot.runner.prime(wx.read())
        else:
            bot.runner.step()
        self.assertEqual(bot.agents.jobs(), [])
        self.assertEqual(wx.sent, [])
        self.assertIn({'role': 'user', 'text': '给claude 写启动前的报告'}, bot.runner.history)

    def test_reply_latest_prime_logs_an_agent_command_without_running_it(self):
        self.assert_reply_latest_command_is_replayed('prime')

    def test_reply_latest_step_logs_an_agent_command_without_running_it(self):
        self.assert_reply_latest_command_is_replayed('step')

    def assert_reply_latest_answers_ordinary_chat(self, entry):
        bot, wx = self.make([{'speak': True, 'to_me': True, 'text': '刚看到'}], reply_latest=True)
        wx.post('示例好友', '这是普通消息')
        if entry == 'prime':
            bot.runner.prime(wx.read())
        else:
            bot.runner.step()
        bot.runner.step()
        self.assertEqual(bot.agents.jobs(), [])
        self.assertEqual(wx.sent, [('示例好友', '刚看到')])
        self.assertEqual(len(self.prompts), 1)

    def test_reply_latest_prime_still_answers_an_ordinary_message(self):
        self.assert_reply_latest_answers_ordinary_chat('prime')

    def test_reply_latest_step_still_answers_an_ordinary_message(self):
        self.assert_reply_latest_answers_ordinary_chat('step')

    def test_agents_switched_off_means_ordinary_chat(self):
        bot, wx = self.make([{'speak': True, 'to_me': True, 'text': '好'}])
        bot.settings.update(agents_enabled=False)
        self.tick(bot)
        self.owner_says(bot, wx, '给claude 写报告')
        self.assertEqual(bot.agents.jobs(), [])
        self.assertEqual(wx.sent, [('示例好友', '好')])

    def test_overrides_reach_the_agent_config(self):
        bot, _ = self.make()
        bot.settings.update(agent_overrides={"codex": {"model": "gpt-test"}})
        configs = bot.agent_configs()
        self.assertEqual(configs["codex"]["model"], "gpt-test")
        self.assertEqual(configs["codex"]["reasoning"], "high")

    # ---- owner sends: "给X发：…" to any chat ----
    def with_contact(self, wx, name='示例联系人'):
        """A WeChat chat the bot is not watching (not in settings["chats"])."""
        wx.chats[name], wx.modes[name] = [], 'private'

    def test_owner_send_reaches_an_unwatched_chat_verbatim(self):
        for multi in (True, False):
            with self.subTest(multi=multi):
                bot, wx = self.make(multi=multi)
                self.with_contact(wx)
                self.tick(bot)
                self.owner_says(bot, wx, '去给示例联系人私聊发：早点休息🌙，晚安啦', ticks=3)
                self.assertIn(('示例联系人', '早点休息🌙，晚安啦'), wx.sent)
                self.assertEqual(wx.sent[-1], ('示例好友', '已发给「示例联系人」：早点休息🌙，晚安啦'))
                self.assertEqual(wx.open, '示例好友')
                self.assertEqual(self.prompts, [])  # never the model, so never a false "sent"
                self.assertNotIn('示例联系人', bot.settings['chats'])

    def test_owner_send_phrasings(self):
        bot, _ = self.make()
        for text, chat, body in [('给示例联系人发：明天见', '示例联系人', '明天见'),
                                 ('你给示例联系人私聊发：快睡', '示例联系人', '快睡'),
                                 ('跟示例群甲说: 今晚八点', '示例群甲', '今晚八点'),
                                 ('帮我给 文件传输助手 发消息：测试', '文件传输助手', '测试')]:
            bot.relays.clear()
            self.assertEqual(bot.agent_command(text), {'log': text, 'reply': None}, text)
            self.assertEqual(bot.relays, [(chat, body, True)], text)
        for text in ('给我发：好的', '你没发啊', '去群里说句话', '给claude发：写周报', '给示例联系人发'):
            bot.relays.clear()
            bot.agent_command(text)
            self.assertFalse([r for r in bot.relays if len(r) == 3], text)

    def test_owner_send_failure_is_reported_truthfully(self):
        bot, wx = self.make()
        self.tick(bot)
        opened = wx.open_chat

        def no_such_chat(name):
            if name == '查无此人':
                raise RuntimeError('搜索结果不唯一或不存在')
            opened(name)

        wx.open_chat = no_such_chat
        self.owner_says(bot, wx, '给查无此人发：你好', ticks=3)
        self.assertEqual(wx.sent, [('示例好友', '没发给「查无此人」：搜索结果不唯一或不存在')])
        self.assertEqual(wx.open, '示例好友')

    def test_uncertain_owner_send_is_not_claimed_or_retried(self):
        from wechat_adapter import SendUncertain
        bot, wx = self.make()
        self.with_contact(wx)
        self.tick(bot)
        original = wx.send

        def uncertain(text, cancel, max_chars=None):
            if wx.open == '示例联系人':
                raise SendUncertain('发送结果不确定')
            return original(text, cancel, max_chars)

        wx.send = uncertain
        self.owner_says(bot, wx, '给示例联系人发：晚安')
        self.assertFalse(bot.enabled.is_set())  # an uncertain send pauses, as before
        bot.enabled.set()
        self.tick(bot, 3)
        self.assertEqual([s for s in wx.sent if s[0] == '示例联系人'], [])
        self.assertEqual(wx.sent[-1], ('示例好友', '发给「示例联系人」的结果不确定，没有重发，请看一下微信'))

    def test_owner_send_from_a_pause_or_a_group_does_nothing(self):
        bot, wx = self.make()
        self.with_contact(wx)
        self.tick(bot)
        self.assertIsNone(bot.agent_command('给示例联系人发：晚安', act=False)['reply'])
        self.assertEqual(bot.relays, [])
        wx.post('示例群甲', '给示例联系人发：晚安', sender='示例群友')
        self.tick(bot, 3)
        self.assertNotIn('示例联系人', [chat for chat, _ in wx.sent])
