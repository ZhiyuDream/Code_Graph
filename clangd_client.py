"""
最小 LSP 客户端：通过 stdio 启动 clangd，发送 JSON-RPC 请求并读取响应。
用于 documentSymbol、prepareCallHierarchy、callHierarchy/outgoingCalls。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

# LSP 消息以 Content-Length 头 + \\r\\n\\r\\n + body 组成
_ENCODING = "utf-8"


def _read_content_length(line: str) -> int | None:
    if line.strip().lower().startswith("content-length:"):
        return int(line.split(":", 1)[1].strip())
    return None


def _write_message(proc: subprocess.Popen, body: str) -> None:
    raw = body.encode(_ENCODING)
    header = f"Content-Length: {len(raw)}\r\n\r\n"
    proc.stdin.write(header.encode(_ENCODING))
    proc.stdin.write(raw)
    proc.stdin.flush()


def notify(proc: subprocess.Popen, method: str, params: dict[str, Any]) -> None:
    """发送 LSP 通知（无 id，不等待响应）。"""
    _write_message(proc, json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))


def _read_message(proc: subprocess.Popen) -> str | None:
    # 读头：一行 Content-Length: N（stdout 为二进制，需解码为 str）
    first = proc.stdout.readline()
    if not first:
        return None
    first_str = first.decode(_ENCODING).strip()
    length = _read_content_length(first_str)
    if length is None:
        while proc.stdout.readline().strip():
            pass
        return None
    # 读空行
    while True:
        line = proc.stdout.readline()
        if line == b"\r\n" or line == b"\n":
            break
        if not line:
            return None
    body = proc.stdout.read(length)
    return body.decode(_ENCODING)


_request_id = 0


def _next_id() -> int:
    global _request_id
    _request_id += 1
    return _request_id


def request(proc: subprocess.Popen, method: str, params: dict[str, Any]) -> Any:
    """发送 LSP 请求，等待并返回 result；若 error 则抛异常。"""
    req = {"jsonrpc": "2.0", "id": _next_id(), "method": method, "params": params}
    _write_message(proc, json.dumps(req))
    while True:
        msg = _read_message(proc)
        if not msg:
            raise RuntimeError("clangd 未返回响应")
        data = json.loads(msg)
        if "id" in data and data["id"] == req["id"]:
            if "error" in data:
                raise RuntimeError(f"LSP error: {data['error']}")
            return data.get("result")
        # 可能是 notification（如 window/logMessage），忽略并继续读
        if "method" in data:
            continue


def _clangd_executable() -> str:
    """优先使用 CODEGRAPH_CLANGD_CMD，否则 clangd-20（支持 CALLS），否则 clangd。"""
    exe = os.environ.get("CODEGRAPH_CLANGD_CMD", "").strip()
    if exe and shutil.which(exe):
        return exe
    if shutil.which("clangd-20"):
        return "clangd-20"
    return "clangd"


def start_clangd(repo_root: Path, compile_commands_dir: Path) -> subprocess.Popen:
    """
    在 repo_root 下启动 clangd，使用 compile_commands_dir 作为 -compile-commands-dir。
    可执行文件优先：CODEGRAPH_CLANGD_CMD → clangd-20 → clangd。
    返回子进程，调用方负责 proc.terminate() 或 proc.kill()。
    """
    build_dir = Path(compile_commands_dir).resolve()
    clangd_bin = _clangd_executable()
    cmd = [
        clangd_bin,
        f"-compile-commands-dir={build_dir}",
        "-background-index",
        "-log=error",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=repo_root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**os.environ},
    )
    return proc


def initialize(proc: subprocess.Popen, repo_root: Path) -> None:
    """发送 initialize 与 initialized，使 clangd 进入可用状态。"""
    root_uri = Path(repo_root).as_uri()
    request(
        proc,
        "initialize",
        {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": {},
            "workspaceFolders": [{"uri": root_uri, "name": "repo"}],
        },
    )
    _write_message(proc, json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}))
    # 调用方可在 initialize 后 sleep 一段时间，等待 clangd 背景索引
