#!/usr/bin/env python3
"""
探索代码图谱的高层抽象方法。

策略：
1. 基于文件路径建立模块边界（ggml/, src/llama-*.cpp, common/, etc.）
2. 对每个模块，用函数名模式 + 签名做聚类
3. 用 LLM 总结每个模块的职责
4. 验证：看 QA 数据集能否被抽象回答
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collections import defaultdict
from neo4j_writer import get_driver
from config import NEO4J_DATABASE, LLM_MODEL
import json, re

# ── 1. 从 Neo4j 读取函数数据 ──────────────────────────────────────────
def load_functions():
    driver = get_driver()
    functions = []
    with driver.session(database=NEO4J_DATABASE) as s:
        result = s.run('''
            MATCH (f:Function)
            RETURN f.name as name, f.signature as sig, f.file_path as fp,
                   f.start_line as start_line, f.end_line as end_line
        ''')
        for r in result:
            functions.append({
                'name': r['name'],
                'sig': r['sig'] or '',
                'fp': r['fp'],
                'start': r['start_line'],
                'end': r['end_line'],
            })
    driver.close()
    return functions

# ── 2. 模块划分 ────────────────────────────────────────────────────────
def classify_module(fp):
    """根据文件路径划分顶级模块"""
    if fp.startswith('ggml/src/'):
        if 'ggml-cpu' in fp:
            return 'ggml-cpu'
        elif 'ggml-cuda' in fp:
            return 'ggml-cuda'
        elif 'ggml-vulkan' in fp:
            return 'ggml-vulkan'
        elif 'ggml-metal' in fp:
            return 'ggml-metal'
        elif 'ggml-opencl' in fp:
            return 'ggml-opencl'
        elif 'ggml-hip' in fp:
            return 'ggml-hip'
        return 'ggml-core'
    if fp.startswith('src/llama-'):
        return 'llama-model'
    if fp.startswith('src/'):
        return 'llama-util'
    if fp.startswith('common/'):
        return 'common'
    if fp.startswith('tests/'):
        return 'tests'
    if fp.startswith('tools/'):
        return 'tools'
    if fp.startswith('vendor/'):
        return 'vendor'
    if fp.startswith('examples/'):
        return 'examples'
    return 'other'

def classify_submodule(fp):
    """二级模块（在 src/ 下）"""
    if not fp.startswith('src/'):
        return None
    fname = fp.split('/')[-1]
    m = re.match(r'llama-([a-z]+)\.cpp', fname)
    if m:
        return m.group(1)
    return 'other'

# ── 3. 函数名聚类 ──────────────────────────────────────────────────────
def extract_function_family(name):
    """从函数名提取家族前缀"""
    # 常见前缀模式
    if name.startswith('ggml_'):
        return 'ggml_' + name.split('_')[1] if '_' in name[5:] else 'ggml_*'
    if name.startswith('llama_'):
        parts = name.split('_')
        if len(parts) >= 2:
            return 'llama_' + parts[1]
        return 'llama_*'
    if name.startswith('common_'):
        parts = name.split('_')
        if len(parts) >= 2:
            return 'common_' + parts[1]
        return 'common_*'
    return 'other'

def get_function_category(name, sig):
    """根据名字和签名推断函数类别"""
    name_lower = name.lower()
    sig_lower = sig.lower()

    # 初始化模式
    if any(x in name_lower for x in ['init', 'new', 'create', 'alloc']):
        return 'initialization'
    # 销毁模式
    if any(x in name_lower for x in ['free', 'destroy', 'release', 'delete', 'cleanup', 'clear']):
        return 'destruction'
    # 计算模式
    if any(x in name_lower for x in ['forward', 'compute', 'calc', 'run', 'exec', 'apply']):
        return 'computation'
    # 获取/设置模式
    if name_lower.startswith('get_') or 'get_' in name_lower:
        return 'getter'
    if name_lower.startswith('set_') or 'set_' in name_lower:
        return 'setter'
    # 采样模式
    if any(x in name_lower for x in ['sample', 'sampler', 'prob']):
        return 'sampling'
    # 量化模式
    if any(x in name_lower for x in ['quant', 'dequant', 'iq_']):
        return 'quantization'
    # IO模式
    if any(x in name_lower for x in ['load', 'save', 'read', 'write', 'parse', 'dump']):
        return 'io'
    return 'other'

# ── 4. 构建模块摘要 ────────────────────────────────────────────────────
def build_module_summary(functions):
    """对每个模块生成统计摘要"""
    # 按顶级模块分组
    by_module = defaultdict(list)
    for f in functions:
        mod = classify_module(f['fp'])
        by_module[mod].append(f)

    summaries = {}
    for mod, funcs in sorted(by_module.items(), key=lambda x: -len(x[1])):
        # 函数家族分布
        families = defaultdict(int)
        categories = defaultdict(int)
        for f in funcs:
            families[extract_function_family(f['name'])] += 1
            categories[get_function_category(f['name'], f['sig'])] += 1

        # 子模块分布（仅对 src）
        submods = defaultdict(int)
        for f in funcs:
            sm = classify_submodule(f['fp'])
            if sm:
                submods[sm] += 1

        # Top 函数（名字最长的一些代表不同功能）
        top_funcs = sorted(funcs, key=lambda x: len(x['name']), reverse=True)[:20]

        summaries[mod] = {
            'func_count': len(funcs),
            'top_families': dict(sorted(families.items(), key=lambda x: -x[1])[:10]),
            'categories': dict(sorted(categories.items(), key=lambda x: -x[1])),
            'submodules': dict(sorted(submods.items(), key=lambda x: -x[1])[:10]),
            'sample_funcs': [f['name'] for f in top_funcs[:10]],
        }
    return summaries

# ── 5. LLM 抽象总结（用已有的 Q/A 思路）────────────────────────────────
LLM_PROMPT_TEMPLATE = """你是一位 C++ 代码架构分析专家。以下是 llama.cpp 项目中一个模块的函数分布统计：

