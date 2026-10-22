"""把任务交给本机命令行 agent，后台运行，做完回调。

设计要点：
- 只用标准库。
- 子进程一律用参数列表启动，绝不使用 shell=True；任务文本只作为一个参数传入。
- Windows 下子进程加 CREATE_NO_WINDOW，停止/超时用 taskkill /T /F 结束整个进程树。
- 每个任务把 stdout/stderr 合并后以 UTF-8（errors="replace"）完整写入
  log_dir/<job_id>.log，并在完成后调用 on_done(job)。

默认四个 agent：
- claude:   Claude Code 非交互模式（-p），--restricted 限制在 cwd 内，
            并且不启用 Bash/PowerShell，因此不会自动执行删除类命令。
- codex:    Codex 应用自带的 codex-cli（每次启动重新 find_codex），
            exec + workspace-write 沙箱 + -C 工作目录。
- draw:     与 codex 相同的命令，但使用独立工作目录和较短超时，专门生成图片。
- deepseek: DeepSeek Harness 的 headless 模式，cwd 就是沙箱 workspaceRoot，
            DSH_PERMISSION_MODE=workspace-write。
"""

from __future__ import annotations

import codecs
import copy
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

__all__ = [
    "AgentJob",
    "AgentJobs",
    "DEFAULT_CONFIGS",
    "DEFAULT_ROUTES",
    "default_configs",
    "draw_task",
    "find_claude",
    "find_codex",
    "find_dsh_paths",
    "find_image",
    "parse_command",
]

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
AGENT_WORK_ROOT = ROOT / "data" / "agent_work"

# 开头说法 -> agent 名。开头匹配不区分大小写。
DEFAULT_ROUTES = {
    "给codex": "codex",
    "给claude": "claude",
    "给deepseek": "deepseek",
    "给本机": "deepseek",
}

# 开头和口令后面允许出现的分隔符（中英文冒号/逗号都认）。
_SEPARATORS = frozenset(" \t\r\n:：,，")
_TAIL_CHARS = 1500


# ---------------------------------------------------------------------------
# 指令解析
# ---------------------------------------------------------------------------

def _skip_separators(text: str) -> tuple[str, bool]:
    index = 0
    while index < len(text) and text[index] in _SEPARATORS:
        index += 1
    return text[index:], index > 0


def parse_command(text: str, routes: dict, passphrase: str = "") -> tuple[str, str] | None:
    """解析“给xxx 任务”指令。

    - 开头说法不区分大小写。
    - 开头后面可以跟空格、中英文冒号或逗号，也可以直接接任务。
    - passphrase 非空时，必须写成“开头 + 分隔符 + 口令 + 分隔符 + 任务”。
    - 不是指令、任务为空、口令缺失或错误，一律返回 None（不抛异常、不泄露口令）。
    """
    if not isinstance(text, str) or not isinstance(routes, dict) or not routes:
        return None

    command = text.lstrip()
    if not command:
        return None

    # 选最长匹配的开头说法，避免“给codex”和“给code”同时存在时选错。
    matches: list[tuple[int, re.Match[str], object]] = []
    for key, agent in routes.items():
        if not isinstance(key, str) or not key:
            continue
        match = re.match(re.escape(key), command, re.IGNORECASE)
        if match is not None:
            matches.append((len(key), match, agent))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    _, match, agent = matches[0]

    rest = command[match.end():]
    phrase = str(passphrase or "").strip()
    if phrase:
        rest, had_separator = _skip_separators(rest)
        if not had_separator:
            return None
        if not rest.startswith(phrase):
            return None
        rest = rest[len(phrase):]
        # 口令后面必须还有分隔符，避免把“芝麻开门任务”当成口令“芝麻开门”+任务。
        if not rest or rest[0] not in _SEPARATORS:
            return None
        rest, _ = _skip_separators(rest)
        task = rest.strip()
        if not task:
            return None
        return agent, task  # type: ignore[return-value]

    rest, _ = _skip_separators(rest)
    task = rest.strip()
    if not task:
        return None
    return agent, task  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 可执行文件定位
# ---------------------------------------------------------------------------

