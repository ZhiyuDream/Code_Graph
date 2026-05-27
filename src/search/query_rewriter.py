"""Query改写器 - 将自然语言问题转换为适合代码搜索的查询"""
from __future__ import annotations

import re
from typing import List, Dict, Set
from dataclasses import dataclass


@dataclass
class RewrittenQuery:
    """改写后的查询"""
    original: str
    translated: str
    keywords: List[str]
    identifiers: List[str]
    search_queries: List[str]
    entity_types: Dict[str, str]


class QueryRewriter:
    """
    智能Query改写器
    
    功能:
    1. 中文→英文代码术语翻译
    2. 缩写/别名扩展
    3. 标识符提取（函数名、类名、变量名）
    4. 多策略搜索query生成
    """
    
    # llama.cpp 专用术语词典（可扩展）
    TERM_DICT = {
        # 核心概念
        "矩阵乘法": ["mat_mul", "matrix multiplication", "gemm", "MMQ", "MUL_MAT", "mul_mat"],
        "矩阵": ["matrix", "mat", "tensor", "ggml_tensor"],
        "向量": ["vector", "vec"],
        "张量": ["tensor", "ggml_tensor"],
        
        # 模型组件
        "注意力": ["attention", "attn", "flash_attn", "flash_attention", "multi_head_attention"],
        "自注意力": ["self_attention", "self_attn"],
        "前馈": ["feed_forward", "ffn", "mlp"],
        "归一化": ["normalize", "norm", "layer_norm", "rms_norm", "batch_norm"],
        "激活函数": ["activation", "gelu", "relu", "silu", "softmax", "sigmoid"],
        "嵌入": ["embedding", "embed"],
        "位置编码": ["positional encoding", "pos_emb", "rope"],
        
        # 内存管理
        "缓存": ["cache", "buffer", "kv_cache", "key_value_cache", "attn_cache"],
        "KV缓存": ["kv_cache", "key_value_cache", "KV cache"],
        "内存分配": ["malloc", "alloc", "allocate", "ggml_alloc", "allocator"],
        "上下文": ["context", "ctx", "ggml_context", "llama_context"],
        
        # 计算图
        "图计算": ["graph", "graph_compute", "ggml_graph", "compute_graph"],
        "计算图": ["compute_graph", "ggml_graph", "graph"],
        "节点": ["node", "tensor", "op", "operation"],
        "边": ["edge", "connection", "dependency"],
        
        # 后端与硬件
        "后端": ["backend", "ggml_backend", "device"],
        "CPU": ["cpu", "ggml_cpu", "x86", "arm", "neon", "avx"],
        "GPU": ["gpu", "cuda", "rocm", "vulkan", "metal", "sycl"],
        "量化": ["quantize", "quantization", "quant", "Q4", "Q8", "Q2", "IQ4", "GGML_TYPE"],
        "反量化": ["dequantize", "dequant"],
        
        # 模型加载与推理
        "模型加载": ["model_load", "llama_model_load", "load_model", "load_checkpoint"],
        "推理": ["inference", "eval", "evaluate", "forward", "predict"],
        "生成": ["generate", "sampling", "sample", "decode"],
        "分词": ["tokenize", "tokenizer", "token", "vocab", "bpe", "sentencepiece"],
        "词表": ["vocab", "vocabulary", "token", "dictionary"],
        
        # 语法与解析
        "语法": ["grammar", "grammar_element", "parse", "parser"],
        "解析": ["parse", "parser", "parsing", "decode"],
        "约束": ["constraint", "rule", "grammar_rule"],
        
        # 性能优化
        "优化": ["optimize", "optimization", "perf", "performance"],
        "并行": ["parallel", "concurrent", "multi_thread", "thread", "threadpool", "openmp"],
        "异步": ["async", "asynchronous", "non_blocking"],
        "融合": ["fuse", "fused", "fusion", "kernel_fusion"],
        "向量化": ["vectorize", "simd", "avx", "sse", "neon", "sve"],
        
        # 文件与IO
        "文件": ["file", "io", "read", "write", "load", "save"],
        "模型文件": ["gguf", "model_file", "checkpoint", "safetensors"],
        
        # 状态与配置
        "状态": ["state", "status", "mode", "flags"],
        "配置": ["config", "configuration", "param", "parameter", "settings"],
        "超参数": ["hyperparameter", "hparams", "param"],
        
        # 常见缩写
        "批次": ["batch", "batch_size", "bs", "b"],
        "序列": ["sequence", "seq", "seq_len", "n_seq"],
        "维度": ["dimension", "dim", "n_dim", "shape"],
        "通道": ["channel", "ch", "n_channels"],
        "头": ["head", "n_head", "num_heads", "multi_head"],
        "层": ["layer", "n_layer", "num_layers", "depth"],
        "步": ["step", "iteration", "iter", "epoch"],
    }
    
    # 常见代码标识符模式
    IDENTIFIER_PATTERNS = [
        r'\bggml_[a-z_][a-z0-9_]*\b',      # ggml_xxx
        r'\bllama_[a-z_][a-z0-9_]*\b',     # llama_xxx
        r'\bGGML_[A-Z_][A-Z0-9_]*\b',      # GGML_XXX (宏)
        r'\bLLAMA_[A-Z_][A-Z0-9_]*\b',     # LLAMA_XXX (宏)
        r'\b[A-Z][a-z]+[A-Z][a-zA-Z0-9_]*\b',  # CamelCase (类名)
        r'\b[a-z_][a-z0-9_]*_t\b',          # xxx_t (类型)
        r'\b[A-Z]{2,}_\w+\b',               # 缩写开头 (如 CUDA_XXX)
    ]
    
    def __init__(self, use_llm: bool = True):
        self.use_llm = use_llm
        self.term_dict = self.TERM_DICT
        
    def rewrite(self, question: str) -> RewrittenQuery:
        """
        改写问题为适合代码搜索的查询
        
        Args:
            question: 原始问题
            
        Returns:
            RewrittenQuery: 改写后的查询对象
        """
        # Step 1: 提取原始标识符
        identifiers = self._extract_identifiers(question)
        
        # Step 2: 中文术语翻译
        translated_terms = self._translate_terms(question)
        
        # Step 3: 生成搜索关键词
        keywords = self._generate_keywords(question, identifiers, translated_terms)
        
        # Step 4: 生成最终搜索query
        search_queries = self._construct_search_queries(keywords, identifiers)
        
        # Step 5: 识别实体类型
        entity_types = self._classify_entities(identifiers)
        
        return RewrittenQuery(
            original=question,
            translated=" ".join(translated_terms),
            keywords=keywords,
            identifiers=identifiers,
            search_queries=search_queries,
            entity_types=entity_types
        )
    
    def _extract_identifiers(self, text: str) -> List[str]:
        """从文本中提取代码标识符"""
        identifiers = []
        
        # 使用正则模式匹配
        for pattern in self.IDENTIFIER_PATTERNS:
            matches = re.findall(pattern, text, re.IGNORECASE)
            identifiers.extend(matches)
        
        # 去重并保持顺序
        seen = set()
        unique_identifiers = []
        for ident in identifiers:
            ident_lower = ident.lower()
            if ident_lower not in seen and len(ident) >= 3:
                seen.add(ident_lower)
                unique_identifiers.append(ident)
        
        return unique_identifiers[:10]  # 最多10个
    
    def _translate_terms(self, text: str) -> List[str]:
        """将中文术语翻译为英文代码术语"""
        translated = []
        
        # 精确匹配
        for cn_term, en_terms in self.term_dict.items():
            if cn_term in text:
                translated.extend(en_terms)
        
        # 模糊匹配（简化实现）
        # TODO: 可以添加更复杂的模糊匹配逻辑
        
        # 去重
        return list(dict.fromkeys(translated))
    
    def _generate_keywords(
        self, 
        question: str, 
        identifiers: List[str], 
        translated_terms: List[str]
    ) -> List[str]:
        """生成搜索关键词列表"""
        keywords = []
        
        # 1. 原始标识符优先级最高（最可信）
        for ident in identifiers:
            if len(ident) >= 3:
                keywords.append(ident)
        
        # 2. 翻译的术语
        for term in translated_terms:
            if len(term) >= 3 and ' ' not in term:  # 优先单词条
                keywords.append(term)
        
        # 3. 提取问题中的潜在缩写（全大写）
        words = re.findall(r'\b[A-Z]{2,}\b', question)
        for w in words:
            if len(w) >= 2:
                keywords.append(w)
        
        # 4. 从翻译术语中提取单个词（如"matrix multiplication" → "matrix"）
        for term in translated_terms:
            if ' ' in term:
                parts = term.split()
                for part in parts:
                    if len(part) >= 3 and part not in keywords:
                        keywords.append(part)
        
        # 去重和过滤
        seen = set()
        filtered = []
        for kw in keywords:
            kw_lower = kw.lower()
            # 过滤噪声：避免 _t 后缀、ggml_前缀的虚构组合
            if kw_lower in seen:
                continue
            if len(kw) < 3:
                continue
            # 避免生成 xxx_t, ggml_xxx 等可能不存在的组合
            if kw.endswith('_t') and kw[:-2] in [id.lower() for id in identifiers]:
                # 只有当原始标识符本身就带 _t 才保留
                continue
            if kw.startswith('ggml_') and kw[5:] in [id.lower() for id in identifiers]:
                # 避免为普通词自动添加 ggml_ 前缀
                continue
            seen.add(kw_lower)
            filtered.append(kw)
        
        return filtered[:12]  # 最多12个关键词
    
    def _construct_search_queries(
        self, 
        keywords: List[str], 
        identifiers: List[str]
    ) -> List[str]:
        """构造多个搜索query"""
        queries = []
        
        # 1. 精确标识符搜索（最高优先级）
        for ident in identifiers[:3]:
            queries.append(ident)
        
        # 2. 关键词组合搜索
        if keywords:
            # 前3个关键词组合
            queries.append(" ".join(keywords[:3]))
            
            # 标识符+翻译词组合
            for ident in identifiers[:2]:
                for kw in keywords[:3]:
                    if ident.lower() != kw.lower():
                        queries.append(f"{ident} {kw}")
        
        # 去重
        return list(dict.fromkeys(queries))[:8]
    
    def _classify_entities(self, identifiers: List[str]) -> Dict[str, str]:
        """分类标识符类型"""
        entity_types = {}
        
        for ident in identifiers:
            if ident.startswith('ggml_'):
                entity_types[ident] = 'ggml_function'
            elif ident.startswith('llama_'):
                entity_types[ident] = 'llama_function'
            elif ident.startswith('GGML_') or ident.startswith('LLAMA_'):
                entity_types[ident] = 'macro'
            elif ident.endswith('_t'):
                entity_types[ident] = 'type'
            elif re.match(r'^[A-Z][a-z]+[A-Z]', ident):
                entity_types[ident] = 'class'
            else:
                entity_types[ident] = 'unknown'
        
        return entity_types
    
    def get_grep_keywords(self, question: str) -> List[str]:
        """
        获取适合Grep搜索的关键词（简化接口）
        
        Args:
            question: 原始问题
            
        Returns:
            List[str]: 关键词列表
        """
        rewritten = self.rewrite(question)
        
        # 优先返回标识符，其次是翻译的关键词
        keywords = []
        keywords.extend(rewritten.identifiers)
        keywords.extend(rewritten.keywords)
        
        # 去重
        seen = set()
        result = []
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower not in seen:
                seen.add(kw_lower)
                result.append(kw)
        
        return result[:10]


