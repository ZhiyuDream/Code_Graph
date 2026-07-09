# Selection Failure Taxonomy (50 questions)

Based on decomposition: `results/retrieval_selection_decomposition_0_50.json`
and gold rank analysis: `results/gold_rank_in_pool_0_50.json`.

## Aggregate Statistics

| Category | Count | Rate |
|----------|-------|------|
| correct | 26 | 52.0% |
| retrieval_miss | 12 | 24.0% |
| same_file | 7 | 14.0% |
| same_module | 2 | 4.0% |
| abstraction_shift | 2 | 4.0% |
| test_or_utility | 1 | 2.0% |

## Per-Question Detail

| QA ID | Gold Rank | Category | Gold Symbol(s) | LLM Selected | Detail |
|-------|-----------|----------|----------------|--------------|--------|
| posthoc_public_001 | MISS | retrieval_miss | ggml_sycl_set_device | `select_device` | gold symbol not in candidate pool |
| posthoc_public_002 | MISS | retrieval_miss | llama_model_chat_template | `llm_chat_detect_template` | gold symbol not in candidate pool |
| posthoc_public_003 | 13 | correct | llama_model_default_params | `llama_model_default_params` | LLM selected gold symbol |
| posthoc_public_004 | 6 | test_or_utility | trim_whitespace | `test_calculate_diff_split_empty_cases` | test_calculate_diff_split_empty_cases is test/utility function |
| posthoc_public_005 | 5 | correct | trim_leading_whitespace | `trim_leading_whitespace` | LLM selected gold symbol |
| posthoc_public_006 | 1 | correct | trim_trailing_whitespace | `trim_trailing_whitespace` | LLM selected gold symbol |
| posthoc_public_007 | 20 | correct | calculate_diff_split | `calculate_diff_split` | LLM selected gold symbol |
| posthoc_public_008 | 38 | correct | until_common_prefix | `until_common_prefix` | LLM selected gold symbol |
| posthoc_public_009 | 1 | correct | segmentize_markers | `segmentize_markers` | LLM selected gold symbol |
| posthoc_public_010 | 1 | same_file | ggml_backend_cann_reg | `get_reg` | get_reg in same file as gold |
| posthoc_public_011 | 1 | correct | common_sampler_init | `common_sampler_init` | LLM selected gold symbol |
| posthoc_public_012 | 28 | same_file | llama_model_chat_template | `common_chat_templates_apply` | common_chat_templates_apply in same file as gold |
| posthoc_public_013 | MISS | retrieval_miss | - | `llama_kv_cache::update` | gold symbol not in candidate pool |
| posthoc_public_014 | 54 | same_file | llama_model_chat_template | `common_chat_verify_template` | common_chat_verify_template in same file as gold |
| posthoc_public_015 | 1 | correct | common_get_model_endpoint | `common_get_model_endpoint` | LLM selected gold symbol |
| posthoc_public_016 | 1 | correct | common_list_cached_models | `common_list_cached_models` | LLM selected gold symbol |
| posthoc_public_017 | 2 | same_file | common_params_to_map | `common_preset_context::load_from_args` | common_preset_context::load_from_args in same file as gold |
| posthoc_public_018 | 2 | correct | common_get_model_endpoint | `common_get_model_endpoint` | LLM selected gold symbol |
| posthoc_public_019 | 6 | correct | llama_model_default_params | `llama_model_default_params` | LLM selected gold symbol |
| posthoc_public_020 | 1 | correct | common_sampler_types_from_names | `common_sampler_types_from_names` | LLM selected gold symbol |
| posthoc_public_021 | 24 | correct | trim_whitespace | `trim_whitespace` | LLM selected gold symbol |
| posthoc_public_022 | 3 | correct | trim_leading_whitespace | `trim_leading_whitespace` | LLM selected gold symbol |
| posthoc_public_023 | 1 | correct | trim_trailing_whitespace | `trim_trailing_whitespace` | LLM selected gold symbol |
| posthoc_public_024 | 5 | same_file | until_common_prefix | `common_prefix_len` | common_prefix_len in same file as gold |
| posthoc_public_025 | 1 | correct | segmentize_markers | `segmentize_markers` | LLM selected gold symbol |
| posthoc_public_026 | 3 | correct | prune_whitespace_segments | `prune_whitespace_segments` | LLM selected gold symbol |
| posthoc_public_027 | 8 | same_module | build_chat_peg_parser | `common_peg_parser_builder::build` | common_peg_parser_builder::build in same module as gold (common) |
| posthoc_public_028 | 10 | correct | calculate_diff_split | `calculate_diff_split` | LLM selected gold symbol |
| posthoc_public_029 | 2 | same_file | common_params_to_map | `common_preset_context::load_from_args` | common_preset_context::load_from_args in same file as gold |
| posthoc_public_030 | 1 | correct | after_common_suffix | `after_common_suffix` | LLM selected gold symbol |
| posthoc_public_031 | 13 | abstraction_shift | ggml_backend_cann_init | `apir_backend_initialize` | apir_backend_initialize appears more generic/specific abstraction |
| posthoc_public_032 | MISS | retrieval_miss | - | `ggml_backend_registry` | gold symbol not in candidate pool |
| posthoc_public_033 | MISS | retrieval_miss | - | `common_sampler_sample` | gold symbol not in candidate pool |
| posthoc_public_034 | MISS | retrieval_miss | ggml_backend_free | `ggml_backend_sched_free` | gold symbol not in candidate pool |
| posthoc_public_035 | 6 | correct | segmentize_markers | `segmentize_markers` | LLM selected gold symbol |
| posthoc_public_036 | 4 | same_module | common_ngram_map_begin | `common_ngram_cache_update` | common_ngram_cache_update in same module as gold (common) |
| posthoc_public_037 | 1 | correct | common_chat_verify_template | `common_chat_verify_template` | LLM selected gold symbol |
| posthoc_public_038 | MISS | retrieval_miss | - | `common_params_handle_model` | gold symbol not in candidate pool |
| posthoc_public_039 | 9 | correct | common_get_model_endpoint | `common_get_model_endpoint` | LLM selected gold symbol |
| posthoc_public_040 | MISS | retrieval_miss | llama_model_chat_template | `common_chat_templates_apply` | gold symbol not in candidate pool |
| posthoc_public_041 | 37 | abstraction_shift | ggml_sycl_count_equal | `ggml_backend_sched_graph_compute_async` | ggml_backend_sched_graph_compute_async appears more generic/specific abstraction |
| posthoc_public_042 | MISS | retrieval_miss | ggml_sycl_op_set | `set_rows_sycl_q` | gold symbol not in candidate pool |
| posthoc_public_043 | 1 | correct | common_download_model | `common_download_model` | LLM selected gold symbol |
| posthoc_public_044 | MISS | retrieval_miss | - | `common_reasoning_budget_init` | gold symbol not in candidate pool |
| posthoc_public_045 | 8 | same_file | parse_cpu_range | `postprocess_cpu_params` | postprocess_cpu_params in same file as gold |
| posthoc_public_046 | 28 | correct | cpu_get_num_physical_cores | `cpu_get_num_physical_cores` | LLM selected gold symbol |
| posthoc_public_047 | 1 | correct | postprocess_cpu_params | `postprocess_cpu_params` | LLM selected gold symbol |
| posthoc_public_048 | 1 | correct | common_get_model_endpoint | `common_get_model_endpoint` | LLM selected gold symbol |
| posthoc_public_049 | MISS | retrieval_miss | - | `common_arg::to_string` | gold symbol not in candidate pool |
| posthoc_public_050 | MISS | retrieval_miss | - | `llama_sampler_init_grammar_impl` | gold symbol not in candidate pool |