def find_codex() -> str:
    """返回最新安装的 Codex 应用 codex.exe；找不到时才退回 PATH 里的 codex。

    Codex 应用每次更新会换 %LOCALAPPDATA%\\OpenAI\\Codex\\bin 下的哈希目录，
    所以不能缓存，AgentJobs 每次启动任务都会重新调用本函数。
    """
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        base = Path(local_appdata) / "OpenAI" / "Codex" / "bin"
        newest: Path | None = None
        newest_mtime = -1
        try:
            candidates = [
                path
                for path in base.glob("*/*.exe")
                if path.name.casefold() == "codex.exe"
            ]
        except OSError:
            candidates = []
        for path in candidates:
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if newest is None or mtime > newest_mtime:
                newest = path
                newest_mtime = mtime
        if newest is not None:
            return str(newest)

    return shutil.which("codex") or "codex"


def find_claude() -> str:
    """返回 Claude Code 可执行文件；找不到时返回 "claude" 让启动阶段报错。"""
    override = os.environ.get("CLAUDE_CLI")
    if override and Path(override).is_file():
        return override

    candidates = [
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / ".claude" / "local" / "claude.exe",
        Path.home() / ".claude" / "local" / "claude",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)

    found = shutil.which("claude")
    if found:
        return found

    appdata = os.environ.get("APPDATA")
    if appdata:
        cmd = Path(appdata) / "npm" / "claude.cmd"
        if cmd.is_file():
            return str(cmd)
    return "claude"


def _root_from_dsh_shim(path: Path) -> Path | None:
    """从 dsh.cmd 或 DeepSeek Harness.exe 反推安装根目录。"""
    name = path.name.casefold()
    if name == "dsh.cmd":
        try:
            return path.parents[4]
        except IndexError:
            return None
    if name == "deepseek harness.exe":
        return path.parent
    return None


def _dsh_roots_from_registry():
    try:
        import winreg
    except ImportError:
        return

    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    views = [0, getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)]
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in views:
            try:
                with winreg.OpenKey(hive, uninstall, 0, winreg.KEY_READ | view) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for index in range(count):
                        try:
                            sub_name = winreg.EnumKey(key, index)
                            with winreg.OpenKey(key, sub_name) as sub:
                                try:
                                    display = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                                except OSError:
                                    continue
                                if "DeepSeek Harness" not in display:
                                    continue
                                try:
                                    location = str(winreg.QueryValueEx(sub, "InstallLocation")[0])
                                except OSError:
                                    continue
                                location = location.strip().strip('"')
                                if location:
                                    yield Path(location)
                        except OSError:
                            continue
            except OSError:
                continue


def find_dsh_paths() -> tuple[str, str]:
    """返回 (DeepSeek Harness.exe, dsh-desktop-host cli.js) 的绝对路径。

    直接调用 Electron 可执行文件（等价于 dsh.cmd 做的事），避免经过 cmd.exe
    重新解析参数，任务文本里的 & | 引号等不会被 shell 吃掉。
    """
    roots: list[Path] = []
    override = os.environ.get("DSH_CLI")
    if override:
        root = _root_from_dsh_shim(Path(override))
        if root is not None:
            roots.append(root)

    found = shutil.which("dsh")
    if found:
        root = _root_from_dsh_shim(Path(found))
        if root is not None:
            roots.append(root)

    roots.extend(_dsh_roots_from_registry())

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        roots.append(Path(local_appdata) / "Programs" / "DeepSeek Harness")
    program_files = os.environ.get("PROGRAMFILES")
    if program_files:
        roots.append(Path(program_files) / "DeepSeek Harness")

    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key in seen:
            continue
        seen.add(key)

        root = Path(key)
        exe = root / "DeepSeek Harness.exe"
        if not exe.is_file():
            continue
        cli = (
            root
            / "resources"
            / "app.asar"
            / "dsh"
            / "node_modules"
            / "@deepseek-ai"
            / "dsh-desktop-host"
            / "lib"
            / "cli.js"
        )
        return str(exe), str(cli)

    raise RuntimeError(
        "找不到 DeepSeek Harness 安装目录；可以设置环境变量 DSH_CLI 指向 dsh.cmd"
    )


# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

def _default_workdir(agent: str) -> str:
    return str(AGENT_WORK_ROOT / agent)


