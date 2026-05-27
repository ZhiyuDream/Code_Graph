# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Code_Graph builds a code graph from C++ repositories (primarily llama.cpp) using clangd LSP, stores it in Neo4j, and answers repository-level questions via a ReAct-based retrieval agent. The primary metric is accuracy (currently ~54% on llamacpp_benchmark_v2).

## Setup

```bash
conda create -n code_graph python=3.11 && conda activate code_graph
pip install -r requirements.txt
# clangd 20+ must be installed at the system level (older versions produce empty CALLS edges)
```

All configuration is in `.env` (loaded by `config.py` via python-dotenv). Required: `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `REPO_ROOT`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`. Never commit `.env` -- it contains real credentials.

## Commands

```bash
# Pipeline stages (run sequentially)
python scripts/run_stage1.py              # Build code graph in Neo4j via clangd
python scripts/run_stage2.py              # Discover workflows (BFS on CALLS edges)
python scripts/run_stage3.py              # Fetch GitHub Issues/PRs

# QA pipeline (main entry point)
python scripts/run_qa_final.py --csv datasets/llama_cpp_QA_cleaned.json --output results/output.json --workers 20

# Evaluation
python tools/eval_benchmark.py eval -i results/output.json -o results/output_evaluated.json -w 20
python tools/eval_benchmark.py compare -b baseline.json -n new_result.json

# Tests
pytest tests/

# Debug a single question
python scripts/debug_single_question.py
```

## Architecture

### Project Layout

```
config.py               # Central config loader (from .env)
src/                    # All importable library code
  pipeline/             # Stage 1 clangd-based graph building
  core/                 # Infrastructure: neo4j_client, llm_client, prompt_loader, answer_generator
  search/               # Retrieval: semantic_search, call_chain, grep_search_v2, code_reader
  qa/                   # QA agent (agent.py) and classic RAG (classic_rag.py)
  workflow/             # Workflow discovery (entry_candidates)
  neo4j_writer.py       # Shared Neo4j constraint/write utilities
scripts/                # CLI entry points (run_stage*.py, run_qa_final.py, eval tools)
experiments/            # Experiment scripts and results
prompts/                # LLM prompt templates (loaded by src/core/prompt_loader.py)
datasets/               # QA benchmarks
tests/                  # Unit tests
data/                   # RAG indexes (gitignored, ~1.1GB)
```

### Pipeline (src/pipeline/) -- Graph Building

Orchestrated by `stage1_clangd.py`:

`LSPClient` (clangd JSON-RPC) -> `symbol_extractor` (documentSymbol + callHierarchy) -> `field_resolver` -> `call_resolver` (RawCall -> precise caller/callee pairs) -> `graph_assembler` (builds nodes/edges dict with Louvain community detection for Modules) -> `neo4j_batch_writer` (UNWIND batch write, 500/batch)

Key data models in `src/pipeline/models.py`: `FunctionSymbol`, `ClassSymbol`, `VariableSymbol`, `RawCall`, `FileResult`, `ResolvedCalls`.

### Neo4j Graph Model

Node types: Repository, Directory, File, Function, Class, Variable, Attribute, Module, Issue, PullRequest, Workflow

Edge types: CONTAINS, CALLS, CALLS_AMBIGUOUS, REFERENCES_VAR, HAS_MEMBER, HAS_METHOD, BELONGS_TO, MODULE_CALLS, FIXES, WORKFLOW_ENTRY, PART_OF_WORKFLOW, CHANGED_IN, MENTIONS

### src/core/ -- Infrastructure Layer

- `neo4j_client.py` -- Singleton driver, `run_cypher()`, `run_cypher_single()`
- `llm_client.py` -- Multi-provider LLM (OpenAI/DeepSeek), `call_llm()` with retry, `call_llm_json()` with json_repair
- `answer_generator.py` -- `build_context()` + `generate_answer()`
- `prompt_loader.py` -- Loads templates from `prompts/` directory
- `react_actions.py` -- ReAct action registry and executor

### src/search/ -- 3-Layer Search

1. `semantic_search.py` -- Embedding-based search using precomputed RAG index (`data/qa_embedding_index.json`)
2. `call_chain.py` -- Neo4j graph traversal (expand callers/callees)
3. `grep_search_v2.py` -- Keyword-based code search with grep

Other: `file_neighbors.py` (same-file/same-class expansion), `issue_search.py` (GitHub Issues/PRs), `code_reader.py` (read source from files), `frequency_penalty.py` (high-freq function filtering).

### ReAct Agent Loop (scripts/run_qa_final.py)

Iterative retrieval with up to 5 rounds:
1. **Initial search**: Semantic search with grep fallback (threshold 0.5)
2. **Decide**: LLM picks `expand_callers`, `expand_callees`, or `sufficient` (prompt from `prompts/react_decide*.txt`)
3. **Expand**: Neo4j call chain expansion; stops early on diminishing returns (2 rounds with gain <= 1)
4. **Answer**: Generate final answer via `generate_answer()`

## Conventions

- Prompts are file-based in `prompts/`, loaded at runtime by `src/core/prompt_loader.py`. Edit prompt files, not inline strings.
- Parallelism: Use `ThreadPoolExecutor` with `--workers 20` for QA and evaluation.
- Experiment results go in `results/` (gitignored). Record improvements in `design/` with good/bad cases.
- Documentation and comments are primarily in Chinese.
- LLM calls go through `src/core/llm_client.py` -- never call the OpenAI SDK directly.
- Neo4j queries go through `src/core/neo4j_client.py` -- never create drivers directly.
