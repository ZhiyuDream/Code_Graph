"""代码读取工具 - 从源文件读取函数定义"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Dict

# 代码库根目录
REPO_ROOT = Path("/data/yulin/RUC/llama.cpp")


def read_function_from_file(
    file_path: str,
    func_name: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    max_lines: int = 50
) -> str:
    """
    从源文件读取函数代码
    
    Args:
        file_path: 文件路径（相对或绝对）
        func_name: 函数名
        start_line: 起始行号（可选）
        end_line: 结束行号（可选）
        max_lines: 最大读取行数
        
    Returns:
        函数代码文本
    """
    # 构建完整路径
    if not file_path.startswith('/'):
        full_path = REPO_ROOT / file_path
    else:
        full_path = Path(file_path)
    
    if not full_path.exists():
        return f"// 文件不存在: {file_path}"
    
    try:
        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except Exception as e:
        return f"// 读取文件失败: {e}"
    
    if not lines:
        return "// 空文件"
    
    # 如果提供了行号范围，直接返回该范围
    if start_line is not None and end_line is not None:
        start_idx = max(0, start_line - 1)
        end_idx = min(len(lines), end_line)
        code = ''.join(lines[start_idx:end_idx])
        return code.strip()
    
    # 否则，尝试通过函数名定位
    return _extract_function_by_name(lines, func_name, max_lines)


def _extract_function_by_name(lines: list, func_name: str, max_lines: int = 50) -> str:
    """通过函数名从文件中提取函数代码"""
    
    # C++ 函数定义模式
    # 匹配函数名后跟左括号，前面可能有返回类型、命名空间等
    patterns = [
        # 标准函数定义: void funcName(
        rf'(?:^|\n)\s*(?:\w+\s+)*?\s*{re.escape(func_name)}\s*\(',
        # 构造函数: Class::funcName(
        rf'{re.escape(func_name)}\s*\(',
        # 模板函数: template<...> void funcName(
        rf'template<[^>]+>\s*(?:\w+\s+)*?{re.escape(func_name)}\s*\(',
    ]
    
    # 找到函数定义的起始行
    start_line_idx = -1
    for i, line in enumerate(lines):
        for pattern in patterns:
            if re.search(pattern, line):
                start_line_idx = i
                break
        if start_line_idx >= 0:
            break
    
    if start_line_idx < 0:
        # 没找到函数定义，返回文件前 max_lines 行作为上下文
        return ''.join(lines[:min(max_lines, len(lines))]).strip()
    
    # 向前回溯，找到函数签名开始的位置（处理多行签名）
    signature_start = start_line_idx
    for i in range(start_line_idx - 1, max(-1, start_line_idx - 10), -1):
        line = lines[i].strip()
        # 如果遇到空行、注释行、预处理指令、其他语句，停止回溯
        if not line or line.startswith('//') or line.startswith('#') or line.endswith(';'):
            break
        # 如果行尾有 ) 或 ,，可能是参数列表的一部分
        if line.endswith(')') or line.endswith(',') or '{' in line:
            signature_start = i
            break
        signature_start = i
    
    # 向后查找函数体的结束位置
    end_line_idx = len(lines)
    brace_count = 0
    in_function = False
    
    for i in range(start_line_idx, min(len(lines), start_line_idx + max_lines)):
        line = lines[i]
        
        # 计算大括号
        for char in line:
            if char == '{':
                brace_count += 1
                in_function = True
            elif char == '}':
                brace_count -= 1
                if in_function and brace_count == 0:
                    end_line_idx = i + 1
                    break
        
        if in_function and brace_count == 0:
            break
    
    # 如果一直没找到结束，限制行数
    if end_line_idx > start_line_idx + max_lines:
        end_line_idx = start_line_idx + max_lines
    
    # 提取代码
    code_lines = lines[signature_start:end_line_idx]
    return ''.join(code_lines).strip()


def enrich_function_with_code(func: Dict) -> Dict:
    """
    为函数信息补充完整代码
    
    Args:
        func: 包含 name, file, start_line, end_line 的字典
        
    Returns:
        补充了 code 字段的字典
    """
    if not func:
        return func
    
    file_path = func.get('file', '')
    func_name = func.get('name', '')
    start_line = func.get('start_line')
    end_line = func.get('end_line')
    
    # 如果已经有文本且足够长，直接使用
    existing_text = func.get('text', '')
    if existing_text and len(existing_text) > 200:
        return func
    
    # 从文件读取代码
    code = read_function_from_file(
        file_path=file_path,
        func_name=func_name,
        start_line=start_line,
        end_line=end_line,
        max_lines=30
    )
    
    if code and not code.startswith('//'):
        func['text'] = code
        func['code_enriched'] = True
    
    return func


def batch_enrich_functions(functions: list) -> list:
    """批量为函数列表补充代码"""
    return [enrich_function_with_code(f) for f in functions]