def default_configs() -> dict:
    """四个默认 agent 配置的深拷贝。"""
    codex_cmd = [
        "{codex}",
        "exec",
        "--skip-git-repo-check",
        "-s",
        "workspace-write",
        "-m",
        "{model}",
        "-c",
        "model_reasoning_effort={reasoning}",
        "-C",
        "{workdir}",
        "{task}",
    ]
    return {
        "claude": {
            # --restricted: 文件工具限制在 cwd（以及 --add-dir）内；
            # --tools 只给只读/写文件工具，不给 Bash/PowerShell，所以不会执行删除命令。
            # --permission-mode acceptEdits: 非交互下自动接受文件编辑，避免卡在权限询问。
            # --tools 可以接多个值，不加 "--" 会把任务文本也当成工具名吞掉（实测报“没有输入”）。
            "cmd": [
                "{claude}",
                "-p",
                "--restricted",
                "--permission-mode",
                "acceptEdits",
                "--tools",
                "Read,Edit,Write,Glob,Grep",
                "--",
                "{task}",
            ],
            "workdir": _default_workdir("claude"),
            "timeout": 1800,
        },
        "codex": {
            # 用户实测可用的 Codex 应用自带 codex-cli 调用方式。
            # {codex} 每次 start() 重新 find_codex()，{model}/{reasoning} 来自本配置。
            "cmd": list(codex_cmd),
            "workdir": _default_workdir("codex"),
            "timeout": 1800,
            "model": "gpt-6.1-sol",
            "reasoning": "high",
        },
        "draw": {
            # --search 是 codex 顶层参数，必须放在 exec 前；exec 子命令本身不接受它。
            "cmd": ["{codex}", "--search", *codex_cmd[1:]],
            "workdir": _default_workdir("draw"),
            "timeout": 300,
            "model": "gpt-6.1-sol",
            "reasoning": "low",
        },
        "deepseek": {
            # 直接执行 dsh.cmd 内部等价的那条 Electron 命令：headless 单次任务。
            # cwd 就是 DSH 的 workspaceRoot，再显式锁 workspace-write。
            "cmd": [
                "{dsh_exe}",
                "--expose-internals",
                "{dsh_cli}",
                "headless",
                "{task}",
            ],
            "workdir": _default_workdir("deepseek"),
            "timeout": 1800,
            "env": {
                "ELECTRON_RUN_AS_NODE": "1",
                "DSH_PERMISSION_MODE": "workspace-write",
            },
        },
    }


DEFAULT_CONFIGS = default_configs()


# ---------------------------------------------------------------------------
# 图片任务
# ---------------------------------------------------------------------------

_DRAW_DESCRIPTION_CHARS = 800
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_REFERENCE_HINT = re.compile(
    r"网上|网络|联网|搜索|搜图|找图|参考图|参考图片|参考.{0,4}(?:形象|照片|图片|造型)|"
    r"二创|同人|角色|官方形象|原作|原型|立绘|剧照|写真|照片|相片|截图"
)


def draw_task(description: str) -> str:
    """把主人给的画面描述整理成只允许调用图片工具的单图任务。"""
    clean = " ".join(str(description or "").split())
    clean = clean[:_DRAW_DESCRIPTION_CHARS].rstrip()
    if not clean:
        clean = "一张简洁自然的图片"
    task = (
        f"用图片生成工具画：{clean}\n"
        "只生成一张图片，并保存为当前目录下的 out.png。\n"
        "不许用代码、Pillow、SVG、Canvas、截图、下载现成图片或其他方式凑一张。\n"
    )
    if _REFERENCE_HINT.search(clean):
        task += (
            "先用实时联网搜索查找描述中的人物、角色或形象，选择 1～3 张合适的参考图保存到当前目录的 refs/，"
            "再把这些本地参考图作为输入交给图片生成工具。\n"
            "refs/ 里的下载图片只能用作参考，绝不允许直接复制、改名或当成 out.png 交差；"
            "out.png 必须是图片生成工具新生成的图片。\n"
            "如果找不到可用参考图，就直接照文字描述生成，并在最终输出中另加一行：NO_REFERENCE\n"
        )
    task += "如果没有图片生成工具，只输出一行：NO_IMAGE_TOOL"
    return task


