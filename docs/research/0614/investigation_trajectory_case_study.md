# 调查轨迹 Case Study

共分析 15 题，选取最有代表性的案例。

## 案例 1: posthoc_public_004 (Entry coverage: 100%)

**问题**: AI 抽了字符串首尾空白裁剪逻辑，我担心调用点对空字符串、全空白字符串和无副作用返回值的假设不一致。帮我看这些调用方是否仍能按相同契约使用裁剪结果？

**Gold files**: common/chat-auto-parser-helpers.cpp, common/chat-diff-analyzer.cpp

**入口文件**: common/chat-auto-parser-helpers.cpp

**访问轨迹**: common/chat-auto-parser-helpers.cpp -> common/chat-diff-analyzer.cpp -> common/chat-auto-parser-helpers.h -> common/chat-auto-parser.cpp -> common/util.cpp -> common/chat-auto-parser.h

**关键方向改变**:

- Step 1: 实现契约是明确的纯函数式返回值，没有就地修改或额外状态。现在需要检查调用方是否误以为这些函数会保留非空返回值、修改原对象或产生其他副作用，特别是 prune_whitespace_segments 的调用场景。因此搜索转向这些函数的调用点。
  - 新证据: 在 common/chat-auto-parser-helpers.cpp 中实现了 trim_whitespace、trim_leading_whitespace、trim_trailing_whitespace、trim_trailing_newlines 和 prune_whitespace_segments。这些函数均接受字符串视图，返回修剪后的 std::string，对空字符串和只含空白字符的字符串返回空字符串，且没有任何副作用。prune_whitespace_segments 同样基于前几个函数构建，结果也是纯字符串变换。
  - 下一目标: prune_whitespace_segments 和其他裁剪函数的调用者，特别是在 diff 分析相关代码中的用法。
  - 动作: read_file:common/chat-diff-analyzer.cpp
- Step 2: 这说明 analyze_base 和 build_tool_call 的契约依赖于输入字符串已被修剪。修剪逻辑若被移除，这些相等比较会失效。因此需要追溯负责修剪的函数（如 prune_whitespace_segments 或 trim_whitespace）及其调用点，确认它们是否仍为 arguments 等字段提供修剪保证。接下来应从声明开始查看修剪函数的接口和实现。
  - 新证据: 在 common/chat-diff-analyzer.cpp 的 analyze_base 中，call_id.prefix/suffix 与 arguments.start/end 使用严格相等比较（如 `if (call_id.prefix == arguments.end) { call_id.prefix = ""; }`），未调用任何 trim 函数；该逻辑隐含分隔符已在之前被修剪的假设。build_tool_call 同样依赖无首尾空白的 call_id.prefix/suffix 进行最终格式构建。
  - 下一目标: prune_whitespace_segments 或 trim_whitespace 的声明与实现
  - 动作: read_file:common/chat-auto-parser-helpers.h
- Step 3: 由于头文件中没有明确的空白处理契约，而 prune_whitespace_segments 的逻辑似乎建立在 trim 函数能一致识别全空白字符串的基础上，因此必须查看 trim 函数的实现以及 prune_whitespace_segments 的完整定义，以核实是否存在不一致的假设。
  - 新证据: common/chat-auto-parser-helpers.h 声明了 trim_whitespace、trim_leading_whitespace、trim_trailing_whitespace、trim_trailing_newlines 四个按值返回 std::string 的裁剪函数，但未在头文件中说明对空字符串或全空白字符串的返回值契约。prune_whitespace_segments 的注释描述了移除仅含空白（如空格、换行）的 TEXT 段，可能依赖上述 trim 函数进行空白判定。
  - 下一目标: trim_whitespace 函数实现与 prune_whitespace_segments 的实现逻辑
  - 动作: read_file:common/chat-auto-parser.cpp
