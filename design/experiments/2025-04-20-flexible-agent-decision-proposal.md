# 完全自由Agent决策方案：从固定流程到自适应启发式搜索

## 当前V8架构的问题分析

### 固定流程的局限

```
当前V8流程:
Step 1: 语义搜索(5个) → 文件扩展(→50个) → Grep Fallback(可选)
Step 2-5: ReAct循环(expand_callers/expand_callees/sufficient)

问题:
1. Step 1工具调用是强制的，不考虑问题类型
2. ReAct决策空间小(只有3个action)
3. 没有利用历史决策效果反馈
4. 无法根据信息增益动态调整策略
```

### 效率浪费分析

| 问题类型 | 实际所需工具 | V8强制调用 | 浪费评估 |
|---------|------------|-----------|---------|
| 具体函数定位 | Grep精确匹配 | 语义搜索+文件扩展 | 高 |
| 设计决策分析 | Issue搜索+架构文档 | 全量函数召回 | 中 |
| 调用链追踪 | Neo4j扩展 | 文件扩展(无关) | 高 |
| 行号查询 | Grep+文件 | 语义搜索 | 高 |
| 模块架构 | 文件级扩展 | Neo4j调用链 | 中 |

---

## 方案一：完全自由Agent决策

### 核心思想

将工具选择权完全交给Agent，基于当前状态和目标自主决策。

### 扩展决策空间

```python
# 当前V8决策空间（仅3个）
actions_v8 = ["expand_callers", "expand_callees", "sufficient"]

# 自由决策方案（12个）
actions_free = {
    # 搜索类（4个）
    "semantic_search": "当问题涉及概念理解、设计意图时使用",
    "grep_search": "当需要精确匹配函数名、变量名、行号时使用", 
    "file_search": "当需要查看某文件内所有函数时使用",
    "issue_search": "当问题涉及设计决策、Bug修复、优化策略时使用",
    
    # 关系扩展类（4个）
    "expand_callers": "当需要追踪谁调用了某函数时使用（向上追溯）",
    "expand_callees": "当需要了解某函数内部实现时使用（向下追溯）",
    "expand_neighbors": "当需要了解某函数上下文时使用（同作用域）",
    "expand_similar": "当需要找语义相似函数时使用",
    
    # 分析与综合类（2个）
    "summarize": "当信息杂乱需要整理时使用",
    "cross_reference": "当需要验证多个信息源一致性时使用",
    
    # 终止类（2个）
    "answer": "当信息充足可以生成答案时使用",
    "clarify": "当问题不明确需要用户澄清时使用"
}
```

### 状态表示增强

```python
@dataclass
class SearchState:
    """搜索状态的完整表示"""
    
    # 收集的信息
    functions: List[FunctionInfo]
    issues: List[IssueInfo]
    code_snippets: List[Snippet]
    
    # 决策历史（用于学习）
    action_history: List[Dict]  # {action, params, gain, time_cost}
    
    # 问题理解
    question_type: str  # "location"/"design"/"chain"/"comparison"/...
    question_keywords: List[str]
    target_entities: List[str]  # 提取的函数名、文件名等
    
    # 当前评估
    confidence: float  # 当前答案置信度 [0-1]
    coverage: Dict[str, float]  # 各维度覆盖度
    info_redundancy: float  # 信息冗余度
```

### 决策Prompt设计