def _recent_image(path: Path, earliest: float) -> tuple[float, Path] | None:
    try:
        if path.is_file() and path.suffix.casefold() in _IMAGE_SUFFIXES:
            mtime = path.stat().st_mtime
            if mtime >= earliest:
                return mtime, path
    except OSError:
        pass
    return None


def find_image(job, workdir, generated_root=Path.home() / ".codex" / "generated_images") -> Path | None:
    """找到本次画图结果，复制进任务工作目录的 images 文件夹。"""
    folder = Path(workdir)
    out = folder / "out.png"
    try:
        refs = (folder / "refs").resolve()
    except OSError:
        refs = folder / "refs"
    earliest = float(job.started) - 2
    found = _recent_image(out, earliest)
    if found is not None and refs.is_dir():
        # A downloaded reference handed in as the result is not a drawing: look for the real one.
        try:
            data = out.read_bytes()
            if any(ref.is_file() and ref.stat().st_size == len(data) and ref.read_bytes() == data
                   for ref in refs.rglob("*")):
                found = None
        except OSError:
            found = None

    if found is None:
        candidates = []
        try:
            for path in Path(generated_root).rglob("*"):
                try:
                    if path.resolve().is_relative_to(refs):
                        continue
                except OSError:
                    continue
                candidate = _recent_image(path, earliest)
                if candidate is not None:
                    candidates.append(candidate)
        except OSError:
            pass
        if candidates:
            found = max(candidates, key=lambda item: (item[0], str(item[1])))

    if found is None:
        try:
            out.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("无法清理旧画图结果：%s", out, exc_info=True)
        return None

    source = found[1]
    destination_dir = folder / "images"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{job.id}{source.suffix.casefold()}"
    shutil.copy2(source, destination)

    try:
        out.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("无法清理画图临时文件：%s", out, exc_info=True)
    return destination


# ---------------------------------------------------------------------------
# 任务管理
# ---------------------------------------------------------------------------

class AgentJob:
    """一次后台 agent 任务。运行中 status 为 "running"，结束后为
    "done" / "failed" / "timeout" / "stopped"。
    """

    def __init__(self, job_id: str, agent: str, task: str, log_path: Path):
        self.id = job_id
        self.agent = agent
        self.task = task
        self.status = "running"
        self.returncode: int | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.output = ""
        self.tail = ""
        self.log_path = log_path
        self._proc: subprocess.Popen | None = None
        self._stop_event = threading.Event()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent,
            "task": self.task,
            "status": self.status,
            "returncode": self.returncode,
            "started": self.started,
            "finished": self.finished,
            "output": self.output,
            "tail": self.tail,
            "log_path": str(self.log_path),
        }

    def __repr__(self) -> str:
        return f"<AgentJob {self.id} agent={self.agent!r} status={self.status!r}>"


