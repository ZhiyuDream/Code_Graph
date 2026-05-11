"""Pipeline 共享数据模型。所有模块通过强类型 dataclass 交互。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FunctionSymbol:
    """函数符号。"""
    id: str = ""  # 在提取后填充: "{file_path}:{name}:{start_line}"
    name: str = ""
    signature: str = ""
    file_path: str = ""
    start_line: int = 0
    end_line: int = 0
    start_character: int = 0
    parent_class: str | None = None  # Python class methods


@dataclass
class ClassSymbol:
    """类/结构体符号。"""
    name: str
    file_path: str
    start_line: int
    end_line: int


@dataclass
class VariableSymbol:
    """变量符号（含参数、成员、局部、全局）。"""
    id: str
    name: str
    file_path: str
    start_line: int
    kind: str  # "param", "member", "local", "global"
    scope_function_index: int | None = None
    scope_class_index: int | None = None
    start_character: int = 0


@dataclass
class RawCall:
    """原始调用记录（由 symbol_extractor 从 clangd callHierarchy 产出）。"""
    caller_index: int
    callee_name: str
    file_path: str
    line: int
    callee_file_path: str | None = None
    callee_line: int | None = None


@dataclass
class FileResult:
    """单个文件的解析结果。"""
    file_path: str
    functions: list[FunctionSymbol] = field(default_factory=list)
    classes: list[ClassSymbol] = field(default_factory=list)
    variables: list[VariableSymbol] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    var_refs: list[tuple[int, str, int]] = field(default_factory=list)
    # 额外的原始字段，供下游模块使用
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolvedCalls:
    """call_resolver 的输出：精确解析后的调用关系。"""
    calls: list[tuple[str, str]] = field(default_factory=list)
    # (caller_id, callee_name, candidate_ids)
    ambiguous: list[tuple[str, str, list[str]]] = field(default_factory=list)
    # (caller_id, callee_name)
    unresolved: list[tuple[str, str]] = field(default_factory=list)