- Step 4: 因目标文件缺失，无法直接分析该文件中的裁剪实现，必须转向其他可能包含这些函数定义的文件。从候选队列看，common/util.cpp 最可能包含通用字符串裁剪函数 trim_whitespace，因此下一步应检查该文件。
  - 新证据: 目标文件 common/chat-auto-parser.cpp 不存在，无法读取其内容。候选符号列表为空，未提供任何裁剪函数（trim_whitespace、prune_whitespace_segments）的实现或调用点信息。
  - 下一目标: trim_whitespace 函数和 prune_whitespace_segments 函数的定义或实现
  - 动作: read_file:common/util.cpp
- Step 5: LLM 返回异常，fallback 选择 frontier 第一个文件
  - 新证据: 
  - 下一目标: trim_whitespace 函数和 prune_whitespace_segments 函数的定义或实现
  - 动作: read_file:common/chat-auto-parser.h

## 案例 2: posthoc_public_005 (Entry coverage: 100%)

**问题**: AI 改了只裁剪字符串开头空白的逻辑，我担心调用方把它和完整裁剪混用。帮我确认现有使用点是否只依赖前缀空白被移除，而不是期待尾部也被处理？

**Gold files**: common/chat-auto-parser-helpers.cpp, common/chat-diff-analyzer.cpp

**入口文件**: common/chat-auto-parser-helpers.cpp

**访问轨迹**: common/chat-auto-parser-helpers.cpp -> common/chat-auto-parser-helpers.h -> common/chat-auto-parser.cpp -> tests/test-chat-auto-parser.cpp -> common/chat-diff-analyzer.cpp -> common/chat-template.cpp

**关键方向改变**:

- Step 1: 由于实现文件中未找到调用点，下一步需要检查头文件中的声明或内联使用，以及另一个候选源文件中是否存在调用。
  - 新证据: 在 common/chat-auto-parser-helpers.cpp 中没有发现任何对 trim_whitespace 或 prune_whitespace_segments 的调用，只列出了可疑符号列表。
  - 下一目标: 查找 trim_whitespace 和 prune_whitespace_segments 的声明和所有调用位置
  - 动作: read_file:common/chat-auto-parser-helpers.h
- Step 2: 既然存在两种裁剪语义，需要查看调用这些函数的代码，确认调用方是期望全裁剪还是仅前缀裁剪，从而判断 AI 改动是否会造成尾部空白残留问题。因此搜索方向转向所有引用 trim_whitespace、trim_leading_whitespace、prune_whitespace_segments 的实现与调用位置。
  - 新证据: 头文件 common/chat-auto-parser-helpers.h 同时声明了完整的 trim_whitespace 函数和仅处理前导空白的 trim_leading_whitespace 函数，并且 prune_whitespace_segments 也依赖空白判定逻辑，说明系统中明确存在前缀裁剪与全裁剪两种语义。
  - 下一目标: trim_whitespace、trim_leading_whitespace 和 prune_whitespace_segments 的调用点
  - 动作: read_file:common/chat-auto-parser.cpp
- Step 3: 目标文件缺失意味着无法直接从定义处追踪调用关系，因此必须从其他候选文件中搜索这些函数的实际调用点来收集使用证据。
  - 新证据: 文件 common/chat-auto-parser.cpp 在当前环境中不存在，无法查看其中 trim_whitespace 或 prune_whitespace_segments 的实现或调用记录。
  - 下一目标: trim_whitespace、trim_leading_whitespace 和 prune_whitespace_segments 的调用点
  - 动作: read_file:tests/test-chat-auto-parser.cpp
