# 0809 失败题逐步深度复盘

> 数据基底：`results/qa_earlyfinish_full.json`（det 84.8% 那一轮全量）+ `results/eval_earlyfinish_det.json`（det 口径）。
> 轨迹导出工具：`scripts/analysis/dump_failure_trajectories.py`，逐题原始轨迹在 `analysis_output/0809_traces/`。
> 本文对 det<100% 的 12 道题逐题展开：初始召回给了什么、每一步 Agent 输入/输出是什么、在哪一步因为什么断链。

## 总览

| 题 | det | gold 文件数 | 漏引 | 主断链类型 | "一步之遥"动作是否存在 |
|---|---|---|---|---|---|
| 001 | 0% | 3 | 3 | C 召回断链 + 析构工具 bug 死锁 | ✓ find_callers(ggml_sycl_set_device) |
| 002 | 33% | 3 | 2 | A 差一跳 ×2 | ✓ find_callers(common_chat_templates_apply) |
| 007 | 67% | 3 | 1 | A 差一跳（核心函数没查调用方） | ✓ find_callers(calculate_diff_split) |
| 010 | 50% | 2 | 1 | 池内 rank1 gold 未读 + 上下文丢失循环 | ✓ read_function(ggml_backend_cann_reg) |
| 013 | 0% | 1 | 1 | C 召回断链 + 同目录探索缺失 | ✓ list_files(ggml-cann)/search("cache") |
| 014 | 33% | 3 | 2 | A 差一跳 ×2（符号两次出现没追） | ✓ read+find_callers(llama_model_chat_template) |
| 034 | 75% | 4 | 1 | 析构函数读不到 → 不敢引用 | ✓ read ~ggml_backend_meta_context（需修工具） |
| 037 | 50% | 2 | 1 | A 差一跳（find_callers 一次没做）+ 重读循环 | ✓ find_callers(common_chat_verify_template) |
| 040 | 33% | 3 | 2 | A 差一跳 ×2（同 014 漏链） | ✓ 同 014 |
| 041 | 50% | 2 | 1 | A 差一跳（dispatch 未下沉到算子文件） | ✓ find_callees(ggml_sycl_compute_forward) |
| 042 | 50% | 2 | 1 | 召回词义撞车（set→set_rows）+ 未目录兜底 | ✓ find_callees(dispatch)/list_files(ggml-sycl) |
| 044 | 0% | 1 | 1 | C 召回断链（锚点全错）+ A 差一跳 | ✓ 追 llama_init_from_model |

**步数浪费合计：360 步中 64% 浪费（重复拒绝 30% + 记忆命中重读 23% + 空搜索 11%）。12/12 题存在"一步之遥"动作。**

（逐题分析见下文各节，汇总分析与改进建议见文末。）

---

## 001（0%）：设备切换调用方契约 —— SYCL 三个调用点文件全漏

**问题**：设备切换相关实现，调用方依赖什么行为，失败/重复切换是否破坏调用方假设。
**gold**：`ggml-sycl/common.cpp`、`cpy.cpp`、`element_wise.cpp`（都是 `ggml_sycl_set_device` 的调用点）。

### 逐步轨迹要点

1. **初始召回 45 个候选，0 个 gold**。问题不点名任何后端，关键词抽取把"设备切换"理解成了 miniaudio 音频设备 + CANN：池子里全是 `chat-diff-analyzer` 测试、`ma_*` 音频回调、`aclnn_*` 算子。SYCL 目录只有 `dpct/helper.hpp` 里两个边缘函数（`compare_dev`/`dev_mgr`，score 0.008-0.03）。
2. step 1-3：读 miniaudio 三个音频设备回调（方向错但合理——召回池就给了这些）。
3. step 5 ⛔：重复 read_function 被拒（第 1 次重复拒绝）。
4. step 6：改读 `ggml_cann_set_device`、`ggml_sycl_set_main_device`、`select_device`——方向开始接近。
5. step 8：`find_callers(ggml_cann_set_device)` 找到 10 个 CANN 调用点（成功，但 CANN 不是 gold）。
6. **step 24（全场最关键一步）**：`find_callers(select_device)` 返回 7 处调用，最后一行是 **`ggml_sycl_set_device @ ggml/src/ggml-sy...`（被截断）**——gold 链的核心函数名字已经出现在 Agent 眼前。但 Agent **从未对 `ggml_sycl_set_device` 执行 find_callers**。如果执行了，common.cpp:77、cpy.cpp:520、element_wise.cpp:378/404 四个 gold 调用点会一次性全部出现。
7. step 16-30：**死锁循环**。find_callers 结果里的调用点包含两个析构函数 `~ggml_cann_pool_buf` / `~ggml_cann_pool_buf_prio`，`read_function` 对 `~` 开头的析构函数名**永远返回 "function not found in this file"**（工具 bug）；而系统的"升级干预"机制反复强制"请立即从以下未读调用点中选择 read_function：~ggml_cann_pool_buf..."——**强制要求读两个永远读不到的函数**，Agent 在 step 16/18/27/30 四次尝试均失败，加上监督者幻觉符号搜索（见下），30 步里至少 12 步被这个死循环烧掉。
8. 监督者 5 次干预中 4 次建议搜索**仓库里根本不存在的符号**：`switch_device`、`device_switch`、`handle_device_switch`、`ma_device_handle_rerouted`、`ma_context_handle_default_device_changed`。Agent 老实执行，每次"没有找到"，再重复搜，再被重复拒绝（step 21/22/26/28 ⛔）。
9. step 29 finish 被拒：guard 要求对三个 miniaudio 回调补 find_callers——这些与 gold 无关。
10. 最终答案其实写得不差（CANN 路径分析很细），但 SYCL 三个 gold 文件**从头到尾没有进入任何工具结果**（除了 step 24 那一行被截断的名字）。