class LLMQueryRewriter(QueryRewriter):
    """使用LLM增强的Query改写器"""
    
    def __init__(self):
        super().__init__(use_llm=True)
        self.llm_prompt = """
你是一个代码搜索专家。请将用户的问题转换为适合在llama.cpp代码库中搜索的关键词。

llama.cpp代码库的特点：
- 使用C/C++编写
- 函数命名规范：ggml_xxx, llama_xxx
- 宏命名规范：GGML_XXX, LLAMA_XXX
- 常见缩写：mat_mul(矩阵乘法), kv_cache(KV缓存), quant(量化)

改写要求：
1. 将中文概念翻译成对应的英文代码标识符
2. 提取可能的函数名、类名、宏名
3. 补充常见的缩写和变体
4. 返回最可能出现在代码中的关键词

示例：
问题: "矩阵乘法的实现代码在哪里"
输出: ["mat_mul", "matrix multiplication", "gemm", "MMQ", "MUL_MAT", "mul_mat"]

问题: "KV缓存怎么实现"
输出: ["kv_cache", "key_value_cache", "KV cache", "kv_cache_init", "kv_cache_clear"]

问题: "模型加载的入口函数"
输出: ["llama_model_load", "load_model", "llama_load_model_from_file"]

现在请改写：
问题: {question}

只返回JSON格式：
{{"keywords": ["关键词1", "关键词2", ...], "identifiers": ["标识符1", ...]}}
"""
    
    def rewrite(self, question: str) -> RewrittenQuery:
        """使用规则+LLM混合改写"""
        # 先使用规则改写
        rule_result = super().rewrite(question)
        
        # 再使用LLM补充
        try:
            from ..core.llm_client import call_llm_json
            
            prompt = self.llm_prompt.format(question=question)
            llm_result = call_llm_json(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150
            )
            
            if llm_result:
                # 合并LLM结果
                llm_keywords = llm_result.get('keywords', [])
                llm_identifiers = llm_result.get('identifiers', [])
                
                # 合并去重
                all_keywords = list(dict.fromkeys(
                    llm_identifiers + llm_keywords + rule_result.keywords
                ))
                all_identifiers = list(dict.fromkeys(
                    llm_identifiers + rule_result.identifiers
                ))
                
                rule_result.keywords = all_keywords[:15]
                rule_result.identifiers = all_identifiers[:10]
                rule_result.search_queries = self._construct_search_queries(
                    all_keywords, all_identifiers
                )
        
        except Exception as e:
            # LLM失败时回退到规则改写
            pass
        
        return rule_result


