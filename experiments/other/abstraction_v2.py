#!/usr/bin/env python3
"""
代码图谱高层抽象框架 v2

目标：
1. 基于文件路径建立模块边界（ggml/src/, src/llama-*.cpp, common/, etc.）
2. 对每个模块，用函数名模式做子模块聚类
3. 识别 llama.cpp 架构层次（LLM 推理流程）
4. 用 LLM 总结每个模块的职责
5. 验证：建立"问题类型 → 相关模块"的映射
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collections import defaultdict
from neo4j_writer import get_driver
from config import NEO4J_DATABASE
import csv, re

# ─────────────────────────────────────────────────────────────────────
# 1. 数据加载
# ─────────────────────────────────────────────────────────────────────
def load_functions():
    driver = get_driver()
    functions = []
    with driver.session(database=NEO4J_DATABASE) as s:
        result = s.run('''
            MATCH (f:Function)
            RETURN f.name as name, f.signature as sig, f.file_path as fp,
                   f.fan_in as fan_in, f.fan_out as fan_out,
                   f.start_line as start_line, f.end_line as end_line
        ''')
        for r in result:
            functions.append({
                'name': r['name'],
                'sig': r['sig'] or '',
                'fp': r['fp'],
                'fan_in': r['fan_in'],
                'fan_out': r['fan_out'],
            })
    driver.close()
    return functions

def load_qa():
    qa_path = Path(__file__).resolve().parent.parent.parent / 'llama_cpp_QA.csv'
    if not qa_path.exists():
        return []
    with open(qa_path) as f:
        return list(csv.DictReader(f))

# ─────────────────────────────────────────────────────────────────────
# 2. 模块划分
# ─────────────────────────────────────────────────────────────────────
def classify_tier1(fp):
    """一级模块"""
    if fp.startswith('ggml/src/ggml-cpu/'):
        return 'ggml-cpu'
    if fp.startswith('ggml/src/ggml-cuda/'):
        return 'ggml-cuda'
    if fp.startswith('ggml/src/ggml-metal/'):
        return 'ggml-metal'
    if fp.startswith('ggml/src/ggml-vulkan/'):
        return 'ggml-vulkan'
    if fp.startswith('ggml/src/ggml-opencl/'):
        return 'ggml-opencl'
    if fp.startswith('ggml/src/ggml-hip/'):
        return 'ggml-hip'
    if fp.startswith('ggml/src/'):
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
    if fp.startswith('examples/'):
        return 'examples'
    if fp.startswith('vendor/'):
        return 'vendor'
    return 'other'

def classify_tier2(fp, name):
    """二级模块/子模块（对 llama-model 进一步细分）"""
    if not fp.startswith('src/llama-'):
        return None
    fname = fp.split('/')[-1]
    m = re.match(r'llama-([a-z\-]+)\.cpp', fname)
    if m:
        return m.group(1)
    # 按函数名细分
    if name.startswith('llama_sampler_'):
        return 'sampler'
    if name.startswith('llama_model_'):
        return 'model'
    if name.startswith('llama_vocab_'):
        return 'vocab'
    if name.startswith('llama_kv_cache_') or name.startswith('llama_kv_'):
        return 'kv-cache'
    if name.startswith('llama_context_') or name.startswith('llama_ctx_'):
        return 'context'
    if name.startswith('llama_batch_'):
        return 'batch'
    if name.startswith('llama_memory_'):
        return 'memory'
    return 'other'

# ─────────────────────────────────────────────────────────────────────
# 3. 函数语义聚类
# ─────────────────────────────────────────────────────────────────────
PATTERNS = {
    'init':      r'_(init|new|create|alloc)\b',
    'free':      r'_(free|destroy|release|delete|cleanup|clear)\b',
    'forward':   r'_(forward|compute|calc|run|exec|apply)\b',
    'get':       r'\bget_|_\bget\b',
    'set':       r'\bset_|_\bset\b',
    'sample':    r'_sample|_sampler|_prob',
    'quant':     r'_quant|_dequant|_iq_',
    'io':        r'_load|_save|_read|_write|_parse|_dump|_encode|_decode',
    'graph':     r'_graph|_node|_edge',
    'backend':   r'_backend|_device|_dev_',
    'mem':       r'_mem|_alloc|_buffer|_pool',
    'sync':      r'_sync|_barrier|_fence',
    'thread':    r'_thread|_parallel|_worker',
    'token':     r'_token|_tok_',
    'kv':        r'_kv_|_kv_cache',
}

def get_semantic_tags(name):
    tags = []
    name_lower = name.lower()
    for tag, pattern in PATTERNS.items():
        if re.search(pattern, name_lower):
            tags.append(tag)
    return tags if tags else ['other']

# ─────────────────────────────────────────────────────────────────────
# 4. 模块摘要生成
# ─────────────────────────────────────────────────────────────────────
def build_module_inventory(functions):
    """为每个模块构建详细的函数清单和统计"""
    by_t1 = defaultdict(list)
    for f in functions:
        t1 = classify_tier1(f['fp'])
        by_t1[t1].append(f)

    inventory = {}
    for t1, funcs in by_t1.items():
        # 二级分类（仅 llama-model）
        by_t2 = defaultdict(list)
        if t1 == 'llama-model':
            for f in funcs:
                t2 = classify_tier2(f['fp'], f['name'])
                by_t2[t2].append(f)
        else:
            by_t2 = {'_all': funcs}

        # 语义标签统计
        tag_counts = defaultdict(int)
        for f in funcs:
            for tag in get_semantic_tags(f['name']):
                tag_counts[tag] += 1

        # 家族前缀统计
        families = defaultdict(int)
        for f in funcs:
            prefix = f['name'].split('_')[0] + '_' + f['name'].split('_')[1] if '_' in f['name'] else f['name'].split('_')[0]
            families[prefix] += 1

        inventory[t1] = {
            'count': len(funcs),
            'by_t2': {k: len(v) for k, v in by_t2.items()},
            't2_funcs': by_t2,
            'tag_counts': dict(sorted(tag_counts.items(), key=lambda x: -x[1])),
            'families': dict(sorted(families.items(), key=lambda x: -x[1])[:15]),
            'sample_funcs': [f['name'] for f in sorted(funcs, key=lambda x: len(x['name']), reverse=True)[:15]],
        }
    return inventory

# ─────────────────────────────────────────────────────────────────────
# 5. 架构层次推断（基于 llam.cpp 推理流程）
# ─────────────────────────────────────────────────────────────────────
ARCH_LAYERS = [
    {
        'layer': 0, 'name': '输入处理',
        'description': '原始输入（prompt、tokens）进入系统',
        'modules': ['llama-util'],  # batch, token 处理
    },
    {
        'layer': 1, 'name': '模型输入编码',
        'description': 'Tokenization、Embedding、位置编码',
        'modules': ['llama-model'],  # vocab, tokenize
    },
    {
        'layer': 2, 'name': '模型计算',
        'description': 'GGML 张量运算、矩阵乘法、Attention、FFN',
        'modules': ['ggml-core', 'ggml-cpu', 'llama-model'],  # ggml_* forward, llama_forward
    },
    {
        'layer': 3, 'name': 'KV Cache 管理',
        'description': 'Key-Value 缓存维护和复用',
        'modules': ['llama-model'],  # kv-cache
    },
    {
        'layer': 4, 'name': '采样输出',
        'description': 'Logits 后处理、采样、语法约束',
        'modules': ['llama-model'],  # sampler, grammar
    },
    {
        'layer': 5, 'name': '工具/应用层',
        'description': 'Server、CLI、评估工具',
        'modules': ['tools', 'examples'],
    },
]

# ─────────────────────────────────────────────────────────────────────
# 6. LLM 模块描述生成（简化版，无 API 调用）
# ─────────────────────────────────────────────────────────────────────
MODULE_DESCRIPTIONS = {
    'ggml-core': '''GGML 核心张量运算库，实现:
  - 张量创建和视图 (ggml_new_tensor, ggml_dup_tensor, ggml_view_tensor)
  - 矩阵乘法 (ggml_mul_mat, ggml_gemm)
  - 激活函数 (ggml_silu, ggml_gelu, ggml_relu)
  - Softmax、LayerNorm、RMSNorm
  - 量化支持 (ggml_quantize, ggml_dequantize)
  架构位置：所有模型计算的基础，所有后端都依赖此核心''',
    'ggml-cpu': '''GGML CPU 后端，实现:
  - AVX/AVX2/AVX512/NEON/VSX 等 SIMD 指令集支持
  - 量化整数运算 (GGML_TYPE_Q4_0, Q5_0, Q8_0 等)
  - 向量运算 (ggml_vec_*, ggml_compute_*)
  架构位置：ggml-core 的 CPU 执行后端''',
    'llama-model': '''LLama 模型核心实现，包含多个子模块:
  - llama-sampler: 采样逻辑（greedy, top-p, top-k, temp）
  - llama-vocab: 词表、tokenizer
  - llama-context: 推理上下文、KV cache 管理
  - llama-model: 模型权重加载、结构定义
  - llama-batch: 批处理逻辑
  - llama-kv-cache: KV cache 实现
  - llama-memory: 内存管理策略
  - llama-grammar: 语法约束解码
  架构位置：直接调用 GGML 执行张量运算''',
    'llama-util': '''LLama 辅助工具，包含:
  - llama-graph: 计算图结构
  - llama-arch: 模型架构定义
  - llama-params/llama-cparams: 参数管理
  - llama-adapter: 适配器（LoRA 等）
  - llama-io: 模型 I/O (llama_model_load, llama_model_save)
  架构位置：支持 llama-model 的基础设施''',
    'common': '''通用工具库，跨模块复用:
  - common/arg.cpp: 命令行参数解析
  - common/sampling.cpp: 独立采样器
  - common/chat.cpp: Chat 格式处理
  - common/console.cpp: 终端交互
  - common/log.cpp: 日志系统
  - common/unicode.cpp: Unicode 处理
  - common/chat-peg-parser.cpp: Chat 模板解析
  架构位置：独立工具层，不被模型直接依赖''',
    'tools': '''命令行工具集:
  - server/: HTTP API 服务器
  - perplexity/: 困惑度评估
  - llama-bench/: 性能基准测试
  - mtmd/: 多模态支持
  - tokenizer/: 分词工具
  架构位置：应用层工具，基于 llama.cpp 库构建''',
    'tests': '测试套件，覆盖 GGML 后端、采样、量化、tokenizer 等功能',
    'vendor': '第三方依赖，主要为 cpp-httplib HTTP 库',
    'examples': '示例程序，展示 llama.cpp 各功能的使用方式',
}

# ─────────────────────────────────────────────────────────────────────
# 7. QA 验证：建立"问题类型 → 模块"映射
# ─────────────────────────────────────────────────────────────────────
def build_qa_module_mapping(qa_data, inventory):
    """分析 QA 问题，映射到相关模块"""
    mappings = defaultdict(list)

    # 关键词到模块的映射
    keyword_map = {
        'ggml': ['ggml-core', 'ggml-cpu'],
        'tensor': ['ggml-core'],
        'quantiz': ['ggml-core', 'ggml-cpu'],
        'matrix': ['ggml-core'],
        'attention': ['llama-model'],
        'sampler': ['llama-model'],
        'sample': ['llama-model'],
        'token': ['llama-model'],
        'vocab': ['llama-model'],
        'kv cache': ['llama-model'],
        'memory': ['llama-model'],
        'context': ['llama-model'],
        'model load': ['llama-util'],
        'grammar': ['llama-model'],
        'chat': ['common'],
        'server': ['tools'],
        'perplexity': ['tools'],
        'bench': ['tools'],
        'init': ['common', 'llama-util'],
        'free': ['common', 'llama-util'],
    }

    for qa in qa_data:
        q = (qa.get('具体问题', '') + ' ' + qa.get('意图', '') + ' ' + qa.get('实体名称', '')).lower()
        cats = qa.get('一级分类', '')
        matched_mods = set()

        for kw, mods in keyword_map.items():
            if kw in q:
                matched_mods.update(mods)

        if matched_mods:
            for mod in matched_mods:
                mappings[mod].append(qa)

    return {mod: len(qas) for mod, qas in mappings.items()}

# ─────────────────────────────────────────────────────────────────────
# 8. 输出报告
# ─────────────────────────────────────────────────────────────────────
def print_report(inventory, qa_data):
    print("=" * 70)
    print(" llama.cpp 代码图谱高层抽象分析报告")
    print("=" * 70)

    # 统计
    total_funcs = sum(inv['count'] for inv in inventory.values())
    print(f"\n总计: {total_funcs} 个函数，分布在 {len(inventory)} 个顶级模块\n")

    # 模块详情
    print("-" * 70)
    print("【模块分层架构】")
    print("-" * 70)

    for mod in sorted(inventory.keys(), key=lambda x: -inventory[x]['count']):
        inv = inventory[mod]
        desc = MODULE_DESCRIPTIONS.get(mod, '（无描述）')

        print(f"\n┌─ {mod:15s} ({inv['count']} 函数)")
        print(f"│")
        print(f"│  {desc[:100]}...")

        # 二级分类
        if inv['by_t2'] and '_all' not in inv['by_t2']:
            print(f"│")
            print(f"│  子模块分布:")
            for t2, cnt in sorted(inv['by_t2'].items(), key=lambda x: -x[1]):
                print(f"│    {t2:20s}: {cnt:4d} 函数")

        # 语义标签
        tags = list(inv['tag_counts'].items())[:6]
        if tags:
            print(f"│")
            print(f"│  语义标签: {', '.join([f'{k}({v})' for k,v in tags])}")

        # 家族前缀
        fams = list(inv['families'].items())[:6]
        if fams:
            print(f"│  主要前缀: {', '.join([f'{k}({v})' for k,v in fams])}")

        # 代表函数
        samples = inv['sample_funcs'][:5]
        print(f"│  代表函数: {', '.join(samples)}")
        print(f"└")

    # 架构层次
    print("\n" + "-" * 70)
    print("【架构层次视图】")
    print("-" * 70)
    for layer in ARCH_LAYERS:
        mods = ', '.join(layer['modules'])
        print(f"  Layer {layer['layer']}: {layer['name']:15s} | {mods}")
        print(f"              {layer['description']}")

    # QA 映射
    print("\n" + "-" * 70)
    print("【QA 数据覆盖分析】")
    print("-" * 70)
    qa_mapping = build_qa_module_mapping(qa_data, inventory)
    total_qa = len(qa_data)

    for mod, cnt in sorted(qa_mapping.items(), key=lambda x: -x[1]):
        pct = cnt * 100 / total_qa if total_qa else 0
        bar = '█' * int(pct / 2)
        print(f"  {mod:15s}: {cnt:3d} ({pct:5.1f}%) {bar}")

    print(f"\n  总计 QA: {total_qa}")

# ─────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────
def main():
    print("加载数据...")
    functions = load_functions()
    qa_data = load_qa()

    print(f"函数: {len(functions)}, QA: {len(qa_data)}")

    print("\n构建模块清单...")
    inventory = build_module_inventory(functions)

    print_report(inventory, qa_data)

    # 输出结构化数据供后续使用
    import json
    output = {
        'inventory': {k: {kk: vv for kk, vv in v.items() if kk != 't2_funcs'}
                      for k, v in inventory.items()},
        'module_descriptions': MODULE_DESCRIPTIONS,
        'arch_layers': ARCH_LAYERS,
    }
    out_path = Path(__file__).parent / 'abstraction_output.json'
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"\n结构化输出已保存到: {out_path}")

if __name__ == '__main__':
    main()