### 断点归因

- **主因（C 类召回断链）**：gold 函数 embedding 排名 1200+，初始池 0 gold，问题无锚点（不点名 SYCL）。
- **次因 1（A 类差一跳）**：step 24 工具结果已出现 `ggml_sycl_set_device`，Agent 没追。这是"发现但未跟进"——frontier 里这个名字没有被记住。
- **次因 2（工具 bug 死锁）**：析构函数名读不到 + 升级干预反复强制读 → 烧掉 1/3 预算。
- **次因 3（监督者幻觉）**：建议的符号 4/5 不存在，制造了大量无效搜索和重复拒绝。

---

## 002（33%）：聊天模板选择返回值语义 —— 差一跳到 common.cpp / llama-model.cpp

**问题**：聊天模板选择逻辑的空模板/缺失模板/fallback 返回值语义是否与调用方一致。
**gold**：`common/chat.cpp`（已引✓）、`common/common.cpp`、`src/llama-model.cpp`。

### 逐步轨迹要点

1. 初始召回 22 个候选，3 个 gold（全部在 chat.cpp 内）。**common.cpp 和 llama-model.cpp 从头到尾不在池里**。
2. step 1-4：批量读 `compare_variants`、`llm_chat_detect_template`、`llm_chat_apply_template`、`apply_jinja/legacy`——开局方向正确。
3. step 6：`find_callers(common_chat_templates_apply_jinja)` → 发现上层 `common_chat_templates_apply`（chat.cpp:2308），系统自动读取了它。**这是通往 common.cpp 的桥头堡——common.cpp 里 `common_init_from_params` 一带是 `common_chat_templates_apply` 的真实调用方，但 Agent 从未对 `common_chat_templates_apply` 本身执行 find_callers**。
4. step 7/15：`find_callers(llm_chat_apply_template)` / `find_callers(llm_chat_detect_template)` 都指向 `src/llama.cpp:1224-1228` 的 `llama_chat_apply_template`，它内部用 `llm_chat_detect_template(curr_tmpl)` 拿模板——**`curr_tmpl` 的来源正是 gold 函数 `llama_model_chat_template`（定义在 src/llama-model.cpp）**。Agent 读了 llama.cpp 的包装函数，但没有继续追"模板字符串从哪来"这最后一跳。
5. step 11/13/23/30：监督者反复建议搜 `get_template_or_fallback`、`select_chat_template`、`template_fallback`、`select_template`——**全部不存在**，4 次搜索空手而归 + 4 次重复拒绝。
6. step 17-30：大量重复动作拒绝（8 次⛔），系统升级干预反复强推 `chat-diff-analyzer.cpp` 里的调用点（`analyze_tool_calls` 等）——**这些与模板选择语义基本无关**，Agent 被推进无关方向读了 `compare_reasoning_presence`。
7. 最终答案：chat.cpp 部分分析扎实（jinja/legacy 两套语义讲清楚了），但 common.cpp（参数层入口）和 llama-model.cpp（模板来源）缺失。

### 断点归因

- **A 类差一跳 ×2**：
  - 一跳 1：`common_chat_templates_apply`（step 6 已自动读过）→ find_callers → common.cpp。没做。
  - 一跳 2：`llama_chat_apply_template` 里的 `curr_tmpl` → `llama_model_chat_template` → llama-model.cpp。没做。
- **监督者幻觉 + 升级干预误导**：幻觉符号搜索烧步数；升级干预强推无关的 diff-analyzer 调用点，把 Agent 从正路上拉走。
- **根子问题**：Agent 没有"证据链向上溯源"的习惯——读了 `llama_chat_apply_template(tmpl, ...)` 却没有问"tmpl 是谁给的"。

---

## 007（67%）：diff 拆分返回结构 —— 从未查核心函数的调用方

**问题**：两段模板文本差异拆分逻辑的返回结构/参数/副作用是否被调用方正确使用。
**gold**：`chat-auto-parser-helpers.cpp`（✓）、`chat-diff-analyzer.cpp`（✓）、`common/chat.cpp`（✗ 未读）。

### 逐步轨迹要点

