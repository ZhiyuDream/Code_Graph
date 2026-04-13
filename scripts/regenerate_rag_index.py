#!/usr/bin/env python3
"""
重新生成 RAG embedding 索引
从 Neo4j 读取所有函数和注释，生成新的 embedding 索引
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL, NEO4J_DATABASE, EMBEDDING_MODEL
from src.neo4j_writer import get_driver
from openai import OpenAI


def get_all_functions(driver) -> list[dict]:
    """从 Neo4j 获取所有函数信息"""
    query = """
        MATCH (fn:Function)
        RETURN fn.name as name, 
               fn.file_path as file_path,
               fn.start_line as start_line,
               fn.annotation_json as annotation,
               fn.signature as signature
        ORDER BY fn.file_path, fn.name
    """
    with driver.session(database=NEO4J_DATABASE) as session:
        result = session.run(query)
        return [dict(record) for record in result]


def create_function_chunk(func: dict) -> dict:
    """为函数创建 chunk"""
    name = func.get("name", "")
    file_path = func.get("file_path", "")
    start_line = func.get("start_line", 0)
    
    # 构建 chunk 文本
    text_parts = [f"// 函数: {name} @ {file_path}:{start_line}"]
    
    # 添加签名
    signature = func.get("signature", "")
    if signature:
        text_parts.append(signature)
    
    # 添加注解
    annotation = func.get("annotation", "")
    if annotation:
        try:
            ann_data = json.loads(annotation) if isinstance(annotation, str) else annotation
            summary = ann_data.get("summary", "")
            if summary:
                text_parts.append(f"// 功能: {summary}")
        except:
            pass
    
    chunk_text = "\n".join(text_parts)
    
    return {
        "id": f"func::{name}::{file_path}:{start_line}",
        "type": "function",
        "text": chunk_text,
        "meta": {
            "name": name,
            "file": file_path,
            "line": start_line
        }
    }


def generate_embeddings(client, chunks: list[dict], batch_size: int = 100) -> list[list[float]]:
    """批量生成 embedding"""
    all_embeddings = []
    total = len(chunks)
    
    for i in range(0, total, batch_size):
        batch = chunks[i:i+batch_size]
        texts = [c["text"] for c in batch]
        
        try:
            resp = client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=texts
            )
            batch_embeddings = [item.embedding for item in resp.data]
            all_embeddings.extend(batch_embeddings)
            
            print(f"  进度: {min(i+batch_size, total)}/{total} ({min(i+batch_size, total)/total*100:.1f}%)")
        except Exception as e:
            print(f"  Error at batch {i}: {e}")
            # 填充空 embedding
            for _ in batch:
                all_embeddings.append([0.0] * 1536)  # text-embedding-3-small 维度
    
    return all_embeddings


def main():
    print("=== 重新生成 RAG Embedding 索引 ===\n")
    
    # 连接 Neo4j
    driver = get_driver()
    print("Neo4j 连接成功")
    
    # 获取所有函数
    print("\n从 Neo4j 获取函数...")
    functions = get_all_functions(driver)
    print(f"找到 {len(functions)} 个函数")
    
    # 创建 chunks
    print("\n创建 chunks...")
    chunks = [create_function_chunk(f) for f in functions]
    
    # 生成 embeddings
    print("\n生成 embeddings (这可能需要一些时间)...")
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)
    embeddings = generate_embeddings(client, chunks)
    
    # 保存索引
    index_data = {
        "chunks": chunks,
        "embeddings": embeddings
    }
    
    output_path = _ROOT / "data" / "classic_rag_index.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
    
    print(f"\n索引已保存: {output_path}")
    print(f"  chunks: {len(chunks)}")
    print(f"  embeddings: {len(embeddings)}")
    
    driver.close()
    print("\n完成!")


if __name__ == "__main__":
    main()