- Step 4: 由于测试未直接调用修剪函数，需要追溯测试所调用的底层实现（如 calculate_diff_split），以确认它们对尾部空白移除的依赖程度。因此搜索方向从测试文件转向这些核心分割函数的定义文件。
  - 新证据: 在 tests/test-chat-auto-parser.cpp 中没有直接出现 trim_whitespace、trim_leading_whitespace 或 prune_whitespace_segments 的调用。但文件中存在大量针对 calculate_diff_split、test_until_common_prefix_basic、test_after_common_suffix_basic 等函数/测试，这些测试的名称暗示它们依赖“公共前缀/后缀”分割操作，这类操作很可能间接调用了字符串首部或尾部的空白修剪函数。
  - 下一目标: calculate_diff_split 函数及其依赖的修剪逻辑
  - 动作: read_file:common/chat-diff-analyzer.cpp
- Step 5: 该文件不直接修剪空白，但可疑符号 analyze_base 等可能隐含依赖完整修剪。需要检查调用这些函数的上层模块（如模板应用或聊天处理）来确认调用方是否假设尾部空白已被去除。
  - 新证据: chat-diff-analyzer.cpp 中未直接调用 trim_whitespace 或 prune_whitespace_segments；包含 analyze_base、build_tool_call、mode_to_str 定义；辅助函数 find_last_marker、find_first_marker 基于 PEG 标记提取，不显式要求尾部空白已移除；但所处理的 prefix/suffix 字符串可能来自模板解析，外部修剪空白逻辑的变更可能影响这些函数。
  - 下一目标: analyze_base / build_tool_call 的调用链（在 chat-template.cpp 或 chat.cpp 中）
  - 动作: read_file:common/chat-template.cpp

## 案例 3: posthoc_public_010 (Entry coverage: 100%)

**问题**: AI 生成了一个后端注册入口，我担心静态初始化、互斥保护和重复调用时的注册状态不一致。帮我看这个注册流程是否符合现有后端获取路径的使用假设？

**Gold files**: ggml/src/ggml-backend-reg.cpp, ggml/src/ggml-cann/ggml-cann.cpp

**入口文件**: ggml/src/ggml-cann/ggml-cann.cpp

**访问轨迹**: ggml/src/ggml-cann/ggml-cann.cpp -> ggml/src/ggml-backend-reg.cpp -> ggml/src/ggml-backend.cpp -> ggml/src/ggml-backend-cann.cpp -> ggml/src/ggml-cann.cpp -> ggml/include/ggml-cann.h

**关键方向改变**:

- Step 1: 注册入口的具体实现可能位于通用后端注册文件或头文件中，因此搜索方向应从具体后端源文件转向注册机制实现或接口声明。
  - 新证据: 在已访问的 ggml-cann.cpp 中未找到与注册入口 ggml_backend_cann_reg、静态初始化、互斥保护或设备状态获取路径相关的实现细节。
  - 下一目标: ggml_backend_cann_reg 的定义及后端注册的静态初始化、互斥保护逻辑
  - 动作: read_file:ggml/src/ggml-backend-reg.cpp
- Step 2: 全局注册表修改操作缺乏同步和去重，意味着静态初始化和动态加载在并发或重复调用下会出现竞争和重复注册。下一步需要验证实际调用 ggml_backend_cann_reg 及 ggml_backend_load_all 的代码路径，确认是否存在重复调用或并发场景，以判断是否触发不一致。
  - 新证据: ggml_backend_registry 构造函数通过编译宏无条件注册静态后端，调用 register_backend 但不检查重复；动态加载路径 load_backend 和 ggml_backend_load_best 直接向全局向量追加后端和设备，无互斥锁或去重保护；get_reg() 返回局部静态对象，其初始化线程安全，但后续可变操作均未加锁；ggml_backend_load_all 等可被重复调用，每次都会尝试加载相同动态库并再次注册，可能造成同一后端多次注册，导致设备列表和注册表状态不一致。
  - 下一目标: ggml_backend_load_all 和 ggml_backend_cann_reg 的调用者及使用模式
  - 动作: read_file:ggml/src/ggml-backend.cpp
