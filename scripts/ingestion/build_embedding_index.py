#!/usr/bin/env python3
"""构建 QA embedding 索引，输出 data/qa_embedding_index.json。

用法：python scripts/ingestion/build_embedding_index.py
"""
from pathlib import Path

from neo4j import GraphDatabase

import sys
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from config import NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE, REPO_ROOT
from src.qa.retrievers.embedding import EmbeddingRetriever


def main():
    driver = GraphDatabase.driver(
        NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
    )
    retriever = EmbeddingRetriever()
    retriever.build_index(
        driver=driver,
        database=NEO4J_DATABASE,
        repo_root=REPO_ROOT,
        force=True,
    )
    driver.close()


if __name__ == "__main__":
    main()
