"""agents.py 的离线单元测试。

全部用当前 Python 解释器 + -c 充当假 agent，不调用 claude/codex/deepseek，
不联网、不安装依赖。运行：

    python -m unittest test_agents -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agents import AgentJobs, DEFAULT_ROUTES, find_codex, parse_command

UTF8_ENV = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


class DefaultConfigTests(unittest.TestCase):
    def test_claude_task_follows_an_end_of_options_marker(self):
        # --tools takes several values; without "--" the real CLI swallows the task as a tool name.
        from agents import default_configs
        cmd = default_configs()["claude"]["cmd"]
        self.assertEqual(cmd[-2:], ["--", "{task}"])


class ParseCommandTests(unittest.TestCase):
    def test_default_routes_and_separators(self):
        cases = [
            ("给codex 整理报告", ("codex", "整理报告")),
            ("给claude:整理报告", ("claude", "整理报告")),
            ("给deepseek：整理报告", ("deepseek", "整理报告")),
            ("给本机,整理报告", ("deepseek", "整理报告")),
            ("给本机，整理报告", ("deepseek", "整理报告")),
            ("给CODEX\t整理报告", ("codex", "整理报告")),
            ("给codex::整理报告", ("codex", "整理报告")),
            ("给codex整理报告", ("codex", "整理报告")),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_command(text, DEFAULT_ROUTES), expected)

    def test_task_text_is_preserved(self):
        text = '给claude: 把 "桌面报告" & 表格 | 2026 整理好'
        self.assertEqual(
            parse_command(text, DEFAULT_ROUTES),
            ("claude", '把 "桌面报告" & 表格 | 2026 整理好'),
        )
        self.assertEqual(
            parse_command("给codex   写  一个 报告  ", DEFAULT_ROUTES),
            ("codex", "写  一个 报告"),
        )
        self.assertEqual(
            parse_command("  给deepseek 任务", DEFAULT_ROUTES),
            ("deepseek", "任务"),
        )

    def test_non_commands(self):
        for text in ["", "   ", "你好", "codex 任务", "给codex", "给codex   ", "给codex:：, "]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text, DEFAULT_ROUTES))

    def test_custom_routes_and_longest_prefix(self):
        routes = {"给agent": "short", "给Agent 高级": "long"}
        self.assertEqual(
            parse_command("给AGENT 高级 做点事", routes),
            ("long", "做点事"),
        )
        self.assertEqual(parse_command("给agent 做点事", routes), ("short", "做点事"))
        self.assertIsNone(parse_command("给 agent 做点事", routes))
        self.assertIsNone(parse_command("给agent 做点事", {}))

    def test_passphrase_correct(self):
        passphrase = "芝麻开门"
        self.assertEqual(
            parse_command("给claude 芝麻开门 把桌面报告整理成表格", DEFAULT_ROUTES, passphrase),
            ("claude", "把桌面报告整理成表格"),
        )
        self.assertEqual(
            parse_command("给claude：芝麻开门：把桌面报告整理成表格", DEFAULT_ROUTES, passphrase),
            ("claude", "把桌面报告整理成表格"),
        )
        self.assertEqual(
            parse_command("给claude，芝麻开门,任务", DEFAULT_ROUTES, passphrase),
            ("claude", "任务"),
        )

    def test_passphrase_missing_or_wrong(self):
        passphrase = "芝麻开门"
        for text in [
            "给claude 把桌面报告整理成表格",       # 缺口令
            "给claude 西瓜开门 把桌面报告整理成表格",  # 错口令
            "给claude 芝麻开门",                  # 只有口令
            "给claude 芝麻开门任务",              # 口令后没有分隔符
            "给claude芝麻开门 任务",              # 开头后没有分隔符
        ]:
            with self.subTest(text=text):
                self.assertIsNone(parse_command(text, DEFAULT_ROUTES, passphrase))

        # 不要求口令时，同一句话里的“芝麻开门”就是任务的一部分。
        self.assertEqual(
            parse_command("给claude 芝麻开门任务", DEFAULT_ROUTES, ""),
            ("claude", "芝麻开门任务"),
        )


class FindCodexTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.base = self.root / "OpenAI" / "Codex" / "bin"
        self.base.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_codex(self, directory: str, mtime: float) -> Path:
        folder = self.base / directory
        folder.mkdir()
        exe = folder / "codex.exe"
        exe.write_bytes(b"")
        os.utime(exe, (mtime, mtime))
        return exe

    def test_picks_newest_codex_exe(self):
        self.make_codex("old", 1_600_000_000)
        newest = self.make_codex("new", 1_700_000_000)
        self.make_codex("middle", 1_650_000_000)
        self.assertEqual(Path(find_codex()), newest)

    def test_refinds_every_call_without_cache(self):
        first = self.make_codex("a", 1_600_000_000)
        self.assertEqual(Path(find_codex()), first)
        second = self.make_codex("b", 1_700_000_000)
        self.assertEqual(Path(find_codex()), second)

    def test_falls_back_to_path(self):
        with mock.patch("agents.shutil.which", return_value=r"C:\tools\codex.exe"):
            self.assertEqual(find_codex(), r"C:\tools\codex.exe")


class AgentJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.logs = self.root / "logs"
        self._job_managers: list[AgentJobs] = []

    def tearDown(self):
        for manager in self._job_managers:
            for job in manager.jobs():
                if job.status == "running":
                    try:
                        manager.stop(job.id)
                    except Exception:
                        pass

    # -- 工具 --------------------------------------------------------------

    def make_config(self, script: str, timeout: float = 10, env: dict | None = None) -> dict:
        cmd = [sys.executable, "-c", textwrap.dedent(script), "{task}"]
        merged_env = dict(UTF8_ENV)
        if env:
            merged_env.update(env)
        return {
            "fake": {
                "cmd": cmd,
                "workdir": str(self.root / "work"),
                "timeout": timeout,
                "env": merged_env,
            }
        }

    def make_jobs(self, configs: dict, callback=None):
        events = []
        done = threading.Event()

        def wrapped(job):
            events.append(job)
            try:
                if callback is not None:
                    callback(job)
            finally:
                done.set()

        manager = AgentJobs(configs, self.logs, wrapped)
        self._job_managers.append(manager)
        return manager, events, done

    def wait(self, manager: AgentJobs, job_id: str, timeout: float = 15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for job in manager.jobs():
                if job.id == job_id and job.finished is not None:
                    return job
            time.sleep(0.02)
        self.fail(f"任务 {job_id} 未在 {timeout}s 内结束")

    def read_log(self, job_id: str) -> str:
        # newline="" 保留子进程实际写出的 \r\n，才能和 job.output 做逐字比较。
        with open(self.logs / f"{job_id}.log", "r", encoding="utf-8", newline="") as handle:
            return handle.read()

    def wait_for_log(self, job_id: str, needle: str, timeout: float = 10) -> bool:
        path = self.logs / f"{job_id}.log"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if needle in path.read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                pass
            time.sleep(0.02)
        return False

    # -- 测试 --------------------------------------------------------------

    def test_success_writes_log_and_fields(self):
        manager, events, done = self.make_jobs(
            self.make_config(
                """
                import sys
                print("stdout 中文", flush=True)
                print("stderr 中文", file=sys.stderr, flush=True)
                """
            )
        )
        job_id = manager.start("fake", "做点事")
        job = self.wait(manager, job_id)

        self.assertEqual(job.status, "done")
        self.assertEqual(job.returncode, 0)
        self.assertEqual(job.agent, "fake")
        self.assertEqual(job.task, "做点事")
        self.assertIsNotNone(job.started)
        self.assertIsNotNone(job.finished)
        self.assertGreaterEqual(job.finished, job.started)
        for field in (
            "id",
            "agent",
            "task",
            "status",
            "returncode",
            "started",
            "finished",
            "output",
            "tail",
        ):
            self.assertTrue(hasattr(job, field), field)
        self.assertIn("stdout 中文", job.output)
        self.assertIn("stderr 中文", job.output)
        self.assertEqual(job.tail, job.output[-1500:])
        self.assertTrue(done.wait(5))
        self.assertEqual([item.id for item in events], [job_id])

        self.assertTrue((self.logs / f"{job_id}.log").is_file())
        self.assertEqual(self.read_log(job_id), job.output)

    def test_task_is_single_argument_with_special_characters(self):
        script = """
        import json
        import sys
        print(json.dumps(sys.argv[1:], ensure_ascii=False))
        """
        task = (
            '第一行 "双引号" & 空格 | 管道 <a> > b ^ %PATH% ! (x) '
            "'单引号' {workdir}\n第二行 {task}"
        )
        manager, _, _ = self.make_jobs(self.make_config(script))
        job_id = manager.start("fake", task)
        job = self.wait(manager, job_id)

        self.assertEqual(job.status, "done")
        argv = json.loads(job.output.strip())
        self.assertEqual(argv, [task])

    def test_workdir_and_env_are_used(self):
        script = """
        import os
        print(os.getcwd())
        print(os.environ.get("AGENT_TEST_VAR"))
        """
        manager, _, _ = self.make_jobs(
            self.make_config(script, env={"AGENT_TEST_VAR": "中文值"})
        )
        job_id = manager.start("fake", "检查")
        job = self.wait(manager, job_id)

        lines = job.output.strip().splitlines()
        expected = os.path.normcase(str((self.root / "work").resolve()))
        self.assertEqual(os.path.normcase(lines[0]), expected)
        self.assertEqual(lines[1], "中文值")

    def test_model_and_reasoning_placeholders(self):
        script = """
        import json
        import sys
        print(json.dumps(sys.argv[1:], ensure_ascii=False))
        """
        task = "做 {task} 和 {workdir} 字面量"
        configs = {
            "fake": {
                "cmd": [
                    sys.executable,
                    "-c",
                    textwrap.dedent(script),
                    "{model}",
                    "effort={reasoning}",
                    "{workdir}",
                    "{task}",
                ],
                "workdir": str(self.root / "work2"),
                "timeout": 10,
                "model": "test-model",
                "reasoning": "high",
                "env": dict(UTF8_ENV),
            }
        }
        manager, _, _ = self.make_jobs(configs)
        job_id = manager.start("fake", task)
        job = self.wait(manager, job_id)

        self.assertEqual(job.status, "done")
        argv = json.loads(job.output.strip())
        self.assertEqual(
            argv,
            ["test-model", "effort=high", str((self.root / "work2").resolve()), task],
        )

    def test_unknown_placeholder_fails_cleanly(self):
        manager, events, done = self.make_jobs(
            {
                "fake": {
                    "cmd": [sys.executable, "-c", "print('x')", "{nope}"],
                    "workdir": str(self.root / "work"),
                    "timeout": 10,
                }
            }
        )
        job_id = manager.start("fake", "任务")
        job = self.wait(manager, job_id)

        self.assertEqual(job.status, "failed")
        self.assertIsNone(job.returncode)
        self.assertIn("占位符", job.output)
        self.assertTrue(done.wait(5))
        self.assertEqual([item.id for item in events], [job_id])
        self.assertEqual(self.read_log(job_id), job.output)
        self.assertIn("占位符", self.read_log(job_id))

    def test_failure_status_and_stderr(self):
        script = """
        import sys
        print("out", flush=True)
        print("err 中文", file=sys.stderr, flush=True)
        sys.exit(3)
        """
        manager, _, _ = self.make_jobs(self.make_config(script))
        job_id = manager.start("fake", "失败")
        job = self.wait(manager, job_id)

        self.assertEqual(job.status, "failed")
        self.assertEqual(job.returncode, 3)
        self.assertIn("out", job.output)
        self.assertIn("err 中文", job.output)
        self.assertEqual(self.read_log(job_id), job.output)

    def test_timeout_kills_process(self):
        script = """
        import time
        print("started", flush=True)
        time.sleep(30)
        """
        manager, events, done = self.make_jobs(self.make_config(script, timeout=1.0))
        job_id = manager.start("fake", "超时")
        job = self.wait(manager, job_id, timeout=15)

        self.assertEqual(job.status, "timeout")
        self.assertIsNotNone(job.returncode)
        self.assertIsNotNone(job.finished)
        self.assertLess(job.finished - job.started, 8)
        self.assertTrue(done.wait(5))
        self.assertEqual([item.id for item in events], [job_id])

    def test_invalid_timeout_fails_cleanly(self):
        configs = self.make_config("print('x')")
        configs["fake"]["timeout"] = "不是数字"
        manager, events, done = self.make_jobs(configs)
        job_id = manager.start("fake", "坏超时")
        job = self.wait(manager, job_id, timeout=10)

        self.assertEqual(job.status, "failed")
        self.assertIn("timeout", job.output)
        self.assertTrue(done.wait(5))
        self.assertEqual([item.id for item in events], [job_id])

    def test_manual_stop_kills_process(self):
        script = """
        import time
        print("ready", flush=True)
        time.sleep(30)
        """
        manager, events, done = self.make_jobs(self.make_config(script))
        job_id = manager.start("fake", "停止")
        self.assertTrue(self.wait_for_log(job_id, "ready"))
        self.assertTrue(manager.stop(job_id))
        job = self.wait(manager, job_id, timeout=15)

        self.assertEqual(job.status, "stopped")
        self.assertFalse(manager.stop(job_id))
        self.assertTrue(done.wait(5))
        self.assertEqual([item.id for item in events], [job_id])

    def test_duplicate_agent_is_rejected_then_reusable(self):
        script = """
        import time
        time.sleep(30)
        """
        manager, _, _ = self.make_jobs(self.make_config(script))
        first = manager.start("fake", "第一个")
        with self.assertRaises(RuntimeError) as ctx:
            manager.start("fake", "第二个")
        self.assertIn("已有任务在运行", str(ctx.exception))

        manager.stop(first)
        self.wait(manager, first)

        second = manager.start("fake", "第三个")
        self.assertNotEqual(first, second)
        manager.stop(second)
        self.wait(manager, second)

    def test_unknown_agent_and_empty_task(self):
        manager, _, _ = self.make_jobs(self.make_config("print('x')"))
        with self.assertRaises(RuntimeError):
            manager.start("不存在的agent", "任务")
        with self.assertRaises(RuntimeError):
            manager.stop("不存在的任务")
        with self.assertRaises(ValueError):
            manager.start("fake", "   ")

    def test_on_done_exception_does_not_break_later_jobs(self):
        calls = []

        def callback(job):
            calls.append(job.id)
            if len(calls) == 1:
                raise RuntimeError("回调故意报错")

        manager, events, done = self.make_jobs(
            self.make_config("print('ok', flush=True)"), callback=callback
        )
        with self.assertLogs("agents", level="ERROR") as captured:
            first = manager.start("fake", "一")
            self.wait(manager, first)
            self.assertTrue(done.wait(5))

            done.clear()
            second = manager.start("fake", "二")
            self.wait(manager, second)
            self.assertTrue(done.wait(5))
            time.sleep(0.1)

        self.assertEqual(calls, [first, second])
        self.assertEqual(len(events), 2)
        self.assertTrue(any("on_done" in line for line in captured.output))
        self.assertEqual([manager.jobs()[0].status, manager.jobs()[1].status], ["done", "done"])

    def test_invalid_utf8_is_replaced(self):
        script = """
        import sys
        sys.stdout.buffer.write(b"ok \\xff\\n")
        sys.stdout.buffer.flush()
        """
        manager, _, _ = self.make_jobs(self.make_config(script))
        job_id = manager.start("fake", "编码")
        job = self.wait(manager, job_id)

        self.assertEqual(job.status, "done")
        self.assertIn("ok \ufffd", job.output)
        self.assertEqual(self.read_log(job_id), job.output)

    def test_tail_is_last_1500_characters(self):
        manager, _, _ = self.make_jobs(self.make_config("print('x' * 2000)"))
        job_id = manager.start("fake", "长输出")
        job = self.wait(manager, job_id)

        self.assertEqual(len(job.tail), 1500)
        self.assertEqual(job.tail, job.output[-1500:])

    def test_jobs_are_newest_first(self):
        manager, _, _ = self.make_jobs(self.make_config("print('ok')"))
        first = manager.start("fake", "一")
        self.wait(manager, first)
        second = manager.start("fake", "二")
        self.wait(manager, second)

        self.assertEqual([job.id for job in manager.jobs()], [second, first])


if __name__ == "__main__":
    unittest.main(verbosity=2)