- Step 3: 这进一步强化了对互斥保护和状态一致性的担忧。为了判断 cann_reg 是否受这些缺陷影响，需要查看 cann 后端注册时如何获取或关联这些 buffer type，并验证是否在并发路径上被调用。因此搜索方向转向 cann 后端注册的具体实现。
  - 新证据: 在 ggml-backend.cpp 中，ggml_backend_cpu_buffer_type 和 ggml_backend_cpu_buffer_from_ptr_type 使用函数局部静态变量实现单例，但无显式互斥锁，且依赖 C++11 静态初始化线程安全（可能在 C 环境下丢失）。这些单例的 device 字段被设为 NULL 并有 FIXME 注释指出应从注册设备获取，表明设备关联未完成。整个文件未出现任何注册相关的互斥保护原语。
  - 下一目标: ggml_backend_cann_reg 的实现及其内部调用的 buffer type 初始化逻辑
  - 动作: read_file:ggml/src/ggml-backend-cann.cpp
- Step 4: 这表明 CANN 后端的 buffer type 初始化与注册逻辑很可能直接写在 ggml/src/ggml-cann.cpp 中，而不是独立的 ggml-backend-cann.cpp。因此搜索方向从“不存在的后端注册文件”转向实际存在的 ggml-cann.cpp，并重点查找 ggml_backend_cann_reg 的实现。
  - 新证据: 尝试读取 ggml/src/ggml-backend-cann.cpp 失败，该文件不存在。CANN 后端的注册入口未存在于预期路径。
  - 下一目标: ggml_backend_cann_reg 函数及其内部调用的 buffer type 初始化逻辑，位于 ggml/src/ggml-cann.cpp
  - 动作: read_file:ggml/src/ggml-cann.cpp
- Step 5: 由于预期实现文件缺失，需要转向其他可能包含 ggml_backend_cann_reg 定义或声明的候选文件，以定位实际实现位置。
  - 新证据: 目标文件 ggml/src/ggml-cann.cpp 不存在，无法审查其内容；候选符号为空，未发现任何函数或变量。
  - 下一目标: ggml_backend_cann_reg 函数的声明或定义，以及 buffer type 初始化逻辑。
  - 动作: read_file:ggml/include/ggml-cann.h

## 案例 4: posthoc_public_013 (Entry coverage: 100%)

**问题**: AI 改了缓存扩容和重新分配逻辑，我担心旧 buffer 释放后重新申请失败，会让容量记录和真实指针状态对不上。帮我查一下成功扩容和分配失败路径是否都处理清楚？

**Gold files**: ggml/src/ggml-cann/aclnn_ops.cpp

**入口文件**: ggml/src/ggml-cann/aclnn_ops.cpp

**访问轨迹**: ggml/src/ggml-cann/aclnn_ops.cpp -> ggml/src/ggml-cann/common.h -> ggml/src/ggml-cann/pool.cpp -> ggml/src/ggml-cann/ggml-cann.cpp -> ggml/src/ggml-cann/common.cpp -> ggml/src/ggml-cann.cpp

**关键方向改变**:

- Step 1: aclnn_ops.cpp 只展示了缓存操作的调用点，未揭示扩容成功时容量记录是否更新，以及分配失败时容量是否保持不变。需要转入 ggml_cann_pool_alloc 等函数的实现，这些实现可能定义在候选队列的 common.h 中。
  - 新证据: 在 ggml/src/ggml-cann/aclnn_ops.cpp 中未找到直接处理缓存扩容后容量记录与实际 buffer 指针一致性的代码。文件中使用了 get_cache_acl_tensor、ggml_cann_pool_alloc、cann_copy 等符号，但容量字段的更新逻辑未在该文件显露。
  - 下一目标: ggml_cann_pool_alloc 函数实现及其容量记录逻辑
  - 动作: read_file:ggml/src/ggml-cann/common.h
