#!/usr/bin/env python3
"""
构建 Module Abstraction Layer 的离线脚本。

运行后会生成：
- data/module_abstraction.json

然后 src/qa/investigation/hierarchical_module.py 和评估脚本可以加载它。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.module_abstraction import ModuleAbstraction


def main():
    cache_path = _ROOT / "data" / "module_abstraction.json"
    ma = ModuleAbstraction(
        cache_path=str(cache_path),
        model="gpt-4.1-mini",
        resolution=0.5,
        min_module_size=10,
        same_file_weight=0.5,
    )

    if ma.load():
        print(f"Module abstraction already exists at {cache_path}")
        print(f"Modules: {len(ma.modules)}")
        return

    ma.build_from_neo4j()
    ma.save()
    print(f"\nBuilt module abstraction with {len(ma.modules)} modules")


if __name__ == "__main__":
    main()
