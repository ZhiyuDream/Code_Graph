"""LLM客户端 - 统一的LLM调用层，支持多Provider切换

重构后：
- 模型配置从 model_config.ModelRegistry 读取，不再硬编码字符串判断
- 新增模型只需在 ModelRegistry 注册，无需修改此文件
- 调用层通过 ModelConfig 对象获取 provider/api_key/base_url/max_tokens字段等
"""
from __future__ import annotations

import json
import re
import time
import threading
from typing import Any

import logging
import os
from openai import OpenAI

from config import LLM_MODEL
from .model_config import ModelRegistry, ModelConfig

logger = logging.getLogger(__name__)

# 全局客户端实例缓存（key: provider_name）
_clients: dict[str, OpenAI] = {}

# 全局 token usage 记录（线程安全）
_usages_lock = threading.Lock()
_usages_global: list[dict] = []

# 调试日志：记录所有 LLM 调用（prompt / response）
_debug_calls: list[dict] = []
_debug_lock = threading.Lock()
_LLMM_DEBUG = os.environ.get("LLM_DEBUG_LOG", "") == "1"


def record_usage(usage):
    """记录一次调用的 token usage"""
    if usage:
        with _usages_lock:
            _usages_global.append({
                'prompt_tokens': getattr(usage, 'prompt_tokens', 0),
                'completion_tokens': getattr(usage, 'completion_tokens', 0),
                'total_tokens': getattr(usage, 'total_tokens', 0),
                'reasoning_tokens': getattr(getattr(usage, 'completion_tokens_details', None), 'reasoning_tokens', 0) or 0,
            })


def get_usage_stats() -> dict:
    """获取全局累计的 token usage 统计"""
    with _usages_lock:
        usages = list(_usages_global)
    if not usages:
        return {}
    return {
        'call_count': len(usages),
        'prompt_tokens': sum(u['prompt_tokens'] for u in usages),
        'completion_tokens': sum(u['completion_tokens'] for u in usages),
        'total_tokens': sum(u['total_tokens'] for u in usages),
        'reasoning_tokens': sum(u['reasoning_tokens'] for u in usages),
    }


def reset_usage_stats():
    """重置全局 token usage 记录"""
    with _usages_lock:
        _usages_global.clear()


def get_debug_calls() -> list[dict]:
    """获取所有记录的 LLM 调用日志"""
    with _debug_lock:
        return list(_debug_calls)


def clear_debug_calls():
    """清空 LLM 调用日志"""
    with _debug_lock:
        _debug_calls.clear()


def get_llm_client(cfg: ModelConfig = None) -> OpenAI:
    """获取 OpenAI 客户端实例（兼容旧接口）。"""
    if cfg is None:
        cfg = ModelRegistry.resolve(LLM_MODEL)
    return _get_client(cfg)


def _get_client(cfg: ModelConfig) -> OpenAI:
    """根据 ModelConfig 获取/创建 OpenAI 客户端实例"""
    key = cfg.provider
    if key in _clients:
        return _clients[key]

    if not cfg.api_key:
        raise ValueError(f"API key not set for provider '{cfg.provider}' (model: {cfg.name})")

    client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url or None)
    _clients[key] = client
    return client


