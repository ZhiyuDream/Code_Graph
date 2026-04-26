"""LLM客户端 - 统一的LLM调用层，支持多Provider切换"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from openai import OpenAI
from config import OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL

# 全局客户端实例缓存
_clients = {}


def get_llm_client(provider: str = "openai") -> OpenAI:
    """
    获取LLM客户端实例（支持多Provider）
    
    Args:
        provider: "openai" (默认) 或 "deepseek"
    """
    if provider in _clients:
        return _clients[provider]
    
    if provider == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY not set")
    else:
        api_key = OPENAI_API_KEY
        base_url = OPENAI_BASE_URL or None
    
    client = OpenAI(api_key=api_key, base_url=base_url or None)
    _clients[provider] = client
    return client


def _get_model_config(model: str):
    """获取模型配置参数"""
    # 判断是否需要 max_completion_tokens
    is_new_openai = model.startswith('gpt-5') or model.startswith('o1') or model.startswith('o3')
    # DeepSeek 使用 max_tokens
    is_deepseek = 'deepseek' in model.lower()
    
    return {
        'is_new_openai': is_new_openai,
        'is_deepseek': is_deepseek,
        'provider': 'deepseek' if is_deepseek else 'openai',
    }


def call_llm(
    messages: list[dict],
    max_tokens: int = 1000,
    timeout: int = 60,
    max_retries: int = 3,
    model: str = None,
    provider: str = None,
    **extra_kwargs
) -> str:
    """
    带重试机制的LLM调用，支持多Provider
    
    Args:
        messages: 消息列表
        max_tokens: 最大token数
        timeout: 超时时间（秒）
        max_retries: 最大重试次数
        model: 模型名称（默认使用 LLM_MODEL）
        provider: 提供商（"openai" 或 "deepseek"，默认自动推断）
        
    Returns:
        LLM返回的文本内容
    """
    model = model or LLM_MODEL
    config = _get_model_config(model)
    provider = provider or config['provider']
    client = get_llm_client(provider)
    
    for attempt in range(max_retries):
        try:
            kwargs = {
                'model': model,
                'messages': messages,
                'timeout': timeout
            }
            if config['is_new_openai']:
                kwargs['max_completion_tokens'] = max_tokens
            else:
                kwargs['max_tokens'] = max_tokens
            kwargs.update(extra_kwargs)
            
            resp = client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            
            # DeepSeek 可能有 reasoning_content
            # 注意：如果使用了 response_format={'type': 'json_object'}，
            # 不应 fallback 到 reasoning_content（reasoning 不是 JSON）
            is_json_mode = extra_kwargs.get('response_format', {}).get('type') == 'json_object'
            if not content.strip() and not is_json_mode:
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
    timeout: int = 60,
    model: str = None,
    provider: str = None
) -> dict | None:
    """
    调用LLM并解析JSON响应
    支持 response_format 强制 JSON 输出（DeepSeek/OpenAI）
    使用 json_repair 做兜底修复
    
    Args:
        messages: 消息列表
        max_tokens: 最大token数
        timeout: 超时时间
        model: 模型名称
        provider: 提供商
        
    Returns:
        解析后的JSON字典或None
    """
    import json
    import re
    from json_repair import repair_json
    
    model = model or LLM_MODEL
    config = _get_model_config(model)
    provider = provider or config['provider']
    
    # DeepSeek 和 OpenAI 新模型支持 response_format 强制 JSON
    extra_kwargs = {}
    if provider == 'deepseek' or config['is_new_openai']:
        extra_kwargs['response_format'] = {'type': 'json_object'}
    
    # DeepSeek reasoning 占用大量 token，JSON 输出需要更大预算
    if config['is_deepseek'] and max_tokens < 1200:
        max_tokens = 1200
    
    content = call_llm(
        messages, max_tokens, timeout,
        model=model, provider=provider, **extra_kwargs
    )
    
    if content.startswith("生成答案失败:"):
        return None
    
    # 尝试提取JSON（多层兜底）
    text = content.strip()
    
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
    
    # 3. json_repair 兜底修复
    try:
        repaired = repair_json(text)
        return json.loads(repaired)
    except Exception:
        pass
    
    # 4. 尝试提取第一个 {...} 或 [...]
    match = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
    if match:
        try:
            repaired = repair_json(match.group(1))
            return json.loads(repaired)
        except Exception:
            pass
    
    return None