1. 初始召回 21 个候选，8 个 gold（helpers + diff-analyzer），**chat.cpp 不在池内**。
2. step 3 `list_functions(chat-auto-parser-helpers.cpp)` 返回里明确显示 **`calculate_diff_split (87-207)`**——这才是"差异拆分逻辑"的本体。
3. **全场 30 步，Agent 从未 read 过 `calculate_diff_split`，更从未对它执行 find_callers**。而 chat.cpp 里的 `common_chat_msg_diff::compute_diffs`（002 题的 list_functions 结果里能看到它就在 chat.cpp:153-219）正是 `calculate_diff_split` 的调用方——一次 find_callers 就能把 chat.cpp 带进来。
4. Agent 反复读 `compare_variants`：step 1/2/17/19/20/27/28 共 7 次（多数是"记忆命中"重读，因为参数写法略有不同——`file_path` vs `file_paths`、列表顺序变化——**绕过了精确匹配的去重 guard**）。
5. step 9/22/23：搜 `DiffResult`、`diff_segment`——不存在（真实结构名是 `diff_split`/`compare_variants_result`）。这两个名字部分是监督者 step15/20/25 建议的（监督者幻觉）。
6. step 10/16/21：重复 read_lines(helpers.h, 1, 100) 三次，两次被拒。
7. step 30 finish 被拒：guard 要求对三个**测试函数**补 find_callers——测试函数的调用方对回答"生产调用方契约"没有价值，guard 在这里机械执行。

### 断点归因

- **A 类差一跳**：`calculate_diff_split` 在 step 3 已入视野，30 步未读未查调用方。Agent 把"差异拆分"锚定在 `compare_variants`（其实是调用 diff 拆分的外层比较器）上，没有下沉到本体。
- **去重 guard 被参数抖动绕过**：同一函数换个参数写法就重读成功（记忆命中），7 次重读烧掉约 1/4 预算；而精确重复又被拒，Agent 在"重读成功"和"重复被拒"之间来回震荡。
- **监督者幻觉**：`DiffResult`/`diff_segment`/`split_diff` 三个不存在的符号被反复建议。

---

## 010（50%）：后端注册入口 —— 池内排名第 1 的 gold 函数 30 步没读

**问题**：后端注册入口的静态初始化/互斥/重复调用状态一致性。
**gold**：`ggml-backend-reg.cpp`（✓）、`ggml-cann/ggml-cann.cpp`（✗ 未读）。

### 逐步轨迹要点

1. **初始召回池第 1 名就是 gold 函数 `ggml_backend_cann_reg`（ggml-cann.cpp:2939），第 18 名是 `ggml_backend_cann_reg_get_proc_address`（同文件）**。召回完全成功。
2. **30 步里 Agent 从未读这两个函数，甚至从未访问 ggml-cann.cpp**。全部预算消耗在 ggml-backend-reg.cpp 一个文件上。
3. 死循环的成因是**上下文丢失**：step 3/4/6/9 的 thought 反复出现"虽然系统提示已读过……但我没有在上下文中看到具体内容"——历史窗口只保留最近约 10 步，更早的 observation 被裁掉后，Agent 认为自己"还没读过"，于是重读 → 精确重复被拒（step 5/8/9/15/19/23/24/27 共 8 次⛔）→ 换个行号区间再读（1-200、1-240、1-250、1-260、1-300、200-500、300-500、300-600、1-114、1-100、1-120……**同一个文件头部被用 11 种不同区间读了 11 次**）。
4. 监督者 5 次干预全部判"方向正确"，反复建议"继续精读 ggml-backend-reg.cpp"——**强化了单文件循环，从未提醒"池子里还有 ggml-cann 的注册函数没读"**。
5. 收尾：30 步只读了 9 个函数。最终答案对 backend-reg.cpp 的分析（无锁、静态局部变量线程安全）是对的，但"AI 生成的注册入口"（CANN reg）这个题目主体完全没碰。

### 断点归因

- **B 类变体（池内 gold 未读）+ 上下文管理失败**：不是召回问题，不是调查能力问题——是 Agent 的工作记忆丢了已读内容，陷入"重读-被拒-换区间再读"的死循环，30 步实际有效调查只有约 9 个函数。
- **frontier 未持久化的直接证据**：池内第 1 名候选一旦滑出可见上下文，就永远消失了。没有任何机制提醒"初始候选里还有 N 个高排名函数没看"。
- **监督者盲区**：监督者只看最近轨迹判断方向"正确/可疑"，手里没有"初始候选清单 vs 已读清单"的对账能力，所以看不出 Agent 在空转。

---

## 013（0%）：缓存扩容/重分配失败路径 —— 进了 ggml-cann 门，没进 aclnn_ops.cpp 房间

**问题**：缓存扩容和重新分配逻辑，旧 buffer 释放后申请失败，容量记录和指针状态是否会对不上。
**gold**：`ggml-cann/aclnn_ops.cpp`（`get_cache_acl_tensor` 的缓存 vector 扩容逻辑）。

### 逐步轨迹要点

