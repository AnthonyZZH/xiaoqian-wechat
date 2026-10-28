"""Owner-only tasks for the DeepSeek Harness installed on this server.

One headless task at a time, in the background: "给本机：整理一下…" runs
`dsh headless <task>` in data/agent/work and reports a one-line result to the
owner's private chat. No shell: node runs DSH's entry script with an argument list.
"""
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

DSH_BIN = Path(r'C:\xiaoqian\app\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js')
NODE = Path(r'C:\Program Files\nodejs\node.exe')
PREFIX = re.compile(r'^\s*(?:给本机|给服务器|给deepseek|给ds|让本机|让ds|任务)\s*[:：,，]?\s*(?P<task>\S.*)$', re.I | re.S)
TIMEOUT = None  # the owner wants no time limit: a task runs until DSH finishes it


def answer(output):
    """The final answer only: drop DSH's status lines and reasoning blocks."""
    keep, skipping = [], False
    for line in output.splitlines():
        if line.startswith('dsh: reasoning'):
            skipping = True
            continue
        if line.startswith(('dsh:', '[llm-')):
            continue
        if skipping and not line.strip():
            skipping = False
            continue
        if not skipping:
            keep.append(line.strip())
    return ' '.join(part for part in keep if part)


class ServerAgent:
    def __init__(self, root, key_env, report):
        self.root, self.key_env, self.report = Path(root), key_env, report
        self.lock = threading.Lock()
        self.busy = None

    def start(self, task):
        with self.lock:
            if self.busy:
                return '本机还在忙上一件事，做完再接新的哦'
            self.busy = uuid.uuid4().hex[:8]
            job = self.busy
        threading.Thread(target=self.run, args=(job, task), daemon=True).start()
        return '收到，交给本机去做了，做完告诉你'

    def run(self, job, task):
        work, logs = self.root / 'work', self.root / 'logs'
        work.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, DSH_HOME=str(self.root / 'dsh-home'), DSH_TELEMETRY_DISABLED='1',
                   DSH_PERMISSION_MODE='danger-full-access',
                   DEEPSEEK_API_KEY=os.environ.get(self.key_env, ''))
        instructions = (r'你在一台 Windows 服务器上，有完整权限。需要插件时可执行 '
                        r'C:\xiaoqian\app\dsh\node_modules\.bin\dsh.cmd plugin --profile headless add <包名>'
                        r' 安装，DSH_HOME=C:\xiaoqian\data\agent\dsh-home，装完下一次任务生效。'
                        '\n\n')
        started = time.monotonic()
        try:
            done = subprocess.run([str(NODE), str(DSH_BIN), 'headless', instructions + task], cwd=work, env=env,
                                  capture_output=True, text=True, encoding='utf-8', errors='replace',
                                  timeout=TIMEOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            (logs / f'{job}.log').write_text(done.stdout + '\n' + done.stderr, encoding='utf-8')
            text = answer(done.stdout) or answer(done.stderr)
            result = (text[:300] + '…') if len(text) > 300 else text
            minutes = max(1, round((time.monotonic() - started) / 60))
            self.report(f'本机做完了（{minutes}分钟）：{result}' if done.returncode == 0 and result
                        else '本机这件事没做成，日志在服务器 data/agent/logs/' + job + '.log')
        except subprocess.TimeoutExpired:
            self.report('本机这件事超过15分钟还没做完，先停下了')
        except Exception:
            self.report('本机这件事没能启动')
        finally:
            with self.lock:
                self.busy = None
