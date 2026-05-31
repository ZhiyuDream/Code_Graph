"""
Tests for src/ingestion/param_flow_extractor.py
"""
from __future__ import annotations

import pytest

from src.ingestion.param_flow_extractor import (
    extract_param_flow_for_function,
    _infer_param_names,
)


class TestExtractParamFlow:
    """Tests for param flow extraction."""

    def test_field_read(self):
        """提取参数成员访问。"""
        lines = [
            "void process(const Params & params) {",
            "    if (params.no_perf) {",
            "        return;",
            "    }",
            "}",
        ]
        flow = extract_param_flow_for_function(
            "process", "a.cpp", 1, 5, lines,
            param_names=["params"],
        )

        assert len(flow) == 1
        assert flow[0]["param"] == "params"
        assert any("field_read:no_perf" in op for op in flow[0]["operations"])

    def test_field_assign(self):
        """提取参数字段赋值（配置级联）。"""
        lines = [
            "void setup(Params & params) {",
            "    params.no_perf = false;",
            "    params.grammar = \"default\";",
            "}",
        ]
        flow = extract_param_flow_for_function(
            "setup", "a.cpp", 1, 4, lines,
            param_names=["params"],
        )

        assert len(flow) == 1
        ops = flow[0]["operations"]
        assert any("field_assign:no_perf" in op for op in ops)
        assert any("field_assign:grammar" in op for op in ops)

    def test_pass_to_callee(self):
        """提取参数传递给下游函数。"""
        lines = [
            "void init(Device * device) {",
            "    init_gpu(device);",
            "    setup_device(device);",
            "}",
        ]
        flow = extract_param_flow_for_function(
            "init", "a.cpp", 1, 4, lines,
            param_names=["device"],
        )

        assert len(flow) == 1
        ops = flow[0]["operations"]
        assert any("pass_to:init_gpu" in op for op in ops)
        assert any("pass_to:setup_device" in op for op in ops)

    def test_assign_to_local(self):
        """提取参数赋值给局部变量。"""
        lines = [
            "void copy(const std::string & src) {",
            "    auto dest = src;",
            "}",
        ]
        flow = extract_param_flow_for_function(
            "copy", "a.cpp", 1, 3, lines,
            param_names=["src"],
        )

        assert len(flow) == 1
        assert any("assign_to:dest" in op for op in flow[0]["operations"])

    def test_return_param(self):
        """提取参数返回。"""
        lines = [
            "const char * get_name(const Model & model) {",
            "    return model.name;",
            "}",
        ]
        flow = extract_param_flow_for_function(
            "get_name", "a.cpp", 1, 3, lines,
            param_names=["model"],
        )

        assert len(flow) == 1
        assert "return" in flow[0]["operations"]

    def test_array_index(self):
        """提取参数数组索引。"""
        lines = [
            "void access(int arr[], int idx) {",
            "    int val = arr[idx];",
            "}",
        ]
        flow = extract_param_flow_for_function(
            "access", "a.cpp", 1, 3, lines,
            param_names=["arr", "idx"],
        )

        # arr 被数组索引
        arr_flow = [f for f in flow if f["param"] == "arr"]
        assert len(arr_flow) == 1
        assert "array_index" in arr_flow[0]["operations"]

    def test_infer_param_names(self):
        """从函数签名推断参数名。"""
        sig = "void foo(int a, const std::string & b, void * c)"
        names = _infer_param_names(sig)
        assert names == ["a", "b", "c"]

    def test_infer_param_names_template(self):
        """从模板函数签名推断参数名。"""
        sig = "template<typename T> void bar(T x, const std::vector<T> & vec)"
        names = _infer_param_names(sig)
        assert names == ["x", "vec"]

    def test_no_param_usage(self):
        """参数未被使用的函数。"""
        lines = [
            "void unused(int a) {",
            "    return;",
            "}",
        ]
        flow = extract_param_flow_for_function(
            "unused", "a.cpp", 1, 3, lines,
            param_names=["a"],
        )

        assert len(flow) == 0

    def test_auto_infer_params(self):
        """不传入 param_names 时自动推断。"""
        lines = [
            "void process(const Params & params, int count) {",
            "    if (params.no_perf) {",
            "        return;",
            "    }",
            "    use_count(count);",
            "}",
        ]
        flow = extract_param_flow_for_function(
            "process", "a.cpp", 1, 6, lines,
        )

        # 应该自动推断出 params 和 count
        params = [f["param"] for f in flow]
        assert "params" in params
        assert "count" in params