```python
DECISION_PROMPT_TEMPLATE = """
你是专业的代码检索Agent。请基于当前状态选择最优的下一步动作。

## 问题分析
问题: {question}
类型: {question_type}
关键实体: {target_entities}

## 当前状态
已收集:
- 函数: {func_count}个 (高相关≥0.7: {high_rel}, 中相关0.5-0.7: {med_rel})
- Issue: {issue_count}个
- 代码片段: {snippet_count}个

覆盖维度:
- 实现细节: {coverage[implementation]:.0%}
- 调用关系: {coverage[call_chain]:.0%}
- 设计意图: {coverage[design]:.0%}

## 决策历史（最近3轮）
{action_history}

## 上次动作效果评估
动作: {last_action}
信息增益: {last_gain}个新函数
质量评分: {last_quality}
效率: {last_efficiency}

## 可用工具及适用场景

### 搜索类
1. semantic_search(query, top_k=5)
   - 适用: 概念理解、设计意图、模糊查询
   - 预期增益: 中（5个语义相关函数）

2. grep_search(keyword, limit=10)
   - 适用: 精确定位函数、变量、行号
   - 预期增益: 高（精确匹配）

3. file_search(file_path)
   - 适用: 了解文件整体结构
   - 预期增益: 高（同文件函数通常相关）

4. issue_search(query, top_k=3)
   - 适用: 设计决策、优化原因、Bug背景
   - 预期增益: 中（设计上下文）

### 关系扩展类
5. expand_callers(function_name, depth=1)
   - 适用: 追踪调用链上游
   - 预期增益: 低-中（平均0.5个/轮）

6. expand_callees(function_name, depth=1)
   - 适用: 了解实现细节
   - 预期增益: 低-中（平均0.5个/轮）

### 终止类
7. answer()
   - 适用: 信息充足（confidence > 0.8）
   - 注意: 一旦选择无法撤回

## 决策原则
- 信息增益连续2轮为0 → 改变策略或answer
- confidence > 0.8且coverage > 80% → answer
- 问题类型为"定位" → 优先grep_search
- 问题类型为"设计" → 优先issue_search + file_search
- 高相关函数已覆盖关键概念 → 考虑answer

返回JSON格式决策:
{
    "thought": "详细分析当前状态、问题需求和工具选择逻辑",
    "action": "工具名称",
    "params": {"参数": "值"},
    "expected_gain": "高/中/低",
    "confidence": 0.85,
    "alternative": "备选动作（如果首选失败）"
}
"""
```

---

## 方案二：启发式搜索策略

### 启发式函数设计

```python
class HeuristicScorer:
    """评估当前状态到回答问题的距离"""
    
    def __init__(self):
        self.weights = {
            'relevance': 0.30,     # 函数相关性
            'coverage': 0.25,      # 概念覆盖度
            'diversity': 0.15,     # 信息来源多样性
            'specificity': 0.20,   # 答案具体性（行号等）
            'freshness': 0.10      # 信息时效性
        }
    
    def score_state(self, state: SearchState, question: str) -> float:
        """
        计算当前状态的启发式分数 (0-1)
        越高表示越接近能回答问题的状态
        """
        scores = {
            'relevance': self._score_relevance(state),
            'coverage': self._score_coverage(state, question),
            'diversity': self._score_diversity(state),
            'specificity': self._score_specificity(state),
            'freshness': self._score_freshness(state)
        }
        
        total = sum(scores[k] * self.weights[k] for k in scores)
        return min(total, 1.0)
    
    def _score_relevance(self, state):
        """高相关函数的比例"""
        if not state.functions:
            return 0.0
        high_rel = sum(1 for f in state.functions if f.score > 0.7)
        return min(high_rel / 3, 1.0)  # 3个高相关为满分
    
    def _score_coverage(self, state, question):
        """问题关键概念的覆盖度"""
        keywords = set(extract_keywords(question))
        if not keywords:
            return 0.0
            
        covered = set()
        for func in state.functions:
            func_text = f"{func.name} {func.file} {func.text}"
            for kw in keywords:
                if kw.lower() in func_text.lower():
                    covered.add(kw)
        
        return len(covered) / len(keywords)
    
    def _score_diversity(self, state):
        """信息来源多样性"""
        sources = set(f.source for f in state.functions)
        # embedding, grep, file_expansion, neo4j_caller, neo4j_callee, issue
        return min(len(sources) / 4, 1.0)
    
    def _score_specificity(self, state):
        """是否有具体行号、精确位置"""
        for func in state.functions:
            # 检查是否有行号信息
            if re.search(r'(行\s*\d+|:\s*\d+|line\s*\d+)', func.text, re.I):
                return 1.0
            # 检查是否有精确文件路径
            if '/' in func.file and len(func.file.split('/')) > 2:
                return 0.8
        return 0.3
    
    def _score_freshness(self, state):
        """信息的时效性（基于commit时间）"""
        # 简化处理：Issue越新越好
        if state.issues:
            newest = max(issue.updated_at for issue in state.issues)
            age_days = (datetime.now() - newest).days
            return max(0, 1 - age_days / 365)  # 一年内满分
        return 0.5
```

