"""Local Stable Diffusion WebUI Forge drawing (the A1111 API), possibly shared with SillyTavern or another front end.

Each picture names its own checkpoint and clip skip through override_settings, restored afterwards,
so Forge's global settings never change. Forge queues requests itself; a stopped drawing is only
discarded here, never interrupted, because /sdapi/v1/interrupt would stop another front end's picture too.
"""
from __future__ import annotations

import base64
import io
import json
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:7860"
DEFAULT_MODEL = "waiIllustriousSDXL_v170.safetensors [f116b0c78f]"
LEGACY_MODEL = "codex-reference"
LEGACY_LABEL = "Codex · 原联网参考图模式"
# Common SillyTavern settings for this checkpoint (Illustrious SDXL).
DEFAULT_PARAMS = {
    "steps": 28, "cfg_scale": 5.5, "width": 832, "height": 1216, "sampler_name": "DPM++ 2M",
    "scheduler": "automatic", "clip_skip": 2,
    "prefix": "masterpiece, best quality, amazing quality, very aesthetic, highres, absurdres, detailed, newest,",
    # A common negative, plus nsfw/nude: this is a group-chat bot.
    "negative": ("nsfw, nude, worst quality, low quality, lowres, bad anatomy, bad hands, extra digits, fewer digits, "
                 "missing fingers, extra fingers, fused fingers, malformed hands, deformed, bad proportions, "
                 "poorly drawn face, poorly drawn hands, text, watermark, signature, username, jpeg artifacts"),
}


class DrawingStopped(Exception):
    """The caller cancelled this drawing; its output must not be delivered."""


def server_url(value):
    value = str(value).strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment \
            or parsed.username or parsed.password:
        raise ValueError("Forge 地址请填写 http://127.0.0.1:7860 这样的 HTTP 地址")
    return value


def payload(prompt, model, params=None, negative=""):
    p = {**DEFAULT_PARAMS, **(params or {})}
    prompt = " ".join(str(prompt).split())
    if not prompt:
        raise ValueError("画面描述不能为空")
    return {
        "prompt": f"{p['prefix']} {prompt}".strip(),
        "negative_prompt": ", ".join(s.strip(" ,") for s in (p["negative"], negative) if s and s.strip(" ,")),
        "steps": int(p["steps"]), "cfg_scale": float(p["cfg_scale"]),
        "width": int(p["width"]), "height": int(p["height"]),
        "sampler_name": p["sampler_name"], "scheduler": p["scheduler"],
        "seed": -1, "batch_size": 1, "n_iter": 1,
        "override_settings": {"sd_model_checkpoint": model, "CLIP_stop_at_last_layers": int(p["clip_skip"])},
        "override_settings_restore_afterwards": True,
        "send_images": True, "save_images": False,
    }


class Forge:
    def __init__(self, url=DEFAULT_URL):
        self.url = server_url(url)

    def request(self, route, data=None, *, timeout=10):
        body = None if data is None else json.dumps(data).encode("utf-8")
        req = Request(self.url + route, data=body,
                      headers={"Content-Type": "application/json"} if body is not None else {},
                      method="POST" if body is not None else "GET")
        try:
            with urlopen(req, timeout=timeout) as response:
                raw = response.read(80 * 1024 * 1024 + 1)
            if len(raw) > 80 * 1024 * 1024:
                raise RuntimeError("Forge 返回的内容过大")
            return json.loads(raw or b"{}")
        except HTTPError as exc:
            detail = exc.read(8192).decode("utf-8", "replace")
            raise RuntimeError(f"Forge HTTP {exc.code}：{detail[:600]}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"连接 Forge 失败（{self.url}）：{exc}") from exc

    def models(self):
        return [{"id": m["title"], "label": m.get("model_name") or m["title"]}
                for m in self.request("/sdapi/v1/sd-models") if m.get("title")]

    def idle(self):
        state = self.request("/sdapi/v1/progress?skip_current_image=true").get("state") or {}
        if state.get("job_count") or state.get("job"):
            raise RuntimeError("Forge 正在画图（可能来自其他前端），请等它画完再操作模型")

    def load(self):
        self.idle()
        self.request("/sdapi/v1/reload-checkpoint", {}, timeout=300)
        return "Forge 模型已加载"

    def unload(self):
        self.idle()
        self.request("/sdapi/v1/unload-checkpoint", {}, timeout=60)
        return "已卸载 Forge 的生图模型并释放显存；服务保持运行，下次画图会自动重新加载"

    def generate(self, prompt, model, output, *, params=None, negative="", timeout=600, cancel=lambda: False,
                 log=lambda text: None, poll_seconds=0.5):
        if cancel():
            raise DrawingStopped("画图已停止")
        if model not in {m["id"] for m in self.models()}:
            raise ValueError(f"Forge 里没有这个模型：{model}，请在画图页刷新模型")
        if cancel():  # never submit a picture nobody wants any more
            raise DrawingStopped("画图已停止")
        body = payload(prompt, model, params, negative)
        log(f"Forge model={model} {body['width']}x{body['height']} steps={body['steps']}")
        result = {}

        def call():  # synchronous in Forge; waited on here so a stop or timeout can give up on it
            try:
                result["data"] = self.request("/sdapi/v1/txt2img", body, timeout=max(60, timeout or 600))
            except Exception as exc:
                result["error"] = exc

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        started = time.monotonic()
        while worker.is_alive():
            if cancel():
                raise DrawingStopped("画图已停止（Forge 里这张会画完，但不会发出去）")
            if timeout and time.monotonic() - started >= timeout:
                raise TimeoutError("Forge 画图超时")
            worker.join(poll_seconds)
        if "error" in result:
            raise result["error"]
        if cancel():
            raise DrawingStopped("画图已停止")
        images = (result.get("data") or {}).get("images") or []
        if not images:
            raise RuntimeError("Forge 没有返回图片")
        image = base64.b64decode(images[0].split(",", 1)[-1])
        from PIL import Image
        with Image.open(io.BytesIO(image)) as decoded:
            decoded.verify()
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_suffix(".tmp")
        try:
            temp.write_bytes(image)
            temp.replace(output)
        finally:
            temp.unlink(missing_ok=True)
        log(f"图片已保存：{output}")
        return output


def start_server(url, config, log_dir, *, cancel=lambda: False, timeout=300):
    """Start the local Forge (run.bat) when it is not running; no browser, no other services touched."""
    client = Forge(url)
    try:
        client.request("/sdapi/v1/sd-models", timeout=3)
        return "Forge 已在运行：" + client.url
    except RuntimeError:
        pass
    if urlsplit(client.url).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("只能从控制台启动本机 Forge；远程服务请在对应电脑上启动")
    root = Path(config.get("forge_root") or "")
    if not (root / "run.bat").is_file():
        raise ValueError("找不到 Forge 的 run.bat，请检查设置里的 forge_root")
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "forge-server.log").open("ab") as log:
        process = subprocess.Popen(["cmd", "/c", "run.bat"], cwd=str(root), stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if cancel():
            raise DrawingStopped("停止等待启动；已启动的 Forge 会继续运行")
        if process.poll() is not None:
            raise RuntimeError("Forge 启动失败，请查看 " + str(log_dir / "forge-server.log"))
        try:
            client.request("/sdapi/v1/sd-models", timeout=3)
            return "Forge 启动完成：" + client.url
        except RuntimeError:
            time.sleep(2)
    raise TimeoutError("Forge 启动等待超时，请查看 " + str(log_dir / "forge-server.log"))