模块: {module}
函数数量: {func_count}

函数家族分布（部分）:
{family_dist}

函数类型分布:
{cat_dist}

子模块分布（部分）:
{submod_dist}

代表性函数名:
{sample_funcs}

请用 3-5 句话总结这个模块的职责和在整体架构中的角色。
"""

def generate_module_description(module, summary):
    """使用 LLM 生成模块描述"""
    if not summary:
        return "（无数据）"

    family_dist = '\n'.join([f'  - {k}: {v}' for k, v in list(summary.get('top_families', {}).items())[:8]])
    cat_dist = '\n'.join([f'  - {k}: {v}' for k, v in list(summary.get('categories', {}).items())[:6]])
    submod_dist = '\n'.join([f'  - {k}: {v}' for k, v in list(summary.get('submodules', {}).items())[:8]])
    sample_funcs = ', '.join(summary.get('sample_funcs', [])[:8])

    prompt = LLM_PROMPT_TEMPLATE.format(
        module=module,
        func_count=summary.get('func_count', 0),
        family_dist=family_dist or '（无）',
        cat_dist=cat_dist or '（无）',
        submod_dist=submod_dist or '（无）',
        sample_funcs=sample_funcs or '（无）',
    )
    return prompt

# ── 6. 验证：用 QA 数据集 ───────────────────────────────────────────────
def load_qa_data():
    """加载 QA 数据集"""
    import csv
    qa_path = Path(__file__).resolve().parent.parent.parent / 'llama_cpp_QA.csv'
    if not qa_path.exists():
        return []
    with open(qa_path) as f:
        reader = csv.DictReader(f)
        return list(reader)

def can_module_answer(qa_row, module_descs):
    """检查模块描述是否能帮助回答某个 QA"""
    # 简单的关键词匹配验证
    question = qa_row.get('具体问题', '') + qa_row.get('问题', '')
    entity = qa_row.get('实体名称', qa_row.get('实体', ''))

    # 如果问题中提到的关键词在某个模块的描述中更突出，返回该模块
    for mod, desc in module_descs.items():
        if entity.lower() in desc.lower():
            return mod
    return None

# ── 主流程 ─────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("llama.cpp 代码图谱高层抽象探索")
    print("=" * 60)

    # 加载函数
    print("\n[1/4] 加载函数数据...")
    functions = load_functions()
    print(f"  共 {len(functions)} 个函数")

    # 构建模块摘要
    print("\n[2/4] 分析模块结构...")
    summaries = build_module_summary(functions)

    # 打印模块统计
    print("\n模块摘要:")
    for mod, summ in summaries.items():
        print(f"\n  【{mod}】 {summ['func_count']} 个函数")
        top_fams = list(summ['top_families'].items())[:5]
        if top_fams:
            print(f"    主要家族: {', '.join([f'{k}({v})' for k,v in top_fams])}")
        cats = list(summ['categories'].items())[:4]
        if cats:
            print(f"    类型分布: {', '.join([f'{k}({v})' for k,v in cats])}")

    # 加载 QA 数据
    print("\n[3/4] 加载 QA 数据集进行验证...")
    qa_data = load_qa_data()
    print(f"  共 {len(qa_data)} 条 QA")

    # 简单打印一些 QA 样例
    if qa_data:
        print("\n  QA 样例:")
        for qa in qa_data[:3]:
            q = qa.get('具体问题', qa.get('问题', ''))[:60]
            e = qa.get('实体名称', qa.get('实体', ''))
            c = qa.get('一级分类', qa.get('类别', ''))
            print(f"    [{c}] {e}: {q}...")

    print("\n[4/4] 模块高层描述（基于函数名模式推断）:")
    print("-" * 60)

    module_descs = {}
    for mod, summ in summaries.items():
        desc = infer_module_description(mod, summ)
        module_descs[mod] = desc
        print(f"\n【{mod}】({summ['func_count']} 函数)")
        print(f"  {desc[:200]}...")

    return summaries, module_descs, qa_data


def infer_module_description(mod, summ):
    """基于统计数据推断模块描述（不调用 LLM 的简化版）"""
    func_count = summ.get('func_count', 0)
    top_families = summ.get('top_families', {})
    categories = summ.get('categories', {})
    submodules = summ.get('submodules', {})

    # 基于模块名和分布做启发式推断
    if mod == 'ggml-core':
        return f"GGML 核心张量运算库，提供矩阵乘法({top_families.get('ggml_matmul', '?')})、激活函数等底层计算。"
    elif mod == 'ggml-cpu':
        return f"GGML CPU 后端实现，提供基于 AVX/NEON 等指令集的量化张量运算。"
    elif mod == 'llama-model':
        sub = list(submodules.keys())[:5]
        return f"LLama 模型核心实现，包含采样器({submodules.get('sampler','?')})、上下文({submodules.get('context','?')})、词汇表({submodules.get('vocab','?')})等模块。"
    elif mod == 'llama-util':
        return f"LLama 辅助工具模块，提供图结构、参数管理、适配器等支持功能。"
    elif mod == 'common':
        return f"通用工具库，包含参数解析({top_families.get('common_arg', '?')})、日志、控制台、采样等跨模块复用功能。"
    elif mod == 'tools':
        return f"命令行工具集，包含 server({submodules.get('server','?')})、benchmark、perplexity 等评估工具。"
    elif mod == 'tests':
        return f"测试套件，覆盖后端算子、采样、量化、tokenizer 等功能模块。"
    elif mod == 'vendor':
        return f"第三方依赖，主要是 HTTP 库(httplib)。"
    elif mod == 'examples':
        return f"示例程序，展示 llama.cpp 的各种使用方式。"
    else:
        return f"包含 {func_count} 个函数，主要涉及 {list(top_families.keys())[:3]} 等操作。"


if __name__ == '__main__':
    summaries, module_descs, qa_data = main()