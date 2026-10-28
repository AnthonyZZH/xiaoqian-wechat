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


def valid_file(raw, work):
    try:
        path = Path(raw)
        if not path.is_absolute():
            return None
        root = Path(work).resolve(strict=True)
        path = path.resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return path
    except (OSError, ValueError, RuntimeError):
        return None


def output_file(output, work):
    """Accept only a final FILE line naming an existing file inside the work tree."""
    lines = output.rstrip().splitlines()
    return valid_file(lines[-1][5:].strip(), work) if lines and lines[-1].startswith('FILE:') else None


class ServerAgent:
    def __init__(self, root, key_env, report):
        self.root, self.key_env, self.report = Path(root), key_env, report
        self.lock = threading.Lock()
        self.busy = None

    def start(self, task, depth=1):
        # Any number of tasks at once: each runs in its own thread with its own log.
        job = uuid.uuid4().hex[:8]
        threading.Thread(target=self.run, args=(job, task, depth), daemon=True).start()
        return '收到，交给本机去做了，做完告诉你'

    def run(self, job, task, depth=1):
        work, logs = self.root / 'work', self.root / 'logs'
        work.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, DSH_HOME=str(self.root / 'dsh-home'), DSH_TELEMETRY_DISABLED='1',
                   DSH_PERMISSION_MODE='danger-full-access',
                   DEEPSEEK_API_KEY=os.environ.get(self.key_env, ''))
        instructions = (r'你在一台 Windows 服务器上，有完整权限。需要插件时可执行 '
                        r'C:\xiaoqian\app\dsh\node_modules\.bin\dsh.cmd plugin --profile headless add <包名>'
                        r' 安装，DSH_HOME=C:\xiaoqian\data\agent\dsh-home，装完下一次任务生效。'
                        r'完成任务缺少能力时自己想办法：可以安装 dsh 插件、pip/npm 包或写脚本；装好后继续把任务做完。'
                        r'截图可用 Python PIL.ImageGrab 截当前桌面（你运行在有桌面的会话里）。不要声称做了没做的事。'
                        r'需要交给主人看的文件（截图、图片或文档）保存到 C:\xiaoqian\data\agent\work\out\ 下，'
                        r'最终回答最后单独一行输出 FILE: <绝对路径>。'
                        '\n\n')
        started = time.monotonic()
        try:
            done = subprocess.run([str(NODE), str(DSH_BIN), 'headless', instructions + task], cwd=work, env=env,
                                  capture_output=True, text=True, encoding='utf-8', errors='replace',
                                  timeout=TIMEOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            (logs / f'{job}.log').write_text(done.stdout + '\n' + done.stderr, encoding='utf-8')
            file = output_file(done.stdout, work) if done.returncode == 0 else None
            stdout = done.stdout.rstrip()
            file_declared = bool(stdout.splitlines() and stdout.splitlines()[-1].startswith('FILE:'))
            if file_declared:
                stdout = '\n'.join(stdout.splitlines()[:-1])
            text = answer(stdout)
            success = done.returncode == 0 and bool(text or file) and not (file_declared and not file)
            failure = '本机声明的文件不存在或不在允许的工作目录内。' if file_declared and not file else '本机任务没有成功完成。'
            self.report({'task':task[:1000], 'result':text[:1800] if success else (failure + text[:1600]),
                         'file':str(file) if file else '', 'success':success, 'depth':depth})
        except subprocess.TimeoutExpired:
            self.report({'task':task[:1000], 'result':'本机任务超时，没有完成。', 'file':'', 'success':False, 'depth':depth})
        except Exception:
            self.report({'task':task[:1000], 'result':'本机任务启动或执行失败。', 'file':'', 'success':False, 'depth':depth})
