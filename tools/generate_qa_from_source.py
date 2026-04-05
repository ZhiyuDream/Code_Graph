import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
基于 llama.cpp 源码直接生成 QA 数据集，不依赖图谱。

设计原则：
- 问题从源码出，答案从源码提取
- 图谱仅用于事后验证覆盖度
- 模板填空中使用真实函数/结构体/模块名

模板来源：Question_templete.xlsx（Llm.cpp 化映射版）
输出格式：对齐现有 CSV 格式
"""

from __future__ import annotations

import csv
import re
import random
from pathlib import Path
from collections import defaultdict
from typing import Optional

# ============ 配置 ============
LLAMA_SRC = Path(__file__).resolve().parent.parent / "llama.cpp" / "src"
GGML_SRC = Path(__file__).resolve().parent.parent / "llama.cpp" / "ggml" / "src"
COMMON_SRC = Path(__file__).resolve().parent.parent / "llama.cpp" / "common"
OUTPUT = Path(__file__).resolve().parent / "llama_cpp_QA_from_source.csv"

random.seed(42)

# ============ Llama.cpp 化的模板映射 ============
# 原始模板 -> (Llama.cpp 适用版本, 问题类型, 意图, 一级分类, 二级分类)

TEMPLATE_MAP: list[dict] = [
    # How 类 - 系统设计
    {
        "template": "在 llama.cpp 中，<Module>（如 ggml/llama/common）如何实现 <Feature> 的设计？",
        "llama_template": "在 llama.cpp 中，{module} 模块如何实现 {feature} 的设计？",
        "qtype": "how",
        "intent": "System Design",
        "cat1": "项目级",
        "cat2": "System Design",
    },
    {
        "template": "绘制 <Function> 的调用时序图，说明其工作流程",
        "llama_template": "绘制 {func} 的调用时序图，说明其工作流程",
        "qtype": "how",
        "intent": "System Design",
        "cat1": "函数级",
        "cat2": "Function",
    },
    {
        "template": "介绍 <Struct> 的数据结构设计及其字段含义",
        "llama_template": "介绍 {struct} 的数据结构设计及其字段含义",
        "qtype": "how",
        "intent": "Algorithm Implementation",
        "cat1": "结构体级",
        "cat2": "Struct",
    },
    {
        "template": "<Module> 中 <Operation> 的处理流程是什么？",
        "llama_template": "{module} 中 {operation} 的处理流程是什么？",
        "qtype": "how",
        "intent": "System Design",
        "cat1": "模块级",
        "cat2": "Module",
    },
    # How 类 - 算法实现
    {
        "template": "在 <Condition> 场景下，<Module> 如何实现 <Feature>？",
        "llama_template": "在 {condition} 场景下，{module} 如何实现 {feature}？",
        "qtype": "how",
        "intent": "Algorithm Implementation",
        "cat1": "模块级",
        "cat2": "Module",
    },
    {
        "template": "<Module> 的 <Algorithm> 算法是如何实现的？",
        "llama_template": "{module} 的 {algorithm} 算法是如何实现的？",
        "qtype": "how",
        "intent": "Algorithm Implementation",
        "cat1": "模块级",
        "cat2": "Module",
    },
    # How 类 - API/框架支持
    {
        "template": "<Module> 如何向其他组件暴露公共接口？",
        "llama_template": "{module} 如何向其他组件暴露公共接口？",
        "qtype": "how",
        "intent": "API / Framework Support",
        "cat1": "模块级",
        "cat2": "Module",
    },
    {
        "template": "<Function> 的使用方法是什么？",
        "llama_template": "{func} 的使用方法是什么？",
        "qtype": "how",
        "intent": "API / Framework Support",
        "cat1": "函数级",
        "cat2": "Function",
    },
    {
        "template": "如何使用 <Function1> 和 <Function2> 实现 <Feature>？",
        "llama_template": "如何使用 {func1} 和 {func2} 实现 {feature}？",
        "qtype": "how",
        "intent": "API / Framework Support",
        "cat1": "函数级",
        "cat2": "Function",
    },
    # How 类 - 功能定位
    {
        "template": "<Feature> 的核心逻辑在代码库中何处实现？",
        "llama_template": "{feature} 的核心逻辑在代码库中何处实现？",
        "qtype": "how",
        "intent": "Feature Location",
        "cat1": "功能级",
        "cat2": "Feature",
    },
    {
        "template": "<Function> 函数的实现位于哪个文件？",
        "llama_template": "{func} 函数的实现位于哪个文件？",
        "qtype": "how",
        "intent": "Feature Location",
        "cat1": "函数级",
        "cat2": "Function",
    },
    # Where 类 - 数据/控制流
    {
        "template": "上游函数在何处调用 <Function>？传递的数据结构是什么？",
        "llama_template": "上游函数在何处调用 {func}？传递的数据结构是什么？",
        "qtype": "where",
        "intent": "Data / Control-flow",
        "cat1": "函数级",
        "cat2": "Function",
    },
    {
        "template": "<Function> 中的 <Variable> 从何处获取？其使用方式是什么？",
        "llama_template": "{func} 中的变量 {var_name} 从何处获取？其使用方式是什么？",
        "qtype": "where",
        "intent": "Data / Control-flow",
        "cat1": "函数级",
        "cat2": "Function",
    },
    # Where 类 - 标识符定位
    {
        "template": "<Error> 错误标识符在代码中何处定义？",
        "llama_template": "{error_type} 错误在代码中何处定义？",
        "qtype": "where",
        "intent": "Identifier Location",
        "cat1": "常量级",
        "cat2": "Identifier",
    },
    # Why 类 - 功能目的探索
    {
        "template": "为何 <Function> 在 <Module> 中承担特定职责？",
        "llama_template": "为何 {func} 在 {module} 中承担特定职责？",
        "qtype": "why",
        "intent": "Purpose Exploration",
        "cat1": "函数级",
        "cat2": "Function",
    },
    {
        "template": "为何 <Function> 被设计为满足特定条件或需求？",
        "llama_template": "为何 {func} 被设计为满足特定条件或需求？",
        "qtype": "why",
        "intent": "Design rationale",
        "cat1": "函数级",
        "cat2": "Function",
    },
    # Why 类 - 性能考量
    {
        "template": "为何 <Module> 在高并发场景下无法高效扩展？",
        "llama_template": "为何 {module} 在高并发场景下存在性能瓶颈？",
        "qtype": "why",
        "intent": "Performance",
        "cat1": "模块级",
        "cat2": "Module",
    },
    # What 类 - 概念/定义
    {
        "template": "<Function> 函数执行后，预期的状态或结果是什么？",
        "llama_template": "{func} 函数执行后，预期的状态或结果是什么？",
        "qtype": "what",
        "intent": "Concept / Definition",
        "cat1": "函数级",
        "cat2": "Function",
    },
    {
        "template": "<Struct> 处理的输入和输出数据字段是什么？",
        "llama_template": "{struct} 处理的输入和输出数据字段是什么？",
        "qtype": "what",
        "intent": "Concept / Definition",
        "cat1": "结构体级",
        "cat2": "Struct",
    },
    {
        "template": "<Function> 函数中，<Variable> 变量的含义或目的是什么？",
        "llama_template": "{func} 函数中，变量 {var_name} 的含义或目的是什么？",
        "qtype": "what",
        "intent": "Concept / Definition",
        "cat1": "函数级",
        "cat2": "Function",
    },
    {
        "template": "<Function> 函数的预期输入参数和返回值是什么？",
        "llama_template": "{func} 函数的预期输入参数和返回值是什么？",
        "qtype": "what",
        "intent": "Concept / Definition",
        "cat1": "函数级",
        "cat2": "Function",
    },
    {
        "template": "介绍 <Interface>，说明其主要实现的功能",
        "llama_template": "介绍 {interface}，说明其主要实现的功能",
        "qtype": "what",
        "intent": "Concept / Definition",
        "cat1": "接口级",
        "cat2": "Interface",
    },
    {
        "template": "<Module> 的功能是什么？",
        "llama_template": "{module} 的功能是什么？",
        "qtype": "what",
        "intent": "Concept / Definition",
        "cat1": "模块级",
        "cat2": "Module",
    },
    {
        "template": "<Feature> 的具体含义及作用是什么？",
        "llama_template": "{feature} 的具体含义及作用是什么？",
        "qtype": "what",
        "intent": "Concept / Definition",
        "cat1": "功能级",
        "cat2": "Feature",
    },
    # What 类 - 依赖追踪
    {
        "template": "<Function> 函数会引入哪些副作用？",
        "llama_template": "{func} 函数会引入哪些副作用？",
        "qtype": "what",
        "intent": "Dependency tracing",
        "cat1": "函数级",
        "cat2": "Function",
    },
    {
        "template": "修改 <Function> 函数的返回值可能造成什么潜在后果？",
        "llama_template": "修改 {func} 函数的返回值可能造成什么潜在后果？",
        "qtype": "what",
        "intent": "Dependency tracing",
        "cat1": "函数级",
        "cat2": "Function",
    },
    {
        "template": "哪些函数依赖 <Struct> 中的 <Field>？",
        "llama_template": "哪些函数依赖 {struct} 中的 {field}？",
        "qtype": "what",
        "intent": "Dependency tracing",
        "cat1": "结构体级",
        "cat2": "Struct",
    },
    {
        "template": "分析 <Module> 的调用关系，详细梳理其直接调用者和间接调用者",
        "llama_template": "分析 {module} 的调用关系，详细梳理其直接调用者和间接调用者",
        "qtype": "what",
        "intent": "Dependency tracing",
        "cat1": "模块级",
        "cat2": "Module",
    },
    # What 类 - 架构探索
    {
        "template": "<Module> 架构中的核心层级及其各自职责是什么？",
        "llama_template": "{module} 架构中的核心层级及其各自职责是什么？",
        "qtype": "what",
        "intent": "Architecture exploration",
        "cat1": "模块级",
        "cat2": "Module",
    },
    {
        "template": "用类图呈现 <StructA> 与 <StructB> 之间的数据映射关系",
        "llama_template": "用结构体关系说明 {struct_a} 与 {struct_b} 之间的数据映射关系",
        "qtype": "what",
        "intent": "Architecture exploration",
        "cat1": "结构体级",
        "cat2": "Struct",
    },
    {
        "template": "介绍 <Module> 处理的主要数据类型及其数据结构",
        "llama_template": "介绍 {module} 处理的主要数据类型及其数据结构",
        "qtype": "what",
        "intent": "Architecture exploration",
        "cat1": "模块级",
        "cat2": "Module",
    },
    {
        "template": "输出 <Module> 的代码框架，标注关键函数和接口",
        "llama_template": "输出 {module} 的代码框架，标注关键函数和接口",
        "qtype": "what",
        "intent": "Architecture exploration",
        "cat1": "模块级",
        "cat2": "Module",
    },
]

# ============ Llama.cpp 真实实体池 ============

# 模块
MODULES = [
    ("ggml", "GGML 张量计算库"),
    ("llama", "Llama 模型推理核心"),
    ("common", "公共工具和 CLI 封装"),
    ("server", "HTTP 服务器模块"),
    ("examples", "示例程序集合"),
]

# 特征/功能关键词
FEATURES = [
    "张量运算", "矩阵乘法", "注意力机制", "RoPE 位置编码",
    "KV Cache 管理", "批量解码", "采样策略", "logits 处理",
    "上下文窗口", "模型加载", "vulkan 加速", "cuda 加速",
    "Metal 加速", "量化推理", "KV Cache 量化", "专家混合(MoE)",
]

# 算法关键词
ALGORITHMS = [
    "矩阵乘法(MatMul)", "注意力(Attention)", "Softmax", "LayerNorm",
    "RoPE", "MoE", "KV Cache", "量化反量化", " logits 采样",
]

# 操作关键词
OPERATIONS = [
    "模型初始化", "token 采样", "上下文编码", "logits 计算",
    "批量推理", "热key处理", "内存管理", "后端选择",
]

# 条件场景
CONDITIONS = [
    "GPU 内存不足", "长上下文", "多序列并发", "量化精度损失",
    "后端不支持", "模型架构不匹配", "token 溢出",
]

# 错误类型
ERROR_TYPES = [
    "OOM(内存溢出)", "CUDA 错误", "Vulkan 错误", "Metal 错误",
    "模型格式错误", "量化参数错误", "上下文长度超限",
]


# ============ 源码解析工具 ============

def scan_source_files() -> dict:
    """扫描所有源文件，按模块分组"""
    modules = {}
    patterns = [
        (LLAMA_SRC, "llama"),
        (GGML_SRC, "ggml"),
        (COMMON_SRC, "common"),
    ]
    for base, name in patterns:
        if base.exists():
            files = list(base.glob("*.c")) + list(base.glob("*.cpp")) + list(base.glob("*.h"))
            modules[name] = sorted(set(str(f.relative_to(base)) for f in files if f.is_file()))
    return modules


def extract_functions_from_file(filepath: Path) -> list[dict]:
    """从单个源文件提取函数定义"""
    functions = []
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return functions

    # 匹配函数定义：返回值 函数名(参数)
    # 排除static inline、static const、宏定义等
    func_pattern = re.compile(
        r'^(?:static\s+)?(?:inline\s+)?(?:\w+\s*\*?)\s+(\w+)\s*\(([^)]*)\)\s*\{',
        re.MULTILINE
    )

    # 提取注释（用于获取函数说明）
    comment_pattern = re.compile(
        r'/\*\*([\s\S]*?)\*/|//\s*(.+)',
    )

    lines = content.split('\n')
    comments = {}
    for i, line in enumerate(lines):
        if '//' in line:
            comments[i] = line.split('//', 1)[-1].strip()
        elif '/*' in line and '*/' in line:
            m = re.search(r'/\*\s*(.+?)\s*\*/', line)
            if m:
                comments[i] = m.group(1)

    for m in func_pattern.finditer(content):
        func_name = m.group(1)
        params = m.group(2).strip()
        line_num = content[:m.start()].count('\n') + 1

        # 跳过过于简短的（可能是内联简单实现）
        if func_name.startswith('_') or func_name in ['if', 'for', 'while', 'switch']:
            continue

        # 收集函数前的注释
        doc = ""
        start_pos = m.start()
        search_start = max(0, start_pos - 500)
        preceding = content[search_start:start_pos]
        # 找最后一个 /** 或 //
        last_comment = re.findall(r'(?:/\*\*|//)\s*(.+?)(?:\n|$)', preceding, re.DOTALL)
        if last_comment:
            doc = last_comment[-1].strip()
            doc = re.sub(r'^\s*\*\s?', '', doc).strip()

        functions.append({
            "name": func_name,
            "file": str(filepath),
            "line": line_num,
            "params": params,
            "doc": doc[:200] if doc else "",
        })

    return functions


def extract_structs_from_file(filepath: Path) -> list[dict]:
    """从源文件提取结构体定义"""
    structs = []
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return structs

    # 匹配 struct/union/typedef struct
    struct_pattern = re.compile(
        r'(?:typedef\s+)?(?:struct|union)\s+(\w+)\s*\{([^}]+)\}',
        re.MULTILINE
    )

    for m in struct_pattern.finditer(content):
        name = m.group(1)
        body = m.group(2)
        line_num = content[:m.start()].count('\n') + 1

        # 提取字段
        fields = []
        for field_line in body.split(';'):
            field_line = field_line.strip()
            if not field_line or field_line.startswith('//'):
                continue
            # 匹配类型 + 字段名 [+ 数组]
            fm = re.match(r'(?:const\s+)?(\w+\s*\*?)\s+(\w+)', field_line)
            if fm:
                ftype = fm.group(1).strip()
                fname = fm.group(2).strip()
                if not fname.startswith('_'):
                    fields.append(f"{ftype} {fname}")

        if fields:
            structs.append({
                "name": name,
                "file": str(filepath),
                "line": line_num,
                "fields": fields[:10],  # 最多10个字段
            })

    return structs


def get_all_entities() -> dict:
    """收集所有实体，构建实体池"""
    modules = scan_source_files()
    all_funcs = []
    all_structs = []

    for mod_name, files in modules.items():
        base = None
        if mod_name == "llama":
            base = LLAMA_SRC
        elif mod_name == "ggml":
            base = GGML_SRC
        elif mod_name == "common":
            base = COMMON_SRC

        if base:
            for rel_file in files:
                fpath = base / rel_file
                if fpath.exists():
                    all_funcs.extend(extract_functions_from_file(fpath))
                    all_structs.extend(extract_structs_from_file(fpath))

    return {
        "functions": all_funcs,
        "structs": all_structs,
        "modules": modules,
    }


def find_caller_of(target_func: str, all_funcs: list[dict]) -> list[dict]:
    """在同文件或跨文件找调用 target_func 的函数"""
    callers = []
    target_lower = target_func.lower()
    for f in all_funcs:
        if f["file"].endswith(('.c', '.cpp')):
            try:
                content = Path(f["file"]).read_text(encoding="utf-8", errors="ignore")
                if target_lower in content.lower() and f["name"] != target_func:
                    callers.append(f)
            except Exception:
                pass
    return callers[:5]


def extract_function_body(func: dict, all_funcs: list[dict]) -> str:
    """提取函数体作为答案来源"""
    try:
        content = Path(func["file"]).read_text(encoding="utf-8", errors="ignore")
        lines = content.split('\n')

        # 找到函数定义的行
        start = func["line"] - 1
        # 简单策略：找到下一个同缩进的函数定义或文件结束
        base_indent = len(lines[start]) - len(lines[start].lstrip()) if lines[start].strip() else 0

        end = start + 1
        brace_count = 0
        found_open = False
        for i in range(start, min(start + 200, len(lines))):
            line = lines[i]
            brace_count += line.count('{') - line.count('}')
            if '{' in line:
                found_open = True
            if found_open and brace_count == 0 and i > start:
                end = i + 1
                break
            if i - start > 150:  # 超长函数截断
                end = start + 150
                break

        body = '\n'.join(lines[start:end])
        return body[:1000]  # 最多1000字符
    except Exception as e:
        return f"// 无法读取函数体: {e}"


# ============ 问题生成器 ============

def _extract_vars_from_params(params_str: str) -> list[str]:
    """从函数参数字符串提取变量名"""
    if not params_str or params_str.strip() == "void" or params_str.strip() == "":
        return []
    # 匹配 类型 变量名 或 类型 *变量名
    vars = re.findall(r'(?:const\s+)?(?:\w+\s*\*?)\s+(\w+)', params_str)
    return [v for v in vars if not v.startswith('_') and v not in ('void', 'int', 'float', 'double', 'char', 'bool')]


def fill_template(template: dict, entities: dict) -> Optional[dict]:
    """用真实实体填充模板，生成一道题"""

    t = template["llama_template"]
    qtype = template["qtype"]
    intent = template["intent"]
    cat1 = template["cat1"]
    cat2 = template["cat2"]

    funcs = entities["functions"]
    structs = entities["structs"]

    question = t
    entity_name = ""
    evidence = ""
    answer = ""

    # 先选择实体（还没填进去）
    func = random.choice(funcs) if funcs else None
    struct = random.choice(structs) if structs else None
    module = random.choice(MODULES)
    feature = random.choice(FEATURES)
    algorithm = random.choice(ALGORITHMS)
    operation = random.choice(OPERATIONS)
    condition = random.choice(CONDITIONS)
    error_type = random.choice(ERROR_TYPES)

    # ========== A组：最复杂的复合模板（3个占位符）============

    # A1: {condition} + {module} + {feature}
    if "{condition}" in question and "{module}" in question and "{feature}" in question:
        mod_name, mod_desc = module
        question = question.replace("{condition}", condition).replace("{module}", mod_name).replace("{feature}", feature)
        entity_name = f"{mod_name}::{feature}"
        evidence = f"{mod_name} 模块"
        answer = f"// 在 {condition} 场景下，{mod_name} 模块如何实现 {feature}\n"
        answer += f"// {mod_name} 模块说明: {mod_desc}"

    # A2: {func} + {module} (需要func存在)
    elif "{func}" in question and "{module}" in question and func:
        func_name = func["name"]
        mod_name, mod_desc = module
        question = question.replace("{func}", func_name).replace("{module}", mod_name)
        entity_name = f"{mod_name}::{func_name}"
        evidence = f"{func['file']}:{func['line']}"
        body = extract_function_body(func, funcs)
        answer = f"// {func_name} 定义于 {func['file']} 第 {func['line']} 行\n"
        if func["doc"]:
            answer += f"// 注释: {func['doc']}\n"
        answer += f"// 签名: {func_name}({func['params']})\n"
        answer += f"// 该函数属于 {mod_name} 模块（{mod_desc}）\n"
        answer += f"\n函数体片段:\n{body[:500]}"

    # A3: {func} + {var_name}
    elif "{func}" in question and "{var_name}" in question and func:
        func_name = func["name"]
        var_names = _extract_vars_from_params(func["params"])
        if not var_names:
            return None
        var_name = random.choice(var_names)
        question = question.replace("{func}", func_name).replace("{var_name}", var_name)
        entity_name = f"{func_name}::{var_name}"
        evidence = f"{func['file']}:{func['line']}"
        answer = f"// {var_name} 是 {func_name} 的参数之一\n"
        answer += f"// {func_name} 定义于 {func['file']} 第 {func['line']} 行\n"
        answer += f"// 签名: {func_name}({func['params']})"

    # A4: {struct} + {field}
    elif "{struct}" in question and "{field}" in question and struct:
        if not struct["fields"]:
            return None
        struct_name = struct["name"]
        field = random.choice(struct["fields"])
        question = question.replace("{struct}", struct_name).replace("{field}", field)
        entity_name = f"{struct_name}::{field}"
        evidence = f"{struct['file']}:{struct['line']}"
        answer = f"// {field} 是 {struct_name} 的一个字段\n"
        answer += f"// {struct_name} 定义于 {struct['file']} 第 {struct['line']} 行\n"
        answer += "// 字段列表:\n  " + "\n  ".join(struct["fields"][:8])

    # ========== B组：双占位符复合模板 ==========

    # B1: {module} + {feature}
    elif "{module}" in question and "{feature}" in question:
        mod_name, mod_desc = module
        question = question.replace("{module}", mod_name).replace("{feature}", feature)
        entity_name = f"{mod_name}::{feature}"
        evidence = f"{mod_name} 模块"
        answer = f"// {mod_name} 模块说明: {mod_desc}\n"
        answer += f"// {feature} 是该模块的一个功能特性\n"
        files = entities["modules"].get(mod_name, [])[:5]
        if files:
            answer += f"// 关键文件: {', '.join(files)}"

    # B2: {module} + {operation}
    elif "{module}" in question and "{operation}" in question:
        mod_name, mod_desc = module
        question = question.replace("{module}", mod_name).replace("{operation}", operation)
        entity_name = f"{mod_name}::{operation}"
        evidence = f"{mod_name} 模块"
        answer = f"// {mod_name} 模块说明: {mod_desc}\n"
        answer += f"// {operation} 是该模块的一个操作流程"

    # B3: {module} + {algorithm}
    elif "{module}" in question and "{algorithm}" in question:
        mod_name, mod_desc = module
        question = question.replace("{module}", mod_name).replace("{algorithm}", algorithm)
        entity_name = f"{mod_name}::{algorithm}"
        evidence = f"{mod_name} 模块"
        answer = f"// {mod_name} 模块说明: {mod_desc}\n"
        answer += f"// {algorithm} 是该模块使用的一种算法"

    # B4: {struct_a} + {struct_b}
    elif "{struct_a}" in question and "{struct_b}" in question:
        if len(structs) < 2:
            return None
        s1, s2 = random.sample(structs, 2)
        question = question.replace("{struct_a}", s1["name"]).replace("{struct_b}", s2["name"])
        entity_name = f"{s1['name']}, {s2['name']}"
        evidence = f"{s1['file']}:{s1['line']}, {s2['file']}:{s2['line']}"
        answer = f"// {s1['name']} 和 {s2['name']} 是 llama.cpp 中的两个数据结构"

    # B5: {func1} + {func2} (可能还有 {feature})
    elif "{func1}" in question and "{func2}" in question:
        if len(funcs) < 2:
            return None
        f1, f2 = random.sample(funcs, 2)
        question = question.replace("{func1}", f1["name"]).replace("{func2}", f2["name"])
        if "{feature}" in question:
            question = question.replace("{feature}", feature)
        entity_name = f"{f1['name']}, {f2['name']}"
        evidence = f"{f1['file']}:{f1['line']}, {f2['file']}:{f2['line']}"
        answer = f"// {f1['name']} 和 {f2['name']} 是 llama.cpp 中的两个相关函数"

    # ========== C组：单占位符模板 ==========

    # C1: {func} only
    elif "{func}" in question and func:
        func_name = func["name"]
        question = question.replace("{func}", func_name)
        entity_name = func_name
        evidence = f"{func['file']}:{func['line']}"
        body = extract_function_body(func, funcs)
        answer = f"// {func_name} 定义于 {func['file']} 第 {func['line']} 行\n"
        if func["doc"]:
            answer += f"// 注释: {func['doc']}\n"
        answer += f"// 签名: {func_name}({func['params']})\n"
        answer += f"\n函数体片段:\n{body}"

    # C2: {struct} only
    elif "{struct}" in question and struct:
        struct_name = struct["name"]
        question = question.replace("{struct}", struct_name)
        entity_name = struct_name
        evidence = f"{struct['file']}:{struct['line']}"
        fields_str = "\n  ".join(struct["fields"][:8])
        answer = f"// {struct_name} 定义于 {struct['file']} 第 {struct['line']} 行\n"
        answer += f"// 字段:\n  {fields_str}"

    # C3: {module} only
    elif "{module}" in question:
        mod_name, mod_desc = module
        question = question.replace("{module}", mod_name)
        entity_name = mod_name
        evidence = f"{mod_name} 模块"
        answer = f"// {mod_name} 模块说明: {mod_desc}\n"
        files = entities["modules"].get(mod_name, [])[:5]
        if files:
            answer += f"// 关键文件: {', '.join(files)}"

    # C4: {feature} only
    elif "{feature}" in question:
        question = question.replace("{feature}", feature)
        entity_name = feature
        evidence = "llama.cpp 源码"
        answer = f"// {feature} 是 llama.cpp 中的一个功能特性\n"
        related = [f for f in funcs if len(feature) >= 4 and feature[:4].lower() in f["name"].lower()]
        if related:
            related_strs = [f"{r['name']}({r['file']}:{r['line']})" for r in related[:3]]
            answer += "// 相关函数: " + ", ".join(related_strs)

    # C5: {algorithm} only
    elif "{algorithm}" in question:
        question = question.replace("{algorithm}", algorithm)
        entity_name = algorithm
        evidence = "llama.cpp 源码"
        answer = f"// {algorithm} 是 llama.cpp 中使用的算法\n"

    # C6: {operation} only
    elif "{operation}" in question:
        question = question.replace("{operation}", operation)
        entity_name = operation
        evidence = "llama.cpp 源码"
        answer = f"// {operation} 是 llama.cpp 中的一个操作流程"

    # C7: {condition} only
    elif "{condition}" in question:
        question = question.replace("{condition}", condition)
        entity_name = condition
        evidence = "llama.cpp 源码"
        answer = f"// {condition} 是 llama.cpp 中需要处理的一种场景"

    # C8: {error_type} only
    elif "{error_type}" in question:
        question = question.replace("{error_type}", error_type)
        entity_name = error_type
        evidence = "llama.cpp 源码"
        answer = f"// {error_type} 是 llama.cpp 中可能遇到的一类错误"

    # C9: {interface} only
    elif "{interface}" in question and func:
        question = question.replace("{interface}", func["name"])
        entity_name = func["name"]
        evidence = f"{func['file']}:{func['line']}"
        answer = f"// {func['name']} 是 llama.cpp 的一个公共接口\n"
        if func["doc"]:
            answer += f"// 说明: {func['doc']}\n"
        answer += f"// 签名: {func['name']}({func['params']})"

    else:
        return None

    if not question or not answer or "{" in question:  # 确保所有占位符都被替换
        return None

    return {
        "一级分类": cat1,
        "二级分类": cat2,
        "问题类型": qtype,
        "意图": intent,
        "实体名称": entity_name,
        "具体问题": question,
        "答案": answer,
        "Evidence": evidence,
    }


def generate_qa(target_count: int = 100) -> list[dict]:
    """生成 QA 数据集"""

    print("扫描 llama.cpp 源码...")
    entities = get_all_entities()

    func_count = len(entities["functions"])
    struct_count = len(entities["structs"])
    print(f"  提取到 {func_count} 个函数, {struct_count} 个结构体")
    for mod, files in entities["modules"].items():
        print(f"  {mod}: {len(files)} 个文件")

    print("\n填充模板生成问题...")

    # 每个模板尝试多次填充（用不同随机种子），收集所有成功结果
    all_rows = []
    for template in TEMPLATE_MAP:
        template_rows = []
        for attempt in range(8):  # 每个模板最多8次尝试
            random.seed(42 + attempt * 100 + len(all_rows) + attempt)
            row = fill_template(template, entities)
            if row:
                template_rows.append(row)
        # 每个模板最多保留3个不同问题
        all_rows.extend(template_rows[:3])

    print(f"  生成 {len(all_rows)} 道候选题")

    # 按意图均衡
    by_intent = defaultdict(list)
    for r in all_rows:
        by_intent[r["意图"]].append(r)

    # 目标分布（尽量对齐原v3的比例）
    targets = {
        "System Design": 8,
        "Algorithm Implementation": 8,
        "API / Framework Support": 6,
        "Feature Location": 5,
        "Data / Control-flow": 6,
        "Identifier Location": 3,
        "Purpose Exploration": 5,
        "Design rationale": 5,
        "Performance": 4,
        "Concept / Definition": 15,
        "Dependency tracing": 10,
        "Architecture exploration": 10,
    }

    selected = []
    for intent, target in targets.items():
        pool = by_intent.get(intent, [])
        if pool:
            k = min(target, len(pool))
            selected.extend(random.sample(pool, k))

    # 补齐到目标数
    remaining = [r for r in all_rows if r not in selected]
    random.shuffle(remaining)
    while len(selected) < target_count and remaining:
        selected.append(remaining.pop())

    random.shuffle(selected)

    return selected[:target_count]


def main():
    print("=" * 60)
    print("Llama.cpp 源码 QA 生成器")
    print("=" * 60)

    rows = generate_qa(target_count=100)

    print(f"\n生成 {len(rows)} 道题")
    print("\n意图分布:")
    intent_counts = defaultdict(int)
    for r in rows:
        intent_counts[r["意图"]] += 1
    for intent, count in sorted(intent_counts.items(), key=lambda x: -x[1]):
        print(f"  {intent}: {count}")

    # 写入 CSV
    fieldnames = ["一级分类", "二级分类", "问题类型", "意图", "实体名称", "具体问题", "答案", "Evidence"]
    with open(OUTPUT, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n已保存到: {OUTPUT}")


if __name__ == "__main__":
    main()
