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

            # DeepSeek 等模型可能有 reasoning_content
            # JSON mode 下 content 可能为空，但 reasoning_content 有内容，也要 fallback
            if not content.strip():
                reasoning = getattr(resp.choices[0].message, 'reasoning_content', '')
                if reasoning:
                    content = reasoning

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
    支持 response_format 强制 JSON 输出（DeepSeek/OpenAI）。
    使用 json_repair 做兜底修复。
    """
    from json_repair import repair_json

    model = model or LLM_MODEL
    cfg = ModelRegistry.resolve(model)

    # JSON 模式下确保 token 预算足够（DeepSeek reasoning 占用大）
    if cfg.reasoning_support and max_tokens < cfg.min_json_tokens:
        max_tokens = cfg.min_json_tokens

    extra = {}
    if cfg.supports_json_format:
        extra['response_format'] = {'type': 'json_object'}

    sink = _usage_sink if _usage_sink is not None else []
    content = call_llm(
        messages, max_tokens, timeout,
        model=model, _usage_sink=sink, **extra
    )

    # 如果没有外部 sink，将收集的 usage 同步到全局（保持兼容性）
    if _usage_sink is None:
        for usage in sink:
            record_usage(usage)

    if content.startswith("生成答案失败:"):
        return None

    # 多层兜底解析 JSON
    text = content.strip()
    _original_text = text[:800]  # 保留用于日志

    # 1. 提取 ```json 代码块
    if '```json' in text:
        text = text.split('```json')[1].split('```')[0]
    elif '```' in text:
        parts = text.split('```')
        if len(parts) >= 2:
            text = parts[1]

    text = text.strip()
    if not text:
        logger.warning("JSON parse failed: empty content after extracting code block. original=%s", _original_text)
        return None

    # 2. 标准 JSON 解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. json_repair 兜底修复（return_objects=True 直接返回解析后的对象）
    try:
        result = repair_json(text, return_objects=True)
        if isinstance(result, dict):
            return result
    except Exception as e:
        logger.warning("json_repair failed: %s. text=%s", e, text[:500])

    # 4. 尝试提取第一个 {...} 或 [...]
    match = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
    if match:
        try:
            result = repair_json(match.group(1), return_objects=True)
            if isinstance(result, dict):
                return result
        except Exception as e:
            logger.warning("Extract-brace JSON repair failed: %s. text=%s", e, text[:500])

    logger.error("JSON parse completely failed. original=%s", _original_text)
    return None
