"""
clangd 索引就绪检测：替代固定的 time.sleep(3.0)。

策略：
1. 初始等待 2s
2. 然后每 2s 尝试请求一个文件的 documentSymbol
3. 若成功返回非空结果，认为索引就绪
4. 最多等待 120s
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


def wait_for_index(
    lsp_request: Callable[[str, dict[str, Any]], Any],
    sample_file_uri: str,
    initial_delay: float = 2.0,
    poll_interval: float = 2.0,
    max_wait: float = 120.0,
    lsp_notify: Callable[[str, dict[str, Any]], None] | None = None,
    file_content: str = "",
) -> bool:
    """
    等待 clangd 背景索引就绪。

    Args:
        lsp_request: LSP request 函数，签名为 request(method, params) -> result
        sample_file_uri: 用于检测的文件 URI（选一个仓库中必定存在的源文件）
        initial_delay: 初始等待时间
        poll_interval: 轮询间隔
        max_wait: 最大等待时间
        lsp_notify: LSP notify 函数（用于发送 textDocument/didOpen）
        file_content: 文件内容（用于 didOpen）

    Returns:
        True: 索引就绪
        False: 超时，但仍可继续（可能部分文件未索引完）
    """
    time.sleep(initial_delay)
    elapsed = initial_delay

    # 先发送 didOpen 让 clangd 知道文件存在
    if lsp_notify:
        try:
            lsp_notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": sample_file_uri,
                        "languageId": "cpp",
                        "version": 1,
                        "text": file_content,
                    }
                },
            )
        except Exception as e:
            logger.warning("didOpen failed: %s", e)

    while elapsed < max_wait:
        try:
            result = lsp_request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": sample_file_uri}},
            )
            if result and isinstance(result, list) and len(result) > 0:
                logger.info("clangd index ready after %.1fs", elapsed)
                return True
        except Exception as e:
            logger.warning("Index poll failed: %s", e)

        time.sleep(poll_interval)
        elapsed += poll_interval

    logger.warning("clangd index wait timed out after %.1fs", max_wait)
    return False
