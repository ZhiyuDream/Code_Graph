#!/usr/bin/env python3
"""
构建 Concept Abstraction Layer 的离线脚本。

运行后会生成：
- data/concept_abstraction.json

依赖：
- data/module_abstraction.json
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.core.concept_abstraction import ConceptAbstraction
from src.core.module_abstraction import ModuleAbstraction


def main():
    module_cache = _ROOT / "data" / "module_abstraction.json"
    concept_cache = _ROOT / "data" / "concept_abstraction.json"

    ma = ModuleAbstraction(cache_path=str(module_cache))
    if not ma.load():
        print("Module abstraction not found. Building first...")
        ma.build_from_neo4j()
        ma.save()

    ca = ConceptAbstraction(
        cache_path=str(concept_cache),
        model="gpt-4.1-mini",
        sub_resolution=2.0,
        min_concept_size=3,
        same_file_weight=0.3,
    )

    if ca.load():
        print(f"Concept abstraction already exists at {concept_cache}")
        print(f"Concepts: {len(ca.concepts)}")
        return

    ca.build_from_modules(ma)
    ca.save()
    print(f"\nBuilt concept abstraction with {len(ca.concepts)} concepts")


if __name__ == "__main__":
    main()
