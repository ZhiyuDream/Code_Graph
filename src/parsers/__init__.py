"""解析器包。不同语言对应不同解析器实现。

用法：
    from parsers import create_parser

    parser = create_parser("python", repo_root="/path/to/repo")
    # 或
    parser = create_parser("cpp", compile_commands_dir="/path/to/build")

    results = parser.collect_all_tus(repo_root)
"""
from .base import Parser
from .python_parser import PythonParser, PythonASTVisitor, parse_python_file
from .cpp_parser import CppParser

_PARSERS: dict[str, type[Parser]] = {
    "python": PythonParser,
    "cpp": CppParser,
}


def create_parser(language: str, **kwargs) -> Parser:
    """
    工厂函数：根据语言创建对应解析器。

    Args:
        language: 语言标识 ("python", "cpp")
        **kwargs: 传给解析器的参数
            - PythonParser: repo_root (Path)
            - CppParser: compile_commands_dir (Path or str)

    Returns:
        Parser 实例

    Raises:
        ValueError: 不支持的语言
    """
    lang = language.lower()
    if lang not in _PARSERS:
        raise ValueError(
            f"不支持的语言: {language}，支持的: {list(_PARSERS.keys())}"
        )
    return _PARSERS[lang](**kwargs)


__all__ = [
    "Parser",
    "PythonParser",
    "CppParser",
    "PythonASTVisitor",
    "parse_python_file",
    "create_parser",
]