1. 初始召回 20 个候选，0 个 gold。但**第 10 名是 `realloc`（ggml-cann.cpp:1205）——同目录、相关主题**。
2. step 1-4：读 test-alloc.cpp 四个测试 + `llama_context::memory_update`。方向偏了（KV cache 内存更新 ≠ CANN 算子缓存扩容），但来自召回池，合理。
3. step 6/7/8/12/21/26/27：监督者建议搜 `grow_cache`、`realloc_buffer`、`handle_alloc_failure`、`alloc_failed`、`resize_buffer`——**全部不存在**，7 次搜索空手 + 2 次重复拒绝。
4. **step 17 是最可惜的一步**：`search_symbol("reallocate")` 的 grep 命中了 `ggml/src/ggml-cann/ggml-cann.cpp` 等 5 个文件，Agent 已经站在 ggml-cann 目录门口——**但从未执行 `list_files(ggml/src/ggml-cann/)`，也从未对 gold 关键词 "cache" 做 search_symbol**。`aclnn_ops.cpp` 里 `get_cache_acl_tensor` 名字就带 cache，搜 "cache" 或在 ggml-cann 目录列文件都极可能命中。
5. step 20：grep "capacity" 命中 `ggml/src/ggml-cann/common.h`——第二次摸到 ggml-cann 目录，还是没进去。
6. step 11/15/18/23/29/30：和 010 一样的**上下文丢失重读循环**——`memory_update` 的 710-762 行被 read_function 读 3 次、read_lines 读 4 次（"之前读过但上下文没有保留具体内容"），4 次被拒。
7. 最终答案：基于 ggml-cann.cpp 的 `realloc`（池内 rank 10）写了"先 clear 再 aclrtMalloc"的风险分析——沾边但不是 gold 的 `get_cache_acl_tensor` 扩容路径。0%。

### 断点归因

- **C 类召回断链（主因）**：gold 函数 embedding 排名极低，池内 0 gold。
- **A 类差两跳（次因）**：池内 rank 10 的 `realloc` 把 Agent 带到了 ggml-cann.cpp，step 17/20 两次摸到目录线索，但 Agent 没有"同目录探索"动作（list_files/list_functions 该目录），也没有围绕"cache"做符号搜索。**进了正确的目录，没进正确的文件**。
- **监督者幻觉**：5 个建议符号全部不存在。
- **上下文丢失重读循环**：约 6 步烧在重复读 memory_update 上。

---

## 014（33%）：模板查找配置一致性 —— gold 符号在 step 8 就出现过，30 步没追

**问题**：聊天模板查找的配置处理，不同入口对默认/指定/缺失模板的返回含义是否一致。
**gold**：`common/chat.cpp`（✓）、`common/common.cpp`、`src/llama-model.cpp`。

### 逐步轨迹要点

1. 初始召回 20 个候选，5 个 gold（全在 chat.cpp）。common.cpp / llama-model.cpp 不在池内。
2. step 1-5：批量读 `common_chat_verify_template`、`common_chat_templates_init`、apply 系列——开局精准。
3. **step 8（关键漏球）**：`find_callees(common_chat_templates_init)` 返回的 callee 列表里**明确包含 `llama_model_chat_template`**——这正是 src/llama-model.cpp 里的 gold 函数（模板来源）。step 15 读 init 源码时 `const auto * str = llama_model_chat...` 又出现一次。**两次出现在眼前，Agent 从未 read 它的定义，也从未对它 find_callers**（find_callers 会带出 common.cpp 等配置入口的调用点）。
4. step 4：`find_callers(common_chat_templates_init)` 返回 10 个调用点（server-context、mtmd-cli、completion、diffusion-cli……），Agent 读了其中几个入口，但**没有读 common/common.cpp 方向的配置传播链**。
5. step 7/9/10/21/22/25：监督者反复建议 `find_template`、`get_chat_template`、`template_exists`——**全部不存在**，6 次搜索空手 + 3 次重复拒绝。真实存在的 `llama_model_chat_template` 反而没人建议。
6. step 17-30：和 010/013 相同的上下文丢失重读循环——`common_chat_verify_template`/`llm_chat_detect_template`/`common_chat_templates_init` 被"记忆命中"重读 6+ 次。
7. 最终答案：chat.cpp 内部三套判据分析得很好，结论第 3 点甚至提到了 `llama_model_chat_template(model, nullptr)`——**知道它重要，但从未读过它的实现**，llama-model.cpp 和 common.cpp 因此缺失。

### 断点归因

- **A 类差一跳 ×2（主因）**：`llama_model_chat_template` 两次出现在工具结果里（step 8 callee 列表、step 15 源码），Agent 没有跟进。这是"发现但未跟进"的教科书案例——frontier 机制没有把 callee 列表里的陌生符号沉淀为待调查项。
- **监督者幻觉（次因）**：建议的 3 个符号不存在，真实的关键符号（已出现在轨迹里）却没被点出来——监督者没有"从工具结果里挑未跟进线索"的能力。
- **上下文丢失重读循环（次因）**：烧掉约 6 步。

---

## 034（75%）：后端释放入口 —— gold 调用点在 find_callers 结果里，但析构函数读不了

**问题**：后端释放入口的空指针保护/释放分发/多调用点契约是否一致，有无漏释放/重复释放。
**gold**：`ggml-backend.cpp`（✓）、`ggml-rpc.cpp`（✓）、`llama-model-loader.cpp`（✓）、`ggml-backend-meta.cpp`（✗ 读过但没引）。

### 逐步轨迹要点

