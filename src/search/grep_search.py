"""Grep搜索工具 - 基于关键词的代码搜索"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Dict

REPO_ROOT = Path("/data/yulin/RUC/llama.cpp")


def grep_codebase(
    keyword: str,
    codebase_path: str = None,
    limit: int = 5,
    context_lines: int = 3
) -> List[Dict]:
    """
    使用ripgrep在代码库中搜索关键词，返回带上下文的代码片段
    
    Args:
        keyword: 搜索关键词
        codebase_path: 代码库路径
        limit: 最大返回结果数
        context_lines: 上下文行数
        
    Returns:
        匹配结果列表，包含文件路径、行号和代码内容
    """
    if codebase_path is None:
        codebase_path = REPO_ROOT
    
    try:
        # 使用ripgrep搜索，带上下文
        cmd = [
            'rg', '-n', f'-C', str(context_lines),
            '--type-add', 'cpp:*.{c,cpp,h,hpp}',
            '-tcpp', '-i', keyword, str(codebase_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0 and not result.stdout:
            return []
        
        # 解析rg输出，分组匹配结果
        matches = []
        current_file = None
        current_lines = []
        
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            
            # 新文件匹配: file:line:content
            if ':' in line and not line.startswith(' ') and not line.startswith('--'):
                # 保存之前的匹配
                if current_file and current_lines and len(matches) < limit:
                    matches.append({
                        'file': current_file,
                        'lines': current_lines,
                        'score': 0.6
                    })
                
                parts = line.split(':', 2)
                if len(parts) >= 2:
                    try:
                        line_num = int(parts[1])
                        content = parts[2] if len(parts) > 2 else ""
                        current_file = parts[0]
                        current_lines = [{'line': line_num, 'content': content}]
                    except ValueError:
                        pass
            elif current_file and (line.startswith(' ') or line.startswith('-') or line.startswith('+')):
                # 上下文行
                content = line[1:].strip() if line[0] in '-+' else line.strip()
                current_lines.append({'line': None, 'content': content})
        
        # 保存最后一个匹配
        if current_file and current_lines and len(matches) < limit:
            matches.append({
                'file': current_file,
                'lines': current_lines,
                'score': 0.6
            })
        
        return matches
        
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return []

def extract_entities_from_question(question: str, use_llm: bool = True) -> List[str]:
    """
    从问题中提取可能的实体关键词
    
    Args:
        question: 问题文本
        use_llm: 是否使用LLM提取（否则用正则）
        
    Returns:
        关键词列表
    """
    import re
    
    if use_llm:
        try:
            from ..core.llm_client import call_llm_json
            
            prompt = f"""从以下问题中提取关键代码实体（函数名、类名、变量名等）。

问题: {question}

要求:
1. 只提取具体的标识符名称（如函数名、类名）
2. 如果问题问的是"函数xxx"，提取xxx
3. 如果问的是"模块yyy"，提取yyy
4. 最多返回3个最相关的实体

返回JSON格式:
{{"entities": ["entity1", "entity2", "entity3"]}}