### A*搜索算法应用

```python
class AdaptiveSearcher:
    """使用A*搜索找到最优的工具调用序列"""
    
    def __init__(self):
        self.heuristic = HeuristicScorer()
        self.cost_model = CostModel()  # 各工具的时间成本
    
    def search(self, question: str, max_cost: float = 100.0) -> List[Action]:
        """
        A*搜索最优动作序列
        
        f(n) = g(n) + h(n)
        g(n): 从初始状态到n的实际成本（时间）
        h(n): 从n到目标的启发式估计
        """
        initial_state = SearchState(question=question)
        
        # 优先队列: (f_score, g_score, state, path)
        open_set = [(
            self.heuristic.score_state(initial_state, question),  # f = h (g=0)
            0,  # g
            initial_state,
            []  # 动作路径
        )]
        
        closed_set = set()
        best_plan = None
        best_score = 0
        
        while open_set:
            f, g, state, path = heapq.heappop(open_set)
            
            # 检查是否达到目标
            if self._is_goal(state, question):
                return path
            
            # 状态去重
            state_key = self._hash_state(state)
            if state_key in closed_set:
                continue
            closed_set.add(state_key)
            
            # 生成后继状态
            for action in self._get_valid_actions(state):
                new_state, cost = self._apply_action(state, action)
                new_g = g + cost
                new_h = self.heuristic.score_state(new_state, question)
                new_f = new_g + (1 - new_h) * 50  # 距离越近h越高，需要反转
                
                if new_g < max_cost:
                    heapq.heappush(open_set, (new_f, new_g, new_state, path + [action]))
        
        return best_plan or []
    
    def _is_goal(self, state, question):
        """判断是否达到回答问题的条件"""
        score = self.heuristic.score_state(state, question)
        return score > 0.85  # 启发式分数超过阈值
```

---

## 方案三：问题类型驱动的策略选择

### 自动问题分类

```python
class QuestionClassifier:
    """基于规则+LLM的问题分类器"""
    
    PATTERNS = {
        'location': [
            r'在哪里|在哪个文件|第几行|位置|定位',
            r'where is|which file|line \d+|location of'
        ],
        'design_decision': [
            r'为什么|为何|怎么设计|设计意图|权衡',
            r'why|design decision|trade-off|rationale'
        ],
        'call_chain': [
            r'调用|谁调用了|被谁调用|调用链|流程',
            r'call|caller|callee|chain|flow'
        ],
        'implementation': [
            r'怎么实现|如何实现|实现细节|逻辑',
            r'how to implement|implementation|logic'
        ],
        'comparison': [
            r'区别|对比|比较|vs|versus',
            r'difference|compare|vs|versus'
        ]
    }
    
    def classify(self, question: str) -> Tuple[str, float]:
        """返回问题类型和置信度"""
        # 规则匹配
        for qtype, patterns in self.PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, question, re.I):
                    return qtype, 0.8
        
        # LLM fallback
        prompt = f"""
        将以下问题分类到一种类型：
        问题: {question}
        
        类型选项:
        - location: 查询代码位置、行号、文件
        - design_decision: 询问设计原因、权衡、意图
        - call_chain: 询问调用关系、执行流程
        - implementation: 询问实现细节、代码逻辑
        - comparison: 比较不同实现/方案
        
        返回JSON: {{"type": "类型", "confidence": 0.9}}
        """
        result = call_llm_json(prompt)
        return result.get('type', 'unknown'), result.get('confidence', 0.5)
```

