"""Prompt 加载器 - 从 prompts/ 目录加载和管理 prompt"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_cache = {}


def load_prompt(name: str, **kwargs) -> str:
    """
    加载 prompt 文件并替换变量
    
    Args:
        name: prompt 文件名（不含 .txt 后缀）
        **kwargs: 变量替换映射
        
    Returns:
        替换后的 prompt 字符串
    """
    cache_key = f"{name}:{json.dumps(kwargs, sort_keys=True, ensure_ascii=False)}"
    if cache_key in _cache:
        return _cache[cache_key]
    
    prompt_path = _PROMPTS_DIR / f"{name}.txt"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt not found: {prompt_path}")
    
    text = prompt_path.read_text(encoding="utf-8")
    
    # 变量替换
    if kwargs:
        text = text.format(**kwargs)
    
    _cache[cache_key] = text
    return text


def load_actions() -> list[dict]:
    """加载 ReAct action 定义"""
    actions_path = _PROMPTS_DIR / "react_actions.json"
    if not actions_path.exists():
        raise FileNotFoundError(f"Actions not found: {actions_path}")
    
    data = json.loads(actions_path.read_text(encoding="utf-8"))
    return data.get("actions", [])


def format_actions_for_prompt(actions: list[dict] | None = None) -> str:
    """将 action 定义格式化为 prompt 中的列表文本"""
    if actions is None:
        actions = load_actions()
    
    lines = []
    for i, action in enumerate(actions, 1):
        name = action["name"]
        desc = action["description"]
        applicable = action.get("applicable", "")
        lines.append(f"{i}. {name} - {desc}")
        if applicable:
            lines.append(f"   适用：{applicable}")
    return "\n".join(lines)


def get_action_names(actions: list[dict] | None = None) -> list[str]:
    """获取所有 action 名称列表"""
    if actions is None:
        actions = load_actions()
    return [a["name"] for a in actions]


def get_action_impl(action_name: str) -> str | None:
    """获取 action 的实现标识"""
    for action in load_actions():
        if action["name"] == action_name:
            return action.get("impl")
    return None


def clear_cache():
    """清除 prompt 缓存"""
    _cache.clear()