1. step 5 `find_callers(ggml_backend_free)` 返回 50KB 结果，**4 个 gold 文件全部在其中**，包括 `~ggml_backend_meta_context @ ggml-backend-meta.cpp:1505: ggml_backend_free(bc.backend);`——gold 调用点赫然在列。
2. **但 `~ggml_backend_meta_context` 是析构函数**：和 001 的 `~ggml_cann_pool_buf` 一样，`read_function` 对 `~` 开头的名字永远 "function not found"。Agent 从头到尾只读了 5 个函数，meta 析构从未读到实现 → 按"读过才能引"的规则，答案无法引用 backend-meta.cpp（det 判"读过但答案未引用"是因为 grep 结果把文件带进了 files_accessed）。
3. step 9/11/17/26/28/29：监督者建议搜 `backend_free_dispatch`、`release_backend`、`backend_free_all`、`free_backend`——**全部不存在**，6 次搜索空手。
4. step 7-30：10 次重复动作拒绝。升级干预反复强推"请读 ~ggml_backend_meta_context"——和 001 同款死锁：**强制读一个工具读不了的函数**。
5. 最终答案对 perplexity/server/llama-bench 调用点的漏释放分析很扎实（75%），唯独 meta 释放链缺失。

### 断点归因

- **工具 bug（主因）**：析构函数不可读，直接导致 gold 文件"看到了调用点、读不到实现、不敢引用"。001/034 两题都踩中。
- **升级干预死锁（次因）**：把不可读的析构函数列为强制待读项，反复推送。
- **监督者幻觉（次因）**：4 个建议符号不存在。

---

## 037（50%）：模板校验 boolean 语义 —— arg.cpp 在 step 11 送到眼前，30 步没碰

**问题**：聊天模板校验的 Jinja/普通分支是否都只用 boolean 返回、无副作用。
**gold**：`common/arg.cpp`（✗ 未读）、`common/chat.cpp`（✓）。

### 逐步轨迹要点

1. step 11 `search_symbol(common_chat_verify_template)` 返回：**"grep 找到 3 个文件包含 'common_chat_verify_template'：common/arg.cpp、common/chat.h、common/chat.cpp"**——gold 的 arg.cpp（调用点 `common_params_parse_ex:635`）直接出现在结果里。
2. **全场 30 步，Agent 从未对 `common_chat_verify_template` 执行 find_callers**（014 题里同一动作一次就带出了 arg.cpp:635），也从未读 arg.cpp。
3. 预算全部烧在上下文丢失重读循环上：chat.cpp 的 485-509 行被用 11 种不同行号区间重复读（480-515、480-520、460-530、470-520、485-520、460-525、480-545、440-560……），11 次⛔拒绝。Agent 在 thought 里反复说"尚未真正看到函数体"——**它读过 10 遍，但内容滑出可见窗口后它认为自己没读过**。
4. 监督者建议搜 `check_jinja_template`、`validate_chat_template`、`is_valid_template`——全部不存在。
5. 最终答案在 step 30 finish 里甚至写"无法从实现级别完全确认 Jinja/普通分支的具体返回方式"——**代码就在它读过的 10 遍里**，上下文管理失败直接损害了答案质量，不只是引用完整性。

### 断点归因

- **A 类差一跳（主因）**：对核心函数做 find_callers 是这类"调用方契约"题的标准动作，037 偏偏一次都没做；arg.cpp 已在 step 11 的 grep 结果里。
- **上下文丢失重读循环（主因 2）**：30 步里有效信息获取约等于前 5 步。这是本题失败的直接原因——不是没找到，是找到之后丢失了。
- **监督者幻觉（次因）**。

---

## 040（33%）：模板状态读取一致性 —— 和 014 同一条漏链

**问题**：模板缺失/默认模板/调用方判空 fallback 的语义一致性。
**gold**：`common/chat.cpp`（✓）、`common/common.cpp`、`src/llama-model.cpp`（均✗）。

### 逐步轨迹要点

1. 初始池 20 个候选 5 个 gold（全在 chat.cpp）。step 4 读 init 源码时 `llama_model_chat...` 再次出现；step 8 `find_callers(common_chat_templates_init)` 返回 10 个入口调用点。
2. **与 014 完全同款的漏法**：`llama_model_chat_template` 在视野里出现至少 2 次，从未读定义（llama-model.cpp）、从未 find_callers（→ common.cpp）。最终答案甚至引用了 mtmd 里的 `llama_model_chat_template(model, nullptr)` 判空代码——知道这个符号是判空语义的关键，却没去看它本身。
3. 监督者幻觉符号：`template_missing`、`has_template`、`get_chat_template`、`load_chat_template`、`default_template`——全部不存在，烧掉 6+ 步。
4. step 23/24：升级干预明确写了 `format_input_text @ examples/diffusion/diffusion-cli.cpp`，Agent 却在 common/chat.cpp 里读它——**hint 里给了文件路径都没用上**，两次失败。
5. step 30 finish 被拒（guard 要求对已查过的函数补 find_callers + 读测试函数），步数耗尽。

### 断点归因

- **A 类差一跳 ×2（主因）**：同 014——`llama_model_chat_template` 的定义与调用方未追。002/014/040 三题是同一 gold 三元组（chat.cpp/common.cpp/llama-model.cpp），**同一断链模式重复出现 3 次**，说明这是系统性盲区而非偶发：Agent 读了"用模板的代码"，从不追"模板从哪来"。
- **监督者幻觉 + hint 利用失败（次因）**。

