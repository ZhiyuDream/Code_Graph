"""
Tests for src/ingestion/resource_lifecycle_extractor.py
"""
from __future__ import annotations

import pytest

from src.ingestion.resource_lifecycle_extractor import (
    ResourceOperation,
    extract_resource_lifecycle_for_function,
)


class TestExtractResourceLifecycle:
    """Tests for resource lifecycle extraction."""

    def test_new_delete(self):
        """提取 new/delete 配对。"""
        lines = [
            "void foo() {",
            "    int * p = new int(10);",
            "    use(p);",
            "    delete p;",
            "}",
        ]
        ops = extract_resource_lifecycle_for_function(
            "f:foo", "a.cpp", 1, 5, lines,
        )

        assert len(ops) == 2
        alloc = [o for o in ops if o.type == "allocate"]
        release = [o for o in ops if o.type == "release"]
        assert len(alloc) == 1
        assert len(release) == 1
        assert alloc[0].resource_type == "memory"
        assert alloc[0].variable_name == "p"
        assert release[0].variable_name == "p"
        # 配对
        assert alloc[0].paired_operation_id == release[0].id
        assert release[0].paired_operation_id == alloc[0].id

    def test_malloc_free(self):
        """提取 malloc/free 配对。"""
        lines = [
            "void bar() {",
            "    void * buf = malloc(1024);",
            "    memcpy(buf, src, 1024);",
            "    free(buf);",
            "}",
        ]
        ops = extract_resource_lifecycle_for_function(
            "f:bar", "a.cpp", 1, 5, lines,
        )

        assert len(ops) == 2
        alloc = [o for o in ops if o.type == "allocate"]
        release = [o for o in ops if o.type == "release"]
        assert len(alloc) == 1
        assert len(release) == 1
        assert alloc[0].variable_name == "buf"
        assert release[0].variable_name == "buf"

    def test_lock_guard(self):
        """提取 RAII lock_guard。"""
        lines = [
            "void baz() {",
            "    std::lock_guard<std::mutex> lock(mtx);",
            "    shared_data++;",
            "}",
        ]
        ops = extract_resource_lifecycle_for_function(
            "f:baz", "a.cpp", 1, 4, lines,
        )

        assert len(ops) == 1
        assert ops[0].type == "raii_guard"
        assert ops[0].resource_type == "mutex"
        assert ops[0].variable_name == "lock"

    def test_throw(self):
        """提取 throw。"""
        lines = [
            "void qux() {",
            "    if (failed) {",
            "        throw std::runtime_error(\"fail\");",
            "    }",
            "}",
        ]
        ops = extract_resource_lifecycle_for_function(
            "f:qux", "a.cpp", 1, 5, lines,
        )

        throws = [o for o in ops if o.type == "throw"]
        assert len(throws) == 1
        assert throws[0].resource_type == "exception"

    def test_gpu_context(self):
        """提取 GPU 上下文创建。"""
        lines = [
            "void init_gpu() {",
            "    wgpu::Instance inst = wgpu::CreateInstance(nullptr);",
            "    ctx->webgpu_global_ctx = webgpu_global_ctx_create(inst);",
            "}",
        ]
        ops = extract_resource_lifecycle_for_function(
            "f:init_gpu", "a.cpp", 1, 4, lines,
        )

        allocs = [o for o in ops if o.type == "allocate"]
        assert len(allocs) == 2
        assert any(o.resource_type == "gpu_context" for o in allocs)

    def test_handle_reset(self):
        """提取句柄重置。"""
        lines = [
            "void cleanup() {",
            "    ctx = nullptr;",
            "}",
        ]
        ops = extract_resource_lifecycle_for_function(
            "f:cleanup", "a.cpp", 1, 3, lines,
        )

        releases = [o for o in ops if o.type == "release"]
        assert len(releases) == 1
        assert releases[0].variable_name == "ctx"

    def test_smart_ptr(self):
        """提取智能指针。"""
        lines = [
            "void smart() {",
            "    auto ptr = std::make_unique<int>(42);",
            "    auto shared = std::make_shared<int>(42);",
            "}",
        ]
        ops = extract_resource_lifecycle_for_function(
            "f:smart", "a.cpp", 1, 4, lines,
        )

        allocs = [o for o in ops if o.type == "allocate"]
        assert len(allocs) == 2
        assert all(o.resource_type == "memory" for o in allocs)

    def test_no_resource_ops(self):
        """无资源操作的函数。"""
        lines = [
            "int add(int a, int b) {",
            "    return a + b;",
            "}",
        ]
        ops = extract_resource_lifecycle_for_function(
            "f:add", "a.cpp", 1, 3, lines,
        )

        assert len(ops) == 0

    def test_function_id_assigned(self):
        """ResourceOperation 应关联正确的 function_id。"""
        lines = [
            "void foo() {",
            "    int * p = new int;",
            "}",
        ]
        ops = extract_resource_lifecycle_for_function(
            "my_func_id", "a.cpp", 1, 3, lines,
        )

        assert len(ops) == 1
        assert ops[0].function_id == "my_func_id"
        assert ops[0].file_path == "a.cpp"