def call_llm(
    messages: list[dict],
    max_tokens: int = 1000,
    timeout: int = 600,
    max_retries: int = 3,
    model: str = None,
    _no_reasoning_fallback: bool = False,
    **extra_kwargs
) -> str:
    """
    带重试机制的LLM调用。

    Args:
        messages: 消息列表
        max_tokens: 最大token数
        timeout: 超时时间（秒）
        max_retries: 最大重试次数
        model: 模型名称（默认使用 LLM_MODEL）
        **extra_kwargs: 额外参数（如 response_format）

    Returns:
        LLM返回的文本内容
    """
    model = model or LLM_MODEL
    cfg = ModelRegistry.resolve(model)
    client = _get_client(cfg)

    for attempt in range(max_retries):
        try:
            # 先提取内部参数，避免传给 API
            usage_sink = extra_kwargs.pop('_usage_sink', None)

            kwargs = {
                'model': cfg.name,
                'messages': messages,
                'timeout': timeout,
                cfg.max_tokens_param: max_tokens,
            }
            kwargs.update(extra_kwargs)

            resp = client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""

            # 记录 token usage
            if hasattr(resp, 'usage') and resp.usage:
                if usage_sink is not None:
                    usage_sink.append(resp.usage)
                else:
                    record_usage(resp.usage)

            # DeepSeek 等模型可能有 reasoning_content。
            # 对于普通文本调用，content 为空时 fallback 到 reasoning_content；
            # 对于 JSON 调用（_no_reasoning_fallback=True），保留空字符串让上层处理。
            if not content.strip() and not _no_reasoning_fallback:
                reasoning = getattr(resp.choices[0].message, 'reasoning_content', '')
                if reasoning:
                    content = reasoning

            if _LLMM_DEBUG:
                with _debug_lock:
                    _debug_calls.append({
                        "model": cfg.name,
                        "messages": messages,
                        "response": content,
                        "reasoning_content": getattr(resp.choices[0].message, 'reasoning_content', '') or '',
                        "timestamp": time.time(),
                    })
            return content or "(无答案)"
        except Exception as e:
            error_msg = str(e).lower()
            retryable = any(kw in error_msg for kw in [
                'timeout', 'connection', 'rate limit', 'too many requests',
                'temporarily unavailable', 'service unavailable', '503', '502', '504'
            ])

            if not retryable or attempt == max_retries - 1:
                return f"生成答案失败: {e}"

            wait_time = 2 ** attempt
            time.sleep(wait_time)

    return "生成答案失败: 达到最大重试次数"


def call_llm_json(
    messages: list[dict],
    max_tokens: int = 500,
    timeout: int = 600,
    model: str = None,
    _usage_sink: list | None = None,
) -> dict | None:
    """
    调用LLM并解析JSON响应。
    解析逻辑：提取代码块 → json.loads → json_repair。
    针对 DeepSeek 等 thinking-mode 模型：
      - 自动提升 max_tokens（reasoning_content 会消耗大量 token）
      - content 为空时尝试解析 reasoning_content
    失败返回 None，由上层处理。
    """
    from json_repair import repair_json

    model = model or LLM_MODEL
    sink = _usage_sink if _usage_sink is not None else []

    # DeepSeek 等 thinking-mode 模型：取消 max_tokens 限制
    # reasoning_content 会消耗大量 token，如果设上限可能挤占 content 空间导致 JSON 截断
    lower_model = model.lower() if model else ""
    if "deepseek" in lower_model:
        max_tokens = None

    def _try_parse(text: str) -> dict | None:
        """尝试从内容中解析 JSON"""
        text = text.strip()
        if not text or text.startswith("生成答案失败:"):
            return None

        # 1. 提取 ```json 代码块
        if '```json' in text:
            text = text.split('```json')[1].split('```')[0]
        elif '```' in text:
            parts = text.split('```')
            if len(parts) >= 2:
                text = parts[1]

        text = text.strip()
        if not text:
            return None

        # 2. 标准 JSON 解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 3. json_repair 兜底
        try:
            result = repair_json(text, return_objects=True)
            if isinstance(result, dict):
                return result
        except Exception:
            pass

        return None

    # 直接调用底层，禁止 reasoning_content → content 的 fallback，
    # 以便我们自己区分 content 和 reasoning_content。
    content = call_llm(
        messages, max_tokens, timeout,
        model=model, _usage_sink=sink,
        _no_reasoning_fallback=True,
    )

    # 优先解析 content（最终答案）
    result = _try_parse(content)
    if result is not None:
        if _usage_sink is None:
            for usage in sink:
                record_usage(usage)
        return result

    # 如果 content 为空或解析失败，尝试从 reasoning_content 中提取 JSON
    # （DeepSeek 等 thinking-mode 模型可能把决策写在思考过程中）
    if not content.strip():
        with _debug_lock:
            if _debug_calls:
                last_reasoning = _debug_calls[-1].get("reasoning_content", "")
                if last_reasoning:
                    result = _try_parse(last_reasoning)
                    if result is not None:
                        logger.info("Parsed JSON from reasoning_content fallback")
                        if _usage_sink is None:
                            for usage in sink:
                                record_usage(usage)
                        return result

    logger.warning("JSON parse failed. content=%s", content[:400])
    if _usage_sink is None:
        for usage in sink:
            record_usage(usage)
    return None
