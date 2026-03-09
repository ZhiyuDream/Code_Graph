"""
从 .env 与环境变量读取配置。Neo4j、仓库路径、compile_commands 等。
"""
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

# 加载 Code_Graph 目录下的 .env
_CODE_GRAPH_DIR = Path(__file__).resolve().parent
load_dotenv(_CODE_GRAPH_DIR / ".env")

# Neo4j
NEO4J_URI = os.environ.get("NEO4J_URI", "neo4j://localhost:7687")
NEO4J_USERNAME = os.environ.get("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.environ.get("NEO4J_DATABASE", "neo4j")

# 仓库与编译数据库（阶段 1）
# REPO_ROOT: 待解析仓库根目录，例如 /data/yulin/RUC/llama.cpp
# COMPILE_COMMANDS_DIR: 含 compile_commands.json 的目录（一般为 build），或直接填 build 的绝对路径
REPO_ROOT = os.environ.get("REPO_ROOT", "")
COMPILE_COMMANDS_DIR = os.environ.get("COMPILE_COMMANDS_DIR", "")

# 阶段 3：GitHub Issue/PR
# GITHUB_TOKEN: 用于调用 GitHub API（.env 中配置，勿提交）
# GITHUB_REPO: 仓库标识 "owner/repo"，若未设置则尝试从 REPO_ROOT 的 git remote origin 推导
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()

# 阶段 5：QA 流水线 / LLM 生成答案
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini").strip()
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small").strip()


def get_compile_commands_path() -> Path | None:
    """返回 compile_commands.json 所在目录的 Path；若未配置则尝试 REPO_ROOT/build。"""
    if COMPILE_COMMANDS_DIR and os.path.isdir(COMPILE_COMMANDS_DIR):
        p = Path(COMPILE_COMMANDS_DIR)
        if (p / "compile_commands.json").exists():
            return p
    if REPO_ROOT and os.path.isdir(REPO_ROOT):
        build = Path(REPO_ROOT) / "build"
        if (build / "compile_commands.json").exists():
            return build
    return None


def get_repo_root() -> Path | None:
    """返回仓库根目录的 Path；若未配置则返回 None。"""
    if REPO_ROOT and os.path.isdir(REPO_ROOT):
        return Path(REPO_ROOT)
    return None


def get_github_repo() -> str:
    """返回 GitHub 仓库标识 "owner/repo"。优先用 GITHUB_REPO；否则从 REPO_ROOT 的 git remote origin 解析。"""
    if GITHUB_REPO:
        return GITHUB_REPO
    root = get_repo_root()
    if not root:
        return ""
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0 or not out.stdout:
            return ""
        url = out.stdout.strip()
        # https://github.com/owner/repo.git 或 git@github.com:owner/repo.git
        if "github.com" in url:
            if url.startswith("https://"):
                path = url.rstrip("/").replace("https://github.com/", "").replace(".git", "")
            elif url.startswith("git@"):
                path = url.split(":")[-1].rstrip("/").replace(".git", "")
            else:
                return ""
            if "/" in path:
                return path
    except Exception:
        pass
    return ""
