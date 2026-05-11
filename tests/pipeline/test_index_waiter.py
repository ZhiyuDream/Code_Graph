"""Tests for index_waiter.py"""
from __future__ import annotations

import pytest

from src.pipeline.index_waiter import wait_for_index


class TestWaitForIndex:
    def test_ready_on_first_poll(self):
        """首次轮询即成功。"""
        def mock_request(method, params):
            if method == "textDocument/documentSymbol":
                return [{"name": "main", "kind": 12}]
            return None

        result = wait_for_index(mock_request, "file:///repo/main.cpp", initial_delay=0.01, poll_interval=0.01, max_wait=0.1)
        assert result is True

    def test_ready_after_retries(self):
        """第三次轮询才成功。"""
        call_count = 0
        def mock_request(method, params):
            nonlocal call_count
            call_count += 1
            if method == "textDocument/documentSymbol":
                if call_count >= 3:
                    return [{"name": "main", "kind": 12}]
                return []
            return None

        result = wait_for_index(mock_request, "file:///repo/main.cpp", initial_delay=0.01, poll_interval=0.01, max_wait=0.1)
        assert result is True
        assert call_count >= 3

    def test_timeout(self):
        """始终未就绪，超时返回 False。"""
        def mock_request(method, params):
            return []

        result = wait_for_index(mock_request, "file:///repo/main.cpp", initial_delay=0.01, poll_interval=0.01, max_wait=0.05)
        assert result is False

    def test_exception_during_poll(self):
        """轮询过程中抛异常，不应中断，继续重试。"""
        call_count = 0
        def mock_request(method, params):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RuntimeError("clangd not ready")
            return [{"name": "main", "kind": 12}]

        result = wait_for_index(mock_request, "file:///repo/main.cpp", initial_delay=0.01, poll_interval=0.01, max_wait=0.1)
        assert result is True