---

## 041（50%）：SYCL kernel 提交路径 —— 没从 dispatch 函数下到具体算子文件

**问题**：SYCL kernel 提交的队列/tensor 生命周期/上层 compute 顺序是否一致。
**gold**：`ggml-sycl/ggml-sycl.cpp`（✓）、`ggml-sycl/count-equal.cpp`（✗ 未读）。

### 逐步轨迹要点

1. 初始池有 2 个 gold（都在 ggml-sycl.cpp）。count-equal.cpp 不在池内。
2. step 6 `find_callees(ggml_backend_sycl_graph_compute_impl)` 返回里有 **`ggml_sycl_compute_forward`**——这是 SYCL 的算子分发总入口，它的 switch 里就有 `GGML_OP_COUNT_EQUAL → ggml_sycl_op_count_equal`（count-equal.cpp）。**Agent 从未对 `ggml_sycl_compute_forward` 执行 find_callees 或 read**——一跳之差，gold 的算子文件就到手了。
3. step 27 `list_files(ggml/src)` 显示 ggml-sycl/ 有 77 个文件，Agent 没有进一步 `list_files(ggml/src/ggml-sycl)`——目录探索停了一层。
4. `ggml_backend_sycl_graph_compute_impl` 被重读 7 次（记忆命中/拒绝交替），监督者幻觉符号 `queue_submit`/`enqueue_kernel`/`sycl_queue`/`ggml_allocr` 烧掉 6+ 步（SYCL 真实提交原语是 `stream->memcpy`/`queue.submit`，这些名字都不存在）。
5. 最终答案对 ggml-sycl.cpp 的分析（queue FIFO、assume_buffer_outlives_graph、synchronize）质量不错，但具体算子 kernel 提交文件缺失。

### 断点归因

- **A 类差一跳（主因）**：`ggml_sycl_compute_forward` 在 step 6 的 callee 列表里，追它就分发到 count-equal.cpp。Agent 一直在"后端主文件"层面打转，没有下沉到"具体算子实现文件"。
- **目录探索停一层（次因）**：知道 ggml-sycl/ 有 77 个文件但没列出来看。
- **重读循环 + 监督者幻觉（次因）**。

---

## 042（50%）：SYCL set 操作路径 —— 读了 set_rows.cpp 和 set_tensor，漏了 set.cpp

**问题**：SYCL set 操作的队列/shape 检查/共享上下文使用顺序。
**gold**：`ggml-sycl/ggml-sycl.cpp`（✓）、`ggml-sycl/set.cpp`（✗ 未读）。

### 逐步轨迹要点

1. 初始池 22 个候选 6 个 gold（全在 ggml-sycl.cpp）。**set.cpp 不在池内**——"set" 关键词召回撞上了 `set_rows_sycl`（set_rows.cpp）、`set_device`、`set_tensor_async`，唯独没有 `ggml_sycl_op_set`（set.cpp）。
2. Agent 读了 set_rows.cpp（step 1/2/5）——**这是另一个算子**（SET_ROWS ≠ SET）。名字撞车把调查带偏了。
3. 与 041 同款：从未 `find_callees(ggml_sycl_compute_forward)`（dispatch switch 里 `GGML_OP_SET → ggml_sycl_op_set`），从未 `list_files(ggml/src/ggml-sycl)`，从未 `search_symbol("op_set")`。三条路都能到 set.cpp，一条都没走。
4. `set_tensor_async` 被重读 7 次（read_function 3 次 + read_lines 4 种区间），8 次⛔；监督者幻觉符号 `shared_context`/`enqueue_set`/`queue_order`/`validate_shape`/`get_sycl_queue` 烧掉 6+ 步。
5. step 30 finish 被拒后步数耗尽。最终答案对 set_tensor_async 的分析合理，但"SET 算子的运行时路径"本体（set.cpp）缺失 → 50%。

### 断点归因

- **召回词义撞车 + 无目录兜底（主因）**："set" 召回全部命中 set_rows/set_tensor/set_device，真正的 set.cpp 不在池；Agent 锚定在这些近义函数上，没有用目录列举或 dispatch 下沉来发现"还有一个 set.cpp"。
- **A 类差一跳（次因）**：`find_callees(ggml_sycl_compute_forward)` 依然是一步到位的动作，没做。
- **重读循环 + 监督者幻觉（次因）**。

---

## 044（0%）：上下文初始化入口的 nullptr 早退 —— 错误锚点 + 14 次重复拒绝

**问题**：上下文初始化入口的多个 nullptr 早退分支是否对应清楚的校验条件。
**gold**：`src/llama-context.cpp`（`llama_context` 构造函数的多个失败早退分支）。

### 逐步轨迹要点