- Step 2: 容量记录逻辑的细节（尤其是扩容和分配失败后对 cache/size 或 ptr/actual_size 的回滚）必须到具体实现文件中查看，因此搜索方向从接口声明转向具体实现 pool.cpp。
  - 新证据: common.h 中定义了 ggml_cann_tensor_cache 结构体直接持有 void* cache 和 int64_t size，没有封装扩容或失败回滚逻辑；ggml_cann_pool_alloc 作为 RAII 记录 pool、ptr 和 actual_size，分配失败时状态需调用方额外处理；抽象基类 ggml_cann_pool 只声明 alloc/free 接口，实际扩容与重分配失败处理在头文件中不可见。
  - 下一目标: ggml_cann_pool_alloc 和 ggml_cann_pool 子类的 alloc/free 实现，尤其扩容、重分配失败路径中对容量记录的处理。
  - 动作: read_file:ggml/src/ggml-cann/pool.cpp
- Step 3: 因为 pool.cpp 缺失，原以为的独立内存池实现可能合并到了 ggml-cann.cpp 或 common.cpp 中。搜索方向从直接读取 pool.cpp 转向同目录下的主后端文件，定位 ggml_cann_pool_alloc 及其扩容/失败路径逻辑。
  - 新证据: 目标文件 ggml/src/ggml-cann/pool.cpp 在当前环境中不存在，无法提取容量记录、扩容或重分配失败路径的代码证据。候选文件队列显示同目录下存在 ggml-cann.cpp 和 common.cpp，这些文件可能包含实际的内存池分配与释放实现。
  - 下一目标: ggml_cann_pool_alloc 及相关 alloc/free 函数，容量更新与错误恢复逻辑
  - 动作: read_file:ggml/src/ggml-cann/ggml-cann.cpp
- Step 4: 关键函数符号已出现但实现缺失，无法验证错误恢复路径，因此必须转向这些函数的定义文件来检查容量更新与回滚逻辑。
  - 新证据: 在 ggml-cann.cpp 中检测到 ggml_cann_host_malloc、realloc、free、get_or_create_tracker 等候选符号，但当前展示的代码片段未包含这些函数的实现，无法判断扩容成功或分配失败时容量记录的更新情况。
  - 下一目标: ggml_cann_host_malloc、realloc 及相关 tracker 的实现，重点检查扩容的容量记录更新和分配失败时的回退处理
  - 动作: read_file:ggml/src/ggml-cann/common.cpp
- Step 5: 由于目标文件缺失，无法验证成功/失败路径的处理；需要转向其他可能包含 ggml_cann_host_malloc 和 realloc 实现的文件，特别是 ggml/src/ggml-cann.cpp 或 ggml/src/ggml.c，以检查扩容的容量记录更新和分配失败时的回退逻辑。
  - 新证据: 指定的审计文件 ggml/src/ggml-cann/common.cpp 不存在，无法直接检查缓存扩容和重新分配逻辑。
  - 下一目标: ggml_cann_host_malloc 和相关 realloc/tracker 函数的实现
  - 动作: read_file:ggml/src/ggml-cann.cpp

## 案例 5: posthoc_public_002 (Entry coverage: 67%)

**问题**: AI 生成了聊天模板选择逻辑，我担心空模板、缺失模板和调用方 fallback 判断被混在一起。帮我看返回值语义是否和现有调用方的判断方式一致？

**Gold files**: common/chat.cpp, common/common.cpp, src/llama-model.cpp

**入口文件**: src/llama-model.cpp

**访问轨迹**: src/llama-model.cpp -> common/chat.cpp -> common/common.cpp -> common/templates.cpp -> common/chat.h

**关键方向改变**:

- Step 1: 无法从实现文件判断返回值是nullptr还是fallback字符串，因此转向检查调用方对返回值的判断逻辑，以确认是否存在混用。
  - 新证据: 在src/llama-model.cpp中未发现关于llama_model_chat_template返回值语义的具体事实，key_facts为空。
  - 下一目标: 调用方中针对llama_model_chat_template返回值的判断逻辑，特别是common/chat.cpp
  - 动作: read_file:common/chat.cpp
