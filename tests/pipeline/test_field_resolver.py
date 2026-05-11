"""Tests for field_resolver.py"""
from __future__ import annotations

import pytest

from src.pipeline.field_resolver import (
    _find_enclosing_class,
    enrich_file_results,
    resolve_file_fields,
)
from src.pipeline.models import ClassSymbol, FileResult, VariableSymbol, FunctionSymbol


class TestFindEnclosingClass:
    def test_exact_match(self):
        classes = [
            ClassSymbol(name="ClassA", file_path="test.h", start_line=10, end_line=15),
            ClassSymbol(name="ClassB", file_path="test.h", start_line=20, end_line=25),
        ]
        assert _find_enclosing_class(classes, "test.h", 12) == 0
        assert _find_enclosing_class(classes, "test.h", 22) == 1

    def test_no_match(self):
        classes = [
            ClassSymbol(name="ClassA", file_path="test.h", start_line=10, end_line=15),
        ]
        assert _find_enclosing_class(classes, "test.h", 20) is None
        assert _find_enclosing_class(classes, "other.h", 12) is None

    def test_nested_classes(self):
        """行号落在内层 class 范围内，应选最内层的（start_line 更大的）。"""
        classes = [
            ClassSymbol(name="Outer", file_path="test.h", start_line=10, end_line=30),
            ClassSymbol(name="Inner", file_path="test.h", start_line=15, end_line=20),
        ]
        assert _find_enclosing_class(classes, "test.h", 17) == 1


class TestResolveFileFields:
    def test_orphan_field_fixed(self):
        """orphan member field 被正确关联到 enclosing class。"""
        fr = FileResult(
            file_path="test.h",
            classes=[
                ClassSymbol(name="MyStruct", file_path="test.h", start_line=10, end_line=15),
            ],
            variables=[
                VariableSymbol(id="v1", name="fieldA", file_path="test.h", start_line=12, kind="member"),
            ],
        )
        result = resolve_file_fields(fr)
        assert result.variables[0].scope_class_index == 0

    def test_already_assigned_unchanged(self):
        """已有 scope_class_index 的不应被覆盖。"""
        fr = FileResult(
            file_path="test.h",
            classes=[
                ClassSymbol(name="A", file_path="test.h", start_line=10, end_line=15),
                ClassSymbol(name="B", file_path="test.h", start_line=20, end_line=25),
            ],
            variables=[
                VariableSymbol(id="v1", name="fieldA", file_path="test.h", start_line=12, kind="member", scope_class_index=0),
            ],
        )
        result = resolve_file_fields(fr)
        assert result.variables[0].scope_class_index == 0

    def test_non_member_unchanged(self):
        """非 member 变量不应被修改。"""
        fr = FileResult(
            file_path="test.cpp",
            classes=[
                ClassSymbol(name="A", file_path="test.cpp", start_line=10, end_line=15),
            ],
            variables=[
                VariableSymbol(id="v1", name="x", file_path="test.cpp", start_line=12, kind="local"),
            ],
        )
        result = resolve_file_fields(fr)
        assert result.variables[0].scope_class_index is None
        assert result.variables[0].kind == "local"

    def test_no_classes_no_change(self):
        fr = FileResult(
            file_path="test.cpp",
            classes=[],
            variables=[
                VariableSymbol(id="v1", name="x", file_path="test.cpp", start_line=5, kind="global"),
            ],
        )
        result = resolve_file_fields(fr)
        assert result.variables[0].kind == "global"

    def test_field_outside_all_classes(self):
        """field 行号不在任何 class 范围内，保持 orphan。"""
        fr = FileResult(
            file_path="test.h",
            classes=[
                ClassSymbol(name="A", file_path="test.h", start_line=10, end_line=15),
            ],
            variables=[
                VariableSymbol(id="v1", name="orphanField", file_path="test.h", start_line=20, kind="member"),
            ],
        )
        result = resolve_file_fields(fr)
        assert result.variables[0].scope_class_index is None


class TestEnrichFileResults:
    def test_multiple_files(self):
        file_results = [
            FileResult(
                file_path="a.h",
                classes=[ClassSymbol(name="A", file_path="a.h", start_line=10, end_line=15)],
                variables=[VariableSymbol(id="v1", name="f1", file_path="a.h", start_line=12, kind="member")],
            ),
            FileResult(
                file_path="b.h",
                classes=[ClassSymbol(name="B", file_path="b.h", start_line=20, end_line=25)],
                variables=[VariableSymbol(id="v2", name="f2", file_path="b.h", start_line=22, kind="member")],
            ),
        ]
        enriched = enrich_file_results(file_results)
        assert enriched[0].variables[0].scope_class_index == 0
        assert enriched[1].variables[0].scope_class_index == 0

    def test_mixed_files(self):
        """含 class 的文件和不含 class 的文件混合。"""
        file_results = [
            FileResult(
                file_path="a.h",
                classes=[ClassSymbol(name="A", file_path="a.h", start_line=10, end_line=15)],
                variables=[VariableSymbol(id="v1", name="f1", file_path="a.h", start_line=12, kind="member")],
            ),
            FileResult(
                file_path="b.cpp",
                classes=[],
                variables=[VariableSymbol(id="v2", name="x", file_path="b.cpp", start_line=5, kind="local")],
            ),
        ]
        enriched = enrich_file_results(file_results)
        assert enriched[0].variables[0].scope_class_index == 0
        assert enriched[1].variables[0].scope_class_index is None