只输出JSON:"""
            
            result = call_llm_json(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100
            )
            
            if result and "entities" in result:
                entities = result.get("entities", [])
                # 过滤太短或太泛的实体
                filtered = [e for e in entities if len(e) >= 3 and e.lower() not in [
                    "function", "class", "module", "variable", "code"
                ]]
                return filtered[:3]
        except Exception:
            pass  # 失败时回退到正则提取
    
    # 回退：用正则提取
    patterns = [
        r'\b[a-zA-Z_][a-zA-Z0-9_]*::[a-zA-Z_][a-zA-Z0-9_]*\b',
        r'\bggml_[a-z_]+\b',
        r'\bllama_[a-z_]+\b',
        r'\b[A-Z][a-z]+[A-Z][a-zA-Z]*\b',
    ]
    
    entities = []
    for pattern in patterns:
        matches = re.findall(pattern, question)
        entities.extend(matches)
    
    seen = set()
    filtered = []
    for e in entities:
        if len(e) >= 3 and e.lower() not in ['function', 'class', 'module', 'variable', 'code']:
            seen.add(e)
            filtered.append(e)
    
    return filtered[:3]


def search_module_functions(module_name: str, limit: int = 5) -> List[Dict]:
    """
    搜索模块相关的函数。
    当关键词是模块名（如 ggml-blas）时，尝试读取模块文件并提取函数定义。
    """
    import re
    from .code_reader import REPO_ROOT
    
    functions = []
    
    # 尝试查找模块的实现文件
    # 常见路径模式：module.cpp, module/module.cpp, src/module.cpp 等
    possible_paths = [
        f"ggml/src/{module_name}/{module_name}.cpp",
        f"ggml/src/{module_name.replace('-', '_')}.cpp",
        f"src/{module_name}.cpp",
        f"src/{module_name.replace('-', '_')}.cpp",
        f"{module_name}.cpp",
    ]
    
    for rel_path in possible_paths:
        full_path = REPO_ROOT / rel_path
        if full_path.exists():
            try:
                with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                
                # 提取函数定义（简单正则匹配）
                # 匹配模式：返回类型 函数名(参数) {
                func_pattern = r'(?:static\s+)?(?:\w+\s+)*(\w+)\s*\([^)]*\)\s*\{'
                matches = list(re.finditer(func_pattern, content))
                
                for i, match in enumerate(matches[:limit]):
                    func_name = match.group(1)
                    start_pos = max(0, match.start() - 100)
                    end_pos = min(len(content), match.end() + 500)
                    func_code = content[start_pos:end_pos]
                    
                    functions.append({
                        'name': func_name,
                        'file': rel_path,
                        'text': func_code,
                        'score': 0.7,
                        'source': 'module_file'
                    })
                
                if functions:
                    break
                    
            except Exception:
                continue
    
    return functions


def convert_grep_to_function_results(grep_results: List[Dict]) -> List[Dict]:
    """将grep结果转换为函数检索结果格式，并补充完整代码"""
    import re
    from .code_reader import enrich_function_with_code, read_function_from_file
    
    functions = []
    seen_files = set()  # 去重
    
    for result in grep_results:
        file_path = result.get('file', '')
        lines = result.get('lines', [])
        
        if not lines or file_path in seen_files:
            continue
        
        seen_files.add(file_path)
        
        # 找到匹配行（有行号的那行）
        match_line = None
        for l in lines:
            if l.get('line') is not None:
                match_line = l
                break
        
        if not match_line:
            continue
        
        # 尝试从匹配行提取函数名
        content = match_line.get('content', '')
        func_name = 'unknown'
        line_num = match_line.get('line', 1)
        
        # 匹配函数定义模式
        func_match = re.search(r'(?:\w+\s+)*(\w+)\s*\(', content)
        if func_match:
            candidate = func_match.group(1)
            # 过滤掉常见非函数名
            if candidate not in ['if', 'for', 'while', 'switch', 'return', 'sizeof']:
                func_name = candidate
        
        # 构建代码文本（包含所有上下文）
        code_lines = [l.get('content', '') for l in lines]
        code_text = '\n'.join(code_lines)
        
        func = {
            'name': func_name,
            'file': file_path,
            'text': code_text[:800],
            'score': result.get('score', 0.5),
            'source': 'grep_fallback',
            'match_line': line_num
        }
        
        # 尝试读取更多代码上下文（从匹配行前后）
        if func_name != 'unknown':
            # 尝试从文件读取完整函数
            full_code = read_function_from_file(
                file_path=file_path,
                func_name=func_name,
                max_lines=40
            )
            if full_code and len(full_code) > len(code_text):
                func['text'] = full_code
                func['code_enriched'] = True
        
        functions.append(func)
    
    return functions
