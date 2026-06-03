"""模型配置中心 — 统一管理多Provider模型参数。

设计原则：
- 模型配置集中注册，不在调用层硬编码字符串判断
- 新增模型只需在注册表加一行，无需改调用逻辑
- 调用层通过 ModelConfig 对象获取所有参数（api_key, base_url, max_tokens字段等）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import os


@dataclass(frozen=True)
class ModelConfig:
    """单个模型的完整配置"""
    name: str                          # 模型标识名，如 "deepseek-v4-pro"
    provider: str                      # "openai" | "deepseek" | "moonshot" ...
    api_key: str                       # API Key
    base_url: Optional[str] = None     # 自定义 Base URL
    max_tokens_param: str = "max_tokens"  # "max_tokens" 或 "max_completion_tokens"
    supports_json_format: bool = True  # 是否支持 response_format={"type":"json_object"}
    reasoning_support: bool = False    # 是否可能返回 reasoning_content
    min_json_tokens: int = 500         # JSON 模式下的最小 max_tokens
    timeout: int = 600                 # 默认超时（秒）

    def get_client_kwargs(self, max_tokens: int, **extra) -> dict:
        """构建传给 OpenAI client.chat.completions.create 的参数"""
        kwargs = {
            "model": self.name,
            self.max_tokens_param: max_tokens,
        }
        if self.supports_json_format:
            json_fmt = extra.get("response_format")
            if json_fmt:
                kwargs["response_format"] = json_fmt
        return kwargs


class ModelRegistry:
    """模型注册表 — 全局单例，运行期不可变"""

    _models: dict[str, ModelConfig] = {}
    _initialized: bool = False

    @classmethod
    def register(cls, cfg: ModelConfig) -> None:
        cls._models[cfg.name] = cfg

    @classmethod
    def get(cls, name: str) -> Optional[ModelConfig]:
        if not cls._initialized:
            cls._init()
        return cls._models.get(name)

    @classmethod
    def resolve(cls, name: str) -> ModelConfig:
        """
        解析模型配置。优先查注册表，未命中则自动推断。
        """
        if not cls._initialized:
            cls._init()
        cfg = cls._models.get(name)
        if cfg:
            return cfg

        # 未命中：根据名称特征自动推断（兜底）
        lower = name.lower()
        if "deepseek" in lower:
            from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL
            cfg = ModelConfig(
                name=name,
                provider="deepseek",
                api_key=DEEPSEEK_API_KEY,
                base_url=DEEPSEEK_BASE_URL or None,
                max_tokens_param="max_tokens",
                supports_json_format=True,
                reasoning_support=True,
                min_json_tokens=1200,
            )
        else:
            from config import OPENAI_API_KEY, OPENAI_BASE_URL
            is_new = name.startswith(("gpt-5", "o1", "o3"))
            cfg = ModelConfig(
                name=name,
                provider="openai",
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_BASE_URL or None,
                max_tokens_param="max_completion_tokens" if is_new else "max_tokens",
                supports_json_format=True,
                reasoning_support=False,
            )
        cls._models[name] = cfg
        return cfg

    @classmethod
    def list_models(cls) -> list[str]:
        if not cls._initialized:
            cls._init()
        return list(cls._models.keys())

    @classmethod
    def _init(cls) -> None:
        """从 config 加载预设模型（延迟初始化）"""
        if cls._initialized:
            return

        # 避免循环导入：函数内导入
        from config import (
            OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL,
            DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
        )

        # OpenAI 兼容模型
        if OPENAI_API_KEY:
            openai_models = [LLM_MODEL, "gpt-4o", "gpt-4o-mini", "gpt-4", "gpt-3.5-turbo"]
            for m in set(openai_models):
                if not m:
                    continue
                is_new = m.startswith(("gpt-5", "o1", "o3"))
                cls.register(ModelConfig(
                    name=m,
                    provider="openai",
                    api_key=OPENAI_API_KEY,
                    base_url=OPENAI_BASE_URL or None,
                    max_tokens_param="max_completion_tokens" if is_new else "max_tokens",
                    supports_json_format=True,
                    reasoning_support=False,
                ))

        # DeepSeek 模型
        if DEEPSEEK_API_KEY:
            deepseek_models = ["deepseek-chat", "deepseek-v4-pro", "deepseek-coder"]
            for m in deepseek_models:
                cls.register(ModelConfig(
                    name=m,
                    provider="deepseek",
                    api_key=DEEPSEEK_API_KEY,
                    base_url=DEEPSEEK_BASE_URL or None,
                    max_tokens_param="max_tokens",
                    supports_json_format=True,
                    reasoning_support=True,
                    min_json_tokens=1200,
                ))

        cls._initialized = True
