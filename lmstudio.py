"""LM Studio model listing and switching. Only touches the bot's own model instance."""
from __future__ import annotations

import json
import re
import subprocess

NO_WINDOW = 0x08000000


def release_memory():
    """Hand llama-server's touched model pages back to Windows.

    With the weights fully on the GPU, the RAM copy left over from loading (and
    caches from describing many images) is only file-backed pages; trimming them is
    harmless and frees ~20 GB. Returns how many server processes were trimmed.
    """
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq llama-server.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, creationflags=NO_WINDOW).stdout
    trimmed = 0
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            handle = kernel32.OpenProcess(0x0400 | 0x0100, False, int(parts[1]))  # QUERY_INFO | SET_QUOTA
            if handle:
                trimmed += bool(psapi.EmptyWorkingSet(handle))
                kernel32.CloseHandle(handle)
    return trimmed


def strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


class LMStudio:
    def __init__(self, lms_path: str):
        self.lms = lms_path

    def run(self, args, timeout):
        task = subprocess.run([self.lms, *args], capture_output=True, timeout=timeout,
                              creationflags=NO_WINDOW)
        out = strip_ansi((task.stdout or b"").decode("utf-8", "replace")
                         + (task.stderr or b"").decode("utf-8", "replace"))
        if task.returncode:
            lines = [l for l in out.strip().splitlines() if l.strip()]
            raise RuntimeError(lines[-1] if lines else f"lms {args[0]} 失败")
        return out

    def json(self, args):
        out = self.run(args, timeout=30)
        return json.loads(out[out.index("["):])

    def models(self):
        """Chat models on disk, as [{key, label, size_gb}]."""
        result = []
        for row in self.json(["ls", "--json"]):
            if row.get("type") != "llm":
                continue
            quant = (row.get("quantization") or {}).get("name", "")
            size = row.get("sizeBytes", 0) / 1e9
            result.append({"key": row["modelKey"], "size_gb": round(size, 1),
                           "label": f'{row["modelKey"]}  ·  {row.get("paramsString", "")} {quant}  ·  {size:.1f} GB'})
        return result

    def instances(self):
        """{identifier: model key} for everything currently loaded."""
        return {row["identifier"]: row["modelKey"] for row in self.json(["ps", "--json"])}

    def estimate(self, key, context):
        out = self.run(["load", key, "--gpu", "max", "-c", str(context), "--parallel", "1",
                        "--estimate-only", "--yes"], timeout=60)
        return "；".join(l.strip() for l in out.splitlines() if "GPU Memory" in l or "Estimate:" in l)

    def ensure(self, key, identifier, context, force=False):
        """Make `identifier` serve `key`, fully on the GPU.

        Partial CPU offload makes a 27B model crawl (~7 tok/s) and freezes the PC,
        so we always ask for full offload; LM Studio refuses a model that cannot fit.
        Other loaded models are left alone.
        """
        loaded = self.instances()
        if loaded.get(identifier) == key and not force:
            return False
        if identifier in loaded:
            self.run(["unload", identifier], timeout=60)
        self.run(["load", key, "--gpu", "max", "-c", str(context), "--parallel", "1",
                  "--identifier", identifier, "--yes"], timeout=600)
        if self.instances().get(identifier) != key:
            raise RuntimeError("模型加载后未在 LM Studio 中出现")
        release_memory()
        return True
