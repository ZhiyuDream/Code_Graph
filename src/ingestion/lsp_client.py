"""
LSP 客户端：启动 clangd，维护 JSON-RPC 连接。

改进点（相比原 clangd_client.py）：
1. request() 增加超时，超时抛出 LSPTimeoutError
2. stderr 读取线程，输出 clangd 日志便于诊断
3. 上下文管理器，确保进程终止
4. 使用 use_process_alive_check 模式避免短超时掐断 clangd 正常工作
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class LSPTimeoutError(TimeoutError):
    """LSP 请求超时。"""
    pass


class LSPClient:
    """clangd LSP 客户端。"""

    _ENCODING = "utf-8"
    _request_counter = 0

    def __init__(self, proc: subprocess.Popen, repo_root: Path):
        self.proc = proc
        self.repo_root = repo_root
        self._stderr_thread: threading.Thread | None = None
        self._running = True

    @classmethod
    def start(
        cls,
        repo_root: Path,
        compile_commands_dir: Path,
        clangd_cmd: str | None = None,
    ) -> "LSPClient":
        """启动 clangd 并初始化 LSP 连接。"""
        exe = cls._find_clangd(clangd_cmd)
        build_dir = Path(compile_commands_dir).resolve()
        cmd = [
            exe,
            f"-compile-commands-dir={build_dir}",
            "-log=error",
            "--background-index=false",
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=repo_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ},
        )
        client = cls(proc, Path(repo_root).resolve())
        client._start_stderr_reader()
        client._initialize()
        return client

    @staticmethod
    def _find_clangd(clangd_cmd: str | None = None) -> str:
        if clangd_cmd and shutil.which(clangd_cmd):
            return clangd_cmd
        for candidate in ["clangd-20", "clangd"]:
            if shutil.which(candidate):
                return candidate
        raise RuntimeError("clangd 未找到，请安装 clangd 20+")

    def _start_stderr_reader(self):
        """启动 stderr 读取线程。"""
        def reader():
            while self._running and self.proc.stderr:
                try:
                    line = self.proc.stderr.readline()
                    if not line:
                        break
                    decoded = line.decode(self._ENCODING, errors="replace").rstrip()
                    if decoded:
                        logger.debug("[clangd stderr] %s", decoded)
                except Exception:
                    break

        self._stderr_thread = threading.Thread(target=reader, daemon=True)
        self._stderr_thread.start()

    def _initialize(self):
        """发送 initialize / initialized。"""
        root_uri = self.repo_root.as_uri()
        self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "capabilities": {},
                "workspaceFolders": [{"uri": root_uri, "name": "repo"}],
            },
            timeout=10.0,
        )
        self._write_message(
            json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        )

    def _write_message(self, body: str) -> None:
        raw = body.encode(self._ENCODING)
        header = f"Content-Length: {len(raw)}\r\n\r\n"
        self.proc.stdin.write(header.encode(self._ENCODING))
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()

    def _read_message(self, read_timeout: float = 1.0) -> str | None:
        """读取一条 LSP 消息，支持超时避免 readline 永久阻塞。"""
        import select

        # 等待 stdout 有数据，超时返回 None
        ready, _, _ = select.select([self.proc.stdout], [], [], read_timeout)
        if not ready:
            return None

        first = self.proc.stdout.readline()
        if not first:
            return None
        length = None
        line_str = first.decode(self._ENCODING).strip()
        if line_str.lower().startswith("content-length:"):
            length = int(line_str.split(":", 1)[1].strip())
        if length is None:
            while self.proc.stdout.readline().strip():
                pass
            return None
        while True:
            blank = self.proc.stdout.readline()
            if blank == b"\r\n" or blank == b"\n" or not blank:
                break
        body = self.proc.stdout.read(length)
        return body.decode(self._ENCODING)

    def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = None,
        use_process_alive_check: bool = False,
    ) -> Any:
        """发送 LSP 请求，等待响应。

        Args:
            timeout: 绝对安全网超时（秒）。None 表示无限等。
                     仅在 use_process_alive_check=False 时作为硬 deadline。
            use_process_alive_check: True 时不设固定 deadline，只要 clangd
                     进程还在运行就一直等；进程退出才立即失败。
        """
        LSPClient._request_counter += 1
        req_id = LSPClient._request_counter
        req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        self._write_message(json.dumps(req))

        if use_process_alive_check:
            # 进程存活检测模式：clangd 没死就一直等
            while True:
                # 如果进程已退出，说明 crash 了，立刻失败
                if self.proc.poll() is not None:
                    raise LSPTimeoutError(
                        f"LSP request '{method}' aborted: clangd process exited (code={self.proc.returncode})"
                    )
                msg = self._read_message(read_timeout=1.0)
                if msg is None:
                    time.sleep(0.05)
                    continue
                data = json.loads(msg)
                if data.get("id") == req_id:
                    if "error" in data:
                        raise RuntimeError(f"LSP error: {data['error']}")
                    return data.get("result")
                if "method" in data:
                    continue
        else:
            # 传统 deadline 模式（保留给 initialize 等快速请求）
            deadline = time.time() + (timeout if timeout is not None else 600.0)
            while time.time() < deadline:
                msg = self._read_message()
                if msg is None:
                    time.sleep(0.05)
                    continue
                data = json.loads(msg)
                if data.get("id") == req_id:
                    if "error" in data:
                        raise RuntimeError(f"LSP error: {data['error']}")
                    return data.get("result")
                if "method" in data:
                    continue
                time.sleep(0.01)
            raise LSPTimeoutError(f"LSP request '{method}' timed out after {timeout}s")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        """发送 LSP 通知。"""
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        self._write_message(body)

    def close(self) -> None:
        """关闭连接并终止 clangd 进程。"""
        self._running = False
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