### 策略模板

```python
STRATEGY_TEMPLATES = {
    'location': {
        'description': '精确代码定位',
        'preferred_tools': ['grep_search', 'file_search'],
        'avoid_tools': ['semantic_search', 'expand_callers'],
        'early_stop': True,  # 找到即停
        'max_steps': 3
    },
    'design_decision': {
        'description': '设计决策分析',
        'preferred_tools': ['issue_search', 'file_search', 'semantic_search'],
        'avoid_tools': ['grep_search'],
        'early_stop': False,
        'max_steps': 5
    },
    'call_chain': {
        'description': '调用链追踪',
        'preferred_tools': ['expand_callers', 'expand_callees', 'file_search'],
        'avoid_tools': ['issue_search'],
        'early_stop': False,
        'max_steps': 5
    },
    'implementation': {
        'description': '实现细节分析',
        'preferred_tools': ['file_search', 'expand_callees', 'semantic_search'],
        'avoid_tools': ['issue_search'],
        'early_stop': False,
        'max_steps': 6
    }
}
```

---

## 实现路线图

### Phase 1: 基础自由决策（2周）

```python
# 目标: 实现12个工具的自由选择
# 关键修改:
1. 扩展ReAct决策空间到12个action
2. 增强state表示，包含history和coverage
3. 优化decision prompt，加入工具适用场景

# 预期效果:
- 准确率提升2-3%（减少无效工具调用）
- 时延降低10-15%（避免强制步骤）
```

### Phase 2: 启发式评估（2周）

```python
# 目标: 实现HeuristicScorer
# 关键修改:
1. 实现5维启发式评分
2. 集成到decision流程
3. 添加early stop机制

# 预期效果:
- 平均步数减少1-2轮
- 信息增益提升20%
```

### Phase 3: 问题分类+策略（1周）

```python
# 目标: 问题类型驱动的策略
# 关键修改:
1. 实现QuestionClassifier
2. 定义5种策略模板
3. A/B测试验证

# 预期效果:
- 特定类型问题准确率提升5-8%
- 整体准确率提升2-3%
```

### Phase 4: A*搜索优化（2周）

```python
# 目标: 全局最优搜索路径
# 关键修改:
1. 实现AdaptiveSearcher
2. 离线学习cost model
3. 在线路径优化

# 预期效果:
- 整体时延降低20-30%
- 准确率提升1-2%
```

---

## 风险评估

| 风险 | 可能性 | 影响 | 缓解措施 |
|-----|--------|------|---------|
| LLM决策不稳定 | 高 | 准确率波动 | 添加规则约束，fallback到默认策略 |
| 工具组合爆炸 | 中 | 时延增加 | 限制max_steps，剪枝无效路径 |
| 启发式权重难调 | 中 | 评分不准 | 基于历史数据自动学习权重 |
| 复杂度增加 | 高 | 维护困难 | 模块化设计，分阶段实现 |

---

## 预期效果总结

| 指标 | 当前V8 | 目标 | 提升 |
|-----|--------|------|------|
| 准确率 | 77.2% | 80-82% | +3-5% |
| 平均步数 | 3.5轮 | 2.5轮 | -28% |
| 平均时延 | 69s | 50s | -28% |
| 零增益比例 | 15% | <5% | -66% |

---

## 下一步行动

1. **快速原型**: 实现Phase 1（自由决策），在50题上测试
2. **数据收集**: 记录决策历史和效果，训练启发式权重
3. **A/B测试**: 对比固定流程 vs 自由决策 vs 启发式搜索
4. **逐步迭代**: 按路线图分阶段实现，每阶段验证效果