# 全局改写器实例（单例模式）
_rewriter_instance = None


def get_query_rewriter(use_llm: bool = False) -> QueryRewriter:
    """获取Query改写器实例"""
    global _rewriter_instance
    if _rewriter_instance is None:
        if use_llm:
            _rewriter_instance = LLMQueryRewriter()
        else:
            _rewriter_instance = QueryRewriter()
    return _rewriter_instance


def rewrite_query(question: str, use_llm: bool = False) -> RewrittenQuery:
    """
    改写问题的便捷接口
    
    Args:
        question: 原始问题
        use_llm: 是否使用LLM增强
        
    Returns:
        RewrittenQuery: 改写结果
    """
    rewriter = get_query_rewriter(use_llm)
    return rewriter.rewrite(question)


def get_grep_keywords(question: str, use_llm: bool = False) -> List[str]:
    """
    获取Grep搜索关键词的便捷接口
    
    Args:
        question: 原始问题
        use_llm: 是否使用LLM增强
        
    Returns:
        List[str]: 关键词列表
    """
    rewriter = get_query_rewriter(use_llm)
    return rewriter.get_grep_keywords(question)


# 测试代码
if __name__ == "__main__":
    # 测试用例
    test_questions = [
        "矩阵乘法的实现代码在哪里",
        "KV缓存怎么实现",
        "模型加载的入口函数",
        "ggml_backend_graph_compute 是做什么的",
        "量化函数在哪里定义",
        "attention机制的实现",
    ]
    
    rewriter = QueryRewriter()
    
    print("=" * 70)
    print("Query改写器测试")
    print("=" * 70)
    
    for q in test_questions:
        print(f"\n问题: {q}")
        result = rewriter.rewrite(q)
        print(f"  标识符: {result.identifiers}")
        print(f"  关键词: {result.keywords[:8]}")
        print(f"  搜索query: {result.search_queries[:4]}")