1. **初始召回 21 个候选，0 个 gold，且方向全错**："上下文初始化入口"被关键词抽取映射到 `virtgpu_init_context`（GPU 虚拟化）、`ma_context_init`（音频）、`init_context`（安卓示例 app）。真正的目标 `llama_context` 构造函数 / `llama_init_from_model` 不在池里。
2. Agent 全程在 ai_chat.cpp（一个 Android example）里打转。监督者 5 次干预全部判"方向正确"，反复说"回到 ai_chat.cpp"——**监督者没有能力识别"锚点本身就是错的"，只会顺着 Agent 当前位置强化**。
3. **step 11/23 其实已经读到了通往 gold 的桥**：ai_chat.cpp 的 `init_context` 里写着 `llama_init_from_model(g_model, ctx_params)`。Agent 在答案里都引用了这行——**但从未 find_callees/read 这个函数**（它在 src/llama.cpp，内部构造 `llama_context` → llama-context.cpp，即 gold）。这是 C 类召回失败之上又叠了一个 A 类差一跳。
4. **全场最惨烈的死循环：14 次重复动作拒绝**。ai_chat.cpp 的 76-105 行被读 6+ 遍（"未记录实际内容"——上下文丢失），read_lines 换区间绕 guard 又被拒。30 步有效动作约 10 个。
5. 最终答案对 ai_chat.cpp 的分析其实挺细（返回值无法区分失败类型、g_model 隐患），但区域完全错误 → 0%。

### 断点归因

- **C 类召回断链（主因）**：无锚问题 + 关键词撞车（"上下文初始化"→ virtgpu/audio/android，而不是 llama_context）。
- **A 类差一跳（次因）**：`llama_init_from_model` 出现在已读代码里，没追。
- **监督者无纠偏能力（次因）**：锚点错时监督者只会强化错误方向。
- **上下文丢失重读循环（次因）**：14 次拒绝，预算利用率约 1/3。

---

## 汇总分析：gap 到底在哪

### 1. 步数浪费统计（12 题 × 30 步 = 360 步）

| 浪费类型 | 步数 | 占比 | 说明 |
|---|---|---|---|
| 重复动作被拒绝（⛔） | 108 | 30% | 精确去重 guard 拒绝；但 Agent 换参数写法就绕过（见下行） |
| "记忆命中"重读 | 83 | 23% | 同一函数/区间换个参数写法重读成功，内容没变 |
| 空搜索（符号不存在） | 40 | 11% | 大部分是监督者幻觉建议的符号 |
| **有效调查动作** | **~129** | **36%** | |

**12 道失败题里，64% 的步数是浪费的。** 这不是 marginal 问题——如果浪费减半，等于每题白捡 10 步有效调查。

### 2. 每题的"一步之遥"动作

逐题复盘最重要的发现：**12/12 题都能指出一个具体的、Agent 能力范围内的一步动作，做了就能（大概率）拿到漏引的 gold 文件**：

| 题 | 缺的那一步动作 | 该动作所需的线索出现在 |
|---|---|---|
| 001 | `find_callers(ggml_sycl_set_device)` | step 24 find_callers 结果（被截断的那行） |
| 002 | `find_callers(common_chat_templates_apply)` 或追 `curr_tmpl` 来源 | step 6 自动读取 / step 7 llama.cpp:1224 |
| 007 | `find_callers(calculate_diff_split)` | step 3 list_functions 结果 |
| 010 | `read_function(ggml_backend_cann_reg)` | **初始召回池第 1 名** |
| 013 | `list_files(ggml/src/ggml-cann)` 或 `search_symbol("cache")` | step 17/20 两次 grep 命中 ggml-cann 目录 |
| 014 | `read_function(llama_model_chat_template)` + find_callers | step 8 find_callees 结果 / step 15 源码 |
| 034 | `read_function(~ggml_backend_meta_context)`（需先修工具 bug） | step 5/22 find_callers 结果 |
| 037 | `find_callers(common_chat_verify_template)` | step 11 grep 结果明确列出 arg.cpp |
| 040 | 同 014（`llama_model_chat_template`） | step 4/8 |
| 041 | `find_callees(ggml_sycl_compute_forward)` | step 6 find_callees 结果 |
| 042 | 同 041（dispatch 下沉到 `ggml_sycl_op_set`）或 `list_files(ggml-sycl)` | step 6 / step 27 |
| 044 | `find_callees` 或 read `llama_init_from_model` | step 11/23 已读代码里的调用行 |

**结论：当前系统的瓶颈已经不是"能力"（工具够用、图够全），而是"跟进"（follow-through）——线索出现在工具结果里之后，没有任何机制保证它被沉淀、被记住、被执行。**

### 3. 六大系统性 gap（按影响排序）

**Gap 1：上下文丢失 → 重读-拒绝死循环（12/12 题中招）**
历史窗口只保留最近约 10 步，旧 observation 滑出后 Agent 认为"没读过"，重读被 guard 拒，换个行号区间/参数写法又重读成功（记忆命中）。010 题同一份文件头部用 11 种区间读了 11 次；044 题 14 次拒绝。**Agent 的"已读"状态（在系统侧）和 Agent 自己"记得读过什么"（在可见上下文里）完全脱节。**

**Gap 2：发现但未跟进（9/12 题）**
工具结果里出现了 gold 符号/文件（find_callers 列表、find_callees 列表、grep 文件清单、list_functions 清单），但没有进入任何持久化的"待办"，滑出窗口即消失。014/040 同一条漏链（`llama_model_chat_template`）重复出现 3 次，002/040/014 三题共享同一 gold 三元组全部以同方式失败——**这是可复现的系统性盲区，不是随机失误**。