def _safe_component(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned[:60] or "agent"


class AgentJobs:
    """管理后台 agent 子进程。

    - 同一个 agent 同时最多 1 个任务。
    - 每个任务一个后台线程；start() 立即返回 job_id。
    - 结束（正常、失败、超时、手动停止）后调用 on_done(job)；
      on_done 抛出的异常会被捕获并记录日志，不会让后台线程崩掉。
    - jobs() 返回所有任务，新的在前。
    """

    def __init__(self, configs: dict, log_dir: Path, on_done):
        if not isinstance(configs, dict):
            raise TypeError("configs 必须是 dict")
        if not callable(on_done):
            raise TypeError("on_done 必须可调用")
        self.configs = configs
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.on_done = on_done
        self._jobs: list[AgentJob] = []
        self._lock = threading.RLock()

    # -- 对外 API ----------------------------------------------------------

    def start(self, agent: str, task: str) -> str:
        """后台启动一个任务，立即返回 job_id。"""
        if not isinstance(task, str) or not task.strip():
            raise ValueError("任务不能为空")
        with self._lock:
            config = self.configs.get(agent)
            if config is None:
                raise RuntimeError(f"未知的 agent：{agent}")
            for job in self._jobs:
                if job.agent == agent and job.status == "running":
                    raise RuntimeError(
                        f"agent「{agent}」已有任务在运行，请等待完成或先停止"
                    )

            job_id = (
                f"{_safe_component(agent)}-{time.strftime('%Y%m%d-%H%M%S')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            snapshot = copy.deepcopy(config)
            job = AgentJob(job_id, agent, task, self.log_dir / f"{job_id}.log")
            self._jobs.append(job)

        thread = threading.Thread(
            target=self._run,
            args=(job, snapshot),
            name=f"agent-job-{job_id}",
            daemon=True,
        )
        thread.start()
        return job_id

    def jobs(self) -> list[AgentJob]:
        """所有任务，新的在前。"""
        with self._lock:
            return list(reversed(self._jobs))

    def stop(self, job_id: str) -> bool:
        """请求停止任务并结束整个进程树。

        返回 True 表示已发出停止请求；任务已经结束时返回 False。
        状态由后台线程在收尾时记为 "stopped"，随后调用 on_done。
        """
        with self._lock:
            job = self._find_job(job_id)
            if job.status != "running":
                return False
            job._stop_event.set()
            proc = job._proc

        if proc is not None and proc.poll() is None:
            self._kill_tree(proc)
        return True

    # -- 内部 --------------------------------------------------------------

    def _find_job(self, job_id: str) -> AgentJob:
        for job in self._jobs:
            if job.id == job_id:
                return job
        raise RuntimeError(f"未知的任务：{job_id}")

    def _run(self, job: AgentJob, config: dict) -> None:
        if job._stop_event.is_set():
            output = "任务在启动前被停止。\n"
            self._write_log(job.log_path, output)
            self._finish(job, "stopped", None, output)
            return

        try:
            workdir = self._prepare_workdir(job.agent, config)
            argv = self._build_argv(config, job.task, workdir)
            env = self._build_env(config)
            timeout = self._timeout(config)
        except Exception as exc:
            output = f"无法启动任务：{exc}\n"
            self._write_log(job.log_path, output)
            self._finish(job, "failed", None, output)
            return

        if job._stop_event.is_set():
            output = "任务在启动前被停止。\n"
            self._write_log(job.log_path, output)
            self._finish(job, "stopped", None, output)
            return

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(workdir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=creationflags,
            )
        except Exception as exc:
            output = f"无法启动任务：{exc}\n"
            self._write_log(job.log_path, output)
            self._finish(job, "failed", None, output)
            return

        with self._lock:
            job._proc = proc

        chunks: list[str] = []
        reader = threading.Thread(
            target=self._pump,
            args=(proc.stdout, job.log_path, chunks),
            name=f"agent-log-{job.id}",
            daemon=True,
        )
        reader.start()

        returncode: int | None = None
        timed_out = False
        stopped = False
        failed_reason: Exception | None = None
        try:
            if job._stop_event.is_set():
                stopped = True
                self._kill_tree(proc)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    if job._stop_event.is_set():
                        stopped = True
                        self._kill_tree(proc)
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        self._kill_tree(proc)
                        break
                    try:
                        returncode = proc.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            if returncode is None:
                try:
                    returncode = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    try:
                        returncode = proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        returncode = -1
        except Exception as exc:
            # 后台线程不能因为意外异常悄悄死掉：记下来，走统一的收尾和 on_done。
            failed_reason = exc
            chunks.append(f"\n[任务执行异常：{exc}]\n")
            if returncode is None:
                returncode = -1
        finally:
            if proc.poll() is None:
                self._kill_tree(proc)
            reader.join(timeout=10)
            if reader.is_alive():
                chunks.append("\n[输出读取线程未能结束]\n")

        if job._stop_event.is_set() and not timed_out:
            stopped = True
        output = "".join(chunks)
        if stopped:
            status = "stopped"
        elif timed_out:
            status = "timeout"
        elif failed_reason is not None:
            status = "failed"
        elif returncode == 0:
            status = "done"
        else:
            status = "failed"
        self._finish(job, status, returncode, output)

    def _finish(
        self,
        job: AgentJob,
        status: str,
        returncode: int | None,
        output: str,
    ) -> None:
        with self._lock:
            job.status = status
            job.returncode = returncode
            job.finished = time.time()
            job.output = output
            job.tail = output[-_TAIL_CHARS:]
        try:
            self.on_done(job)
        except Exception:
            logger.exception("on_done 回调异常（job=%s）", job.id)

    def _prepare_workdir(self, agent: str, config: dict) -> Path:
        raw = config.get("workdir")
        if raw:
            path = Path(os.path.expandvars(str(raw))).expanduser()
            if not path.is_absolute():
                path = ROOT / path
        else:
            path = AGENT_WORK_ROOT / _safe_component(agent)
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def _timeout(self, config: dict) -> float:
        raw = config.get("timeout", 1800)
        try:
            timeout = float(raw)
        except (TypeError, ValueError):
            raise RuntimeError(f"timeout 不是数字：{raw!r}") from None
        if timeout < 0:
            raise RuntimeError(f"timeout 不能为负数：{raw!r}")
        return timeout

    def _build_env(self, config: dict) -> dict[str, str]:
        env = os.environ.copy()
        extra = config.get("env") or {}
        if not isinstance(extra, dict):
            raise RuntimeError("env 必须是 dict")
        for key, value in extra.items():
            env[str(key)] = str(value)
        return env

    def _build_argv(self, config: dict, task: str, workdir: Path) -> list[str]:
        template = config.get("cmd")
        if not isinstance(template, (list, tuple)) or not template:
            raise RuntimeError("agent 配置缺少 cmd 参数列表")
        template = [str(arg) for arg in template]

        values: dict[str, str] = {"workdir": str(workdir)}
        for key, value in config.items():
            if key in ("cmd", "workdir", "timeout", "env"):
                continue
            if isinstance(value, (str, int, float, bool)):
                values[key] = str(value)

        joined = "\n".join(template)
        if "{codex}" in joined:
            values["codex"] = find_codex()
        if "{claude}" in joined:
            values["claude"] = find_claude()
        if "{dsh_exe}" in joined or "{dsh_cli}" in joined:
            exe, cli = find_dsh_paths()
            values["dsh_exe"] = exe
            values["dsh_cli"] = cli

        argv: list[str] = []
        for arg in template:
            # 先检查模板里有没有未知占位符（此时任务文本还没插入，避免误判）。
            probe = arg
            for key in values:
                probe = probe.replace("{" + key + "}", "")
            probe = probe.replace("{task}", "")
            unknown = re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", probe)
            if unknown is not None:
                raise RuntimeError(f"配置中有无法解析的占位符：{unknown.group(0)}")

            for key, value in values.items():
                arg = arg.replace("{" + key + "}", value)
            # 任务文本最后替换，只替换一次，任务里出现 {task}/{workdir} 也原样保留。
            arg = arg.replace("{task}", task)
            argv.append(arg)
        return argv

    def _pump(self, pipe, log_path: Path, chunks: list[str]) -> None:
        """读取子进程输出，UTF-8 解码（errors=replace），边读边写日志。"""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            with open(log_path, "w", encoding="utf-8", newline="") as log:
                while True:
                    chunk = pipe.read(4096) if pipe is not None else b""
                    if not chunk:
                        break
                    text = decoder.decode(chunk)
                    if text:
                        chunks.append(text)
                        log.write(text)
                        log.flush()
                final = decoder.decode(b"", final=True)
                if final:
                    chunks.append(final)
                    log.write(final)
                    log.flush()
        except Exception as exc:
            chunks.append(f"\n[读取输出失败：{exc}]\n")
        finally:
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    def _write_log(self, log_path: Path, text: str) -> None:
        try:
            log_path.write_text(
                text, encoding="utf-8", errors="replace", newline=""
            )
        except OSError:
            pass

    def _kill_tree(self, proc: subprocess.Popen) -> None:
        """结束整个进程树。Windows 用 taskkill /T /F。"""
        if proc.poll() is not None:
            return

        if os.name == "nt":
            system_root = os.environ.get("SystemRoot", r"C:\Windows")
            taskkill = Path(system_root) / "System32" / "taskkill.exe"
            command = [str(taskkill) if taskkill.is_file() else "taskkill"]
            command += ["/T", "/F", "/PID", str(proc.pid)]
            try:
                subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    proc.kill()
                except OSError:
                    pass

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
