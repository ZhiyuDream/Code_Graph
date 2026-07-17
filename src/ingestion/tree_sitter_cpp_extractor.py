"""Best-effort C/C++ extraction backed by Tree-sitter.

This module intentionally has no dependency on a compilation database.  It
produces the pipeline's existing ``FileResult`` model so it can be evaluated
beside the clangd extractor before it is wired into the production ingestion
entrypoint.

Tree-sitter provides syntax, not C++ name/type resolution.  Calls emitted by
this module are lexical call candidates and must be resolved with an explicit
confidence/provenance policy downstream.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Iterable, Iterator

from tree_sitter import Language, Node, Parser
import tree_sitter_cpp

from .models import ClassSymbol, FileResult, FunctionSymbol, RawCall


CPP_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
SKIP_DIRS = {
    ".git", "build", "bin", "obj", ".cache", "node_modules", "venv",
    ".venv", "__pycache__",
}
SKIP_PATH_PARTS = ("template-instances/fattn-", "template-instances/mmq-")


@dataclass(frozen=True)
class ParseDiagnostics:
    elapsed_ms: float
    byte_size: int
    has_error: bool
    error_nodes: int
    missing_nodes: int


def create_parser() -> Parser:
    """Create a parser compatible with current tree-sitter Python bindings."""
    return Parser(Language(tree_sitter_cpp.language()))


def iter_cpp_files(repo_root: Path) -> Iterator[Path]:
    """Yield repository-owned C/C++ files without retaining the full list."""
    for path in repo_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in CPP_EXTENSIONS:
            continue
        rel = path.relative_to(repo_root).as_posix()
        if any(part in SKIP_DIRS for part in path.relative_to(repo_root).parts):
            continue
        if any(pattern in rel for pattern in SKIP_PATH_PARTS):
            continue
        yield path


def collect_cpp_files(repo_root: Path) -> list[Path]:
    """Compatibility wrapper; large ingestion should use ``iter_cpp_files``."""
    return sorted(iter_cpp_files(repo_root))


def iter_file_batches(files: Iterable[Path], batch_size: int = 512) -> Iterator[list[Path]]:
    """Yield bounded file batches, never materializing the whole input."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    batch: list[Path] = []
    for path in files:
        batch.append(path)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def iter_parsed_batches(
    repo_root: Path,
    files: Iterable[Path] | None = None,
    batch_size: int = 512,
) -> Iterator[list[tuple[FileResult, ParseDiagnostics]]]:
    """Parse files in bounded batches and release results at each boundary."""
    source_files = iter_cpp_files(repo_root) if files is None else files
    for batch in iter_file_batches(source_files, batch_size=batch_size):
        parser = create_parser()
        parsed: list[tuple[FileResult, ParseDiagnostics]] = []
        for path in batch:
            try:
                parsed.append(parse_cpp_file(path, repo_root, parser=parser))
            except Exception:
                continue
        yield parsed


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _text(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _declarator_name(node: Node | None, source: bytes) -> tuple[str, int]:
    """Return the terminal function name and its zero-based column."""
    if node is None:
        return "", 0

    if node.type in {"identifier", "field_identifier", "operator_name", "destructor_name"}:
        return _text(node, source), node.start_point.column

    if node.type in {"qualified_identifier", "scoped_identifier"}:
        # Preserve names such as common_arg::get_args to match clangd IDs.
        return _text(node, source), node.start_point.column

    child = node.child_by_field_name("declarator")
    if child is not None and child.id != node.id:
        result = _declarator_name(child, source)
        if result[0]:
            return result

    # Grammar variants such as template_function and parenthesized_declarator
    # do not consistently expose a declarator field.
    for candidate in reversed(node.named_children):
        result = _declarator_name(candidate, source)
        if result[0]:
            return result
    return "", 0


def _call_name(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type in {"identifier", "field_identifier", "operator_name", "destructor_name"}:
        return _text(node, source)
    if node.type in {"qualified_identifier", "scoped_identifier", "template_function"}:
        name = node.child_by_field_name("name")
        if name is not None:
            return _call_name(name, source)
    if node.type in {"field_expression", "pointer_expression"}:
        field = node.child_by_field_name("field")
        if field is not None:
            return _call_name(field, source)
    # Preserve a useful terminal name for grammar variants while refusing
    # arbitrary expression text such as ``factory()()``.
    for candidate in reversed(node.named_children):
        name = _call_name(candidate, source)
        if name:
            return name
    return ""


def _signature(node: Node, source: bytes) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    raw = source[node.start_byte:end].decode("utf-8", errors="replace")
    return " ".join(raw.split())


def _function_from_definition(
    node: Node,
    source: bytes,
    file_path: str,
) -> FunctionSymbol | None:
    declarator = node.child_by_field_name("declarator")
    name, column = _declarator_name(declarator, source)
    if not name:
        return None
    start_line = node.start_point.row + 1
    end_line = node.end_point.row + 1
    return FunctionSymbol(
        id=f"{file_path}:{name}:{start_line}",
        name=name,
        signature=_signature(node, source),
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        start_character=column,
        is_definition=True,
    )


def _find_function_declarator(node: Node | None) -> Node | None:
    """Follow only declarator fields to a function declarator."""
    current = node
    while current is not None:
        if current.type == "function_declarator":
            return current
        next_node = current.child_by_field_name("declarator")
        if next_node is None or next_node.id == current.id:
            return None
        current = next_node
    return None


def _function_from_declaration(
    node: Node,
    source: bytes,
    file_path: str,
) -> FunctionSymbol | None:
    declarator = node.child_by_field_name("declarator")
    function_declarator = _find_function_declarator(declarator)
    if function_declarator is None:
        return None
    name, column = _declarator_name(
        function_declarator.child_by_field_name("declarator"), source
    )
    if not name:
        return None
    start_line = node.start_point.row + 1
    end_line = node.end_point.row + 1
    signature = " ".join(_text(node, source).rstrip("; ").split())
    if len(signature) > 2000:
        return None
    return FunctionSymbol(
        id=f"{file_path}:{name}:{start_line}",
        name=name,
        signature=signature,
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        start_character=column,
        is_definition=False,
    )


def _class_from_node(node: Node, source: bytes, file_path: str) -> ClassSymbol | None:
    name_node = node.child_by_field_name("name")
    name = _text(name_node, source)
    if not name:
        return None
    return ClassSymbol(
        name=name,
        file_path=file_path,
        start_line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
    )


def parse_cpp_file(
    path: Path,
    repo_root: Path,
    parser: Parser | None = None,
) -> tuple[FileResult, ParseDiagnostics]:
    """Parse one file into a clangd-pipeline-compatible ``FileResult``."""
    parser = parser or create_parser()
    source = path.read_bytes()
    relative_path = path.relative_to(repo_root).as_posix()

    started = perf_counter()
    tree = parser.parse(source)
    parse_ms = (perf_counter() - started) * 1000.0

    functions: list[FunctionSymbol] = []
    classes: list[ClassSymbol] = []
    calls: list[RawCall] = []
    includes: list[str] = []
    error_nodes = 0
    missing_nodes = 0

    for node in _walk(tree.root_node):
        if node.type == "ERROR":
            error_nodes += 1
        if node.is_missing:
            missing_nodes += 1
        if node.type == "preproc_include":
            includes.append(_text(node, source).strip())
        elif node.type in {"class_specifier", "struct_specifier", "union_specifier"}:
            cls = _class_from_node(node, source, relative_path)
            if cls is not None:
                classes.append(cls)
        elif node.type == "function_definition":
            fn = _function_from_definition(node, source, relative_path)
            if fn is None:
                continue
            caller_index = len(functions)
            functions.append(fn)
            body = node.child_by_field_name("body")
            if body is None:
                continue
            for child in _walk(body):
                if child.type != "call_expression":
                    continue
                callee_name = _call_name(child.child_by_field_name("function"), source)
                if not callee_name:
                    continue
                calls.append(RawCall(
                    caller_index=caller_index,
                    callee_name=callee_name,
                    file_path=relative_path,
                    line=child.start_point.row + 1,
                ))

        elif node.type in {"declaration", "field_declaration"}:
            fn = _function_from_declaration(node, source, relative_path)
            if fn is not None:
                functions.append(fn)

    result = FileResult(
        file_path=relative_path,
        functions=functions,
        classes=classes,
        variables=[],
        calls=calls,
        raw={
            "parser": "tree-sitter-cpp",
            "includes": includes,
            "has_error": tree.root_node.has_error,
            "error_nodes": error_nodes,
            "missing_nodes": missing_nodes,
        },
    )
    diagnostics = ParseDiagnostics(
        elapsed_ms=parse_ms,
        byte_size=len(source),
        has_error=tree.root_node.has_error,
        error_nodes=error_nodes,
        missing_nodes=missing_nodes,
    )
    return result, diagnostics
