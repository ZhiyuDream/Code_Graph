#!/usr/bin/env python3
"""
修复所有 tools/*.py 的 import paths。
"""
import re, sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent / "tools"

SYS_PATH_SETUP = '''import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）
'''

IMPORT_REMAP = {
    'from src.config import': 'from config import',
    'from src.neo4j_writer import': 'from neo4j_writer import',
    'from src.clangd_parser import': 'from clangd_parser import',
    'from src.graph_builder import': 'from graph_builder import',
    'from src.ast_parser import': 'from ast_parser import',
    'from src.enrich_graph_metrics import': 'from enrich_graph_metrics import',
    'from src.workflow_expand import': 'from workflow_expand import',
    'from src.workflow_writer import': 'from workflow_writer import',
    'from src.github_fetcher import': 'from github_fetcher import',
    'from src.issue_pr_writer import': 'from issue_pr_writer import',
    'from src.import_github_to_graph import': 'from import_github_to_graph import',
    'from src.fetch_github_data import': 'from fetch_github_data import',
    'from tools.agent_qa import': 'from agent_qa import',
}

def fix_file(fpath: Path):
    content = fpath.read_text(encoding='utf-8')

    # 跳过已有正确设置的
    if 'sys.path.insert' in content and '_ROOT' in content and 'src' in content:
        print(f'  Already fixed: {fpath.name}')
        return

    lines = content.split('\n')

    # 找 shebang 位置
    shebang_idx = -1
    future_idx = -1
    first_import_idx = -1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if i == 0 and stripped.startswith('#!'):
            shebang_idx = i
        if stripped.startswith('from __future__'):
            future_idx = i
        if stripped.startswith('import ') or stripped.startswith('from '):
            if first_import_idx < 0:
                first_import_idx = i

    # 如果有 shebang，插在其后；否则插在 from __future__ 前
    insert_before = future_idx if future_idx >= 0 else first_import_idx
    if insert_before < 0:
        insert_before = len(lines)

    # 如果有 shebang，sys.path setup 插在 shebang 后
    if shebang_idx >= 0:
        insert_at = shebang_idx + 1
    else:
        insert_at = 0

    # 插入 sys.path setup
    setup_lines = SYS_PATH_SETUP.strip().split('\n')
    for j, sl in enumerate(setup_lines):
        lines.insert(insert_at + j, sl)

    # 调整 future_idx 和 first_import_idx（已被扰乱）
    content = '\n'.join(lines)

    # 统一 import remapping
    for old, new in IMPORT_REMAP.items():
        if old != new:
            content = content.replace(old, new)

    fpath.write_text(content, encoding='utf-8')
    print(f'  Fixed: {fpath.name}')

print('Fixing imports in tools/*.py...')
for f in sorted((TOOLS_DIR).glob('*.py')):
    fix_file(f)
print('Done.')