**Gap 3：监督者幻觉符号（12/12 题，平均 3-4 个不存在符号/题）**
监督者建议的 `switch_device`/`find_template`/`check_jinja_template`/`enqueue_set` 等符号在仓库里不存在，Agent 老实执行搜索，空手而归，再重复搜再被拒。监督者只在"语义层"工作，从不验证符号存在性；且当锚点本身错误时（044），监督者只会强化错误方向（5 次干预全部判"方向正确"）。

**Gap 4：工具 bug——析构函数不可读（001/034 直接因此丢引用）**
`read_function("~ggml_xxx")` 永远 "function not found"。更糟的是升级干预把不可读的析构函数列为"强制待读项"反复推送，制造死锁。

**Gap 5：去重 guard 被参数抖动绕过**
`file_path` vs `file_paths`、列表顺序、行号区间微调（485-509 → 480-515 → 470-520）都能绕过精确匹配去重。guard 只挡住了"完全相同的调用"，挡不住"同一内容的换皮调用"——而后者占浪费的大头（83 步）。

**Gap 6：C 类召回断链（001/013/044 零引用三题 + 042 部分）**
无锚问题（不点名后端/模块）+ 关键词撞车（"set"→set_rows、"上下文初始化"→virtgpu/audio），初始池 0 gold。这是之前已经定位的老问题，本轮复盘确认它仍是零引用题的第一因。**但注意：即便召回失败，001/044 的轨迹里仍然出现过通往 gold 的桥（ggml_sycl_set_device 的名字、llama_init_from_model 的调用行）——召回差不等于没救，是跟进机制没接住。**

### 4. 失败模式的因果链

```
召回无锚（C类题）
   ↓ 召回给了"近义但错误"的锚点
Agent 锚定错误/边缘区域
   ↓ 工具结果里偶尔出现真正的 gold 线索
线索没有持久化（Gap 2），滑出上下文即消失
   ↓ 已读内容也滑出上下文（Gap 1）
Agent 重读 → guard 拒绝/记忆命中重读（Gap 5）→ 步数燃烧
   ↓ 步数越紧，Agent 越焦虑地重复确认
监督者幻觉符号（Gap 3）+ 升级干预强推不可读函数（Gap 4）
   ↓ 30 步耗尽
finish guard 机械拒绝（要求查测试函数/无关调用链）
   ↓
答案基于残缺证据生成，gold 文件漏引
```

### 5. 改进建议（按 ROI 排序）

**P0-1：修上下文丢失（预计可回收 30-50% 浪费步数）**
把"已读函数清单+每个函数的 3-5 行职责摘要+行号"作为**持久系统状态**每步展示（不是放在会被裁剪的历史里）。Agent 看到"读过，摘要在此"就不会重读。这比我们之前做的 frontier 更进一步：不只是"待调查什么"，还有"已调查到什么结论"。

**P0-2：线索跟进清单（"未跟进线索"持久化）**
每次工具结果（find_callers/find_callees/grep/list_functions）解析出的新符号，自动进入"未跟进线索表"，每步展示，读掉才消。升级干预应该推这个表，而不是推"调用点归属失败的条目"。9/12 题的一步之遥动作都在这个表里出现过。

**P0-3：监督者建议符号前过索引验证**
监督者输出 `suggest_keywords` 后，系统用 Neo4j 符号索引做一次存在性检查（毫秒级），不存在的符号直接剔除或替换为近似真实符号。零成本消除 40 步空搜索。

**P1-1：修析构函数读取 + 升级干预推送前验证可读性**
read_function 对 `~xxx` 做名称规范化（Neo4j 里析构函数的存储名可能需要匹配 `~` 前缀或类名）。推送待读项前先确认该函数可读。

**P1-2：去重 guard 按内容指纹而非参数文本**
同一（函数, 文件）不论参数写法都算重复，直接返回缓存内容但**不计步数**（或计 0 成本）——重读本身不是罪，罪是重读还消耗预算还打乱节奏。read_lines 做区间包含判定。

**P1-3：finish guard 收敛**
"存在未读调用点 ≠ 调用点对问题有价值"（007 要求查测试函数的调用方）。改为证据充分性判断：相关函数的调用链查过 + 无高价值未跟进线索即可 finish。

**P2：C 类召回（无锚问题）**
本轮复盘确认：召回差的三题（001/013/044）里，001/044 其实靠"跟进机制"就能救回（桥出现过）。真正纯召回无解的只有 013（gold 函数从未以任何形式出现在任何工具结果里——不对，step 17 grep reallocate 命中过 ggml-cann 目录，只是没进去）。**结论修正：先做 P0 的跟进/记忆，C 类题会有一部分被"救"回来，剩下的再专门攻召回。**

### 6. 一句话总结

**现在的系统不是"找不到"，是"找到了没记住、记住了没跟进、跟进了被打断"。12 道失败题每题都有一个出现在视野里却没被执行的关键动作；64% 的步数浪费在重复确认上已经读过的东西。下一步最高 ROI 的不是召回、不是新工具、不是更大的图——是把"调查状态"变成 Agent 永远可见、永不过期的一等公民。**