- Step 2: 当前文件未展示 llama_model_chat_template 的调用点，而 common_chat_template_direct_apply 和 common_chat_try_specialized_template 极有可能是封装该调用的关键函数，其返回值语义直接决定 template_default 的设置。因此需要查看这些函数的实现，以弄清它们如何区分空模板、缺失模板与 fallback。
  - 新证据: 在 common/chat.cpp 中，common_chat_templates_apply 无条件解引用 tmpls->template_default，要求上层已将其设为非空；但文件中并未看到对 llama_model_chat_template 的直接调用，模板选择逻辑集中在 common_chat_template_direct_apply 与 common_chat_try_specialized_template 等符号上。此外，多个 common_chat_params_init_* 函数可能各自重复处理模板，易出现判断不一致。
  - 下一目标: common_chat_template_direct_apply 与 common_chat_try_specialized_template 的实现
  - 动作: read_file:common/chat.cpp
- Step 3: common_chat_templates_apply 的强依赖表明模板选择逻辑必须保证 template_default 非空，而多个 init 函数的分散处理增加了不一致风险。需要立刻查看负责模板选择与 fallback 的两个关键函数：common_chat_template_direct_apply 和 common_chat_try_specialized_template，以确定其返回值是否区分 nullptr 与空字符串，以及调用方如何利用这些返回值。
  - 新证据: 在 common/chat.cpp 中，common_chat_templates_apply 无条件解引用 tmpls->template_default，若该指针为 nullptr 将直接崩溃；未在当前片段中发现 llama_model_chat_template 的直接调用；代码中存在多个 common_chat_params_init_* 初始化函数，可能各自处理模板逻辑；common_chat_msg::to_json_oaicompat 将空 content 统一设为空字符串，反映出空字符串与“无内容”的字面量混用模式。
  - 下一目标: common_chat_template_direct_apply 与 common_chat_try_specialized_template 的实现
  - 动作: read_file:common/common.cpp
- Step 4: common/common.cpp 中没有目标函数的实现，且证据强化了模板返回值可能为 std::string 的假设，因此需要转向模板处理专用文件来定位 common_chat_template_direct_apply 与 common_chat_try_specialized_template 的真实实现和返回值语义。
  - 新证据: common/common.cpp 中的 common_control_vector_load 使用 n_embd=-1 哨兵值指示错误，未使用空指针或空容器；文件中未出现 llama_model_chat_template 的定义或调用，但广泛使用 string_format、string_from 等字符串工具，暗示模板可能以 std::string 返回，缺失时可能为空字符串；common_prompt_batch_decode 通过 bool 返回值区分成功/失败，不依赖 nullptr 表示缺失。
  - 下一目标: common_chat_template_direct_apply 和 common_chat_try_specialized_template 的实现（可能在 common/templates.cpp 或 common/chat.cpp 的未深入部分）
  - 动作: read_file:common/templates.cpp
- Step 5: 由于该文件缺失，无法直接验证返回值语义。需要转向其他可能包含这些函数定义的文件，首先从 common/chat.h 查找声明，再定位实现文件。
  - 新证据: 目标文件 common/templates.cpp 在当前环境中缺失，未能读取到任何代码。已访问文件中未发现 common_chat_template_direct_apply 和 common_chat_try_specialized_template 的定义。
  - 下一目标: common_chat_template_direct_apply 和 common_chat_try_specialized_template 的声明或定义位置
  - 动作: read_file:common/chat.h

## 主要观察

- 4/15 题在 Entry-only 条件下达到 100% coverage。
- 所有题目均出现至少一次方向改变（decision_impact）。
- 高 coverage 案例的共同特点：入口文件直接包含核心实现，且调用点与入口在同一模块内。
- 低 coverage 案例的主要问题：Agent 能访问到部分 gold files，但答案引用不完整，或继续追错了方向。
