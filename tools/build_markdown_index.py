#!/usr/bin/env python3
"""
Build markdown index for llama.cpp documentation.
Extracts text from all .md files, chunks them, computes embeddings, and saves to JSON.

Usage:
    python tools/build_markdown_index.py [--repo /path/to/llama.cpp]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from openai import OpenAI

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL, EMBEDDING_MODEL

MARKDOWN_INDEX_PATH = _ROOT / "data" / "llama_markdown_index.json"
CHUNK_MAX = 600  # chars per chunk


def extract_markdown_text(md_path: Path) -> str:
    """Extract plain text from markdown, removing code blocks and excessive whitespace."""
    try:
        content = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

    # Remove code blocks (```...``` and `...`)
    content = re.sub(r'```[\s\S]*?```', '', content)
    content = re.sub(r'`[^`]+`', '', content)

    # Remove markdown links but keep text
    content = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', content)

    # Remove images
    content = re.sub(r'!\[.*?\]\(.*?\)', '', content)

    # Remove HTML tags
    content = re.sub(r'<[^>]+>', '', content)

    # Normalize whitespace
    content = re.sub(r'\n{3,}', '\n\n', content)
    content = content.strip()

    return content


def chunk_markdown(md_path: Path, text: str, max_chars: int = CHUNK_MAX) -> list[dict]:
    """Chunk markdown text by sections (headers) or by character limit."""
    chunks = []
    rel_path = str(md_path.relative_to(md_path.anchor) if hasattr(md_path, 'anchor') else md_path)

    # Try to split by headers (## and ###)
    sections = re.split(r'\n(?=##?\s)', text)

    current_chunk = ""
    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Get header line if exists
        header_match = re.match(r'(#{1,3}\s+[^\n]+)\n(.*)', section, re.DOTALL)
        if header_match:
            header = header_match.group(1).strip()
            body = header_match.group(2).strip()
            section_text = f"{header}\n\n{body}"
        else:
            section_text = section

        if len(current_chunk) + len(section_text) <= max_chars:
            current_chunk += ("\n\n" if current_chunk else "") + section_text
        else:
            if current_chunk:
                chunks.append(current_chunk)
            # If single section is too big, split by lines
            if len(section_text) > max_chars:
                lines = section_text.split('\n')
                current_chunk = ""
                for line in lines:
                    if len(current_chunk) + len(line) <= max_chars:
                        current_chunk += ("\n" if current_chunk else "") + line
                    else:
                        if current_chunk:
                            chunks.append(current_chunk)
                        current_chunk = line
            else:
                current_chunk = section_text

    if current_chunk:
        chunks.append(current_chunk)

    return [{"text": c, "source": str(rel_path)} for c in chunks]


def embed_texts(client, texts: list[str], batch_size: int = 64) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        for e in sorted(resp.data, key=lambda x: x.index):
            out.append(e.embedding)
        print(f"  embedded {min(i + batch_size, len(texts))}/{len(texts)}", flush=True)
    return out


def build_index(repo_path: Path) -> dict:
    """Build markdown index from repo."""
    md_files = list(repo_path.glob("**/*.md"))
    print(f"Found {len(md_files)} markdown files")

    # Filter out low-value files (changelogs, PR descriptions, etc.)
    skip_patterns = ["changelog", "CHANGELOG", ".github", "release-", "releases/"]
    skip_heavy = [
        "docs/2-",  # Release notes like docs/2-07-0.md
        "docs/3-",
    ]

    filtered = []
    for f in md_files:
        path_str = str(f)
        if any(p in path_str for p in skip_patterns):
            continue
        if any(p in path_str for p in skip_heavy):
            continue
        # Skip very large files (> 3000 lines - likely release notes)
        try:
            line_count = len(f.read_text(errors="replace").splitlines())
            if line_count > 3000:
                print(f"  Skipping large file: {f.relative_to(repo_path)} ({line_count} lines)")
                continue
        except Exception:
            pass
        filtered.append(f)

    print(f"After filtering: {len(filtered)} markdown files")

    chunks = []
    for md_file in filtered:
        text = extract_markdown_text(md_file)
        if not text.strip():
            continue
        file_chunks = chunk_markdown(md_file, text)
        for i, chunk in enumerate(file_chunks):
            chunk["id"] = f"{md_file.relative_to(repo_path)}::{i}"
            chunk["type"] = "markdown"
            chunks.append(chunk)

    print(f"Total chunks: {len(chunks)}")
    return {"chunks": chunks}


def main():
    parser = argparse.ArgumentParser(description="Build markdown index for llama.cpp")
    parser.add_argument("--repo", type=Path,
                        default=Path("/data/yulin/RUC/llama.cpp"),
                        help="Path to llama.cpp repo")
    parser.add_argument("--output", type=Path,
                        default=MARKDOWN_INDEX_PATH,
                        help="Output index path")
    args = parser.parse_args()

    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)

    print(f"Building markdown index from {args.repo}...")
    index_data = build_index(args.repo)

    print("Computing embeddings...")
    texts = [c["text"] for c in index_data["chunks"]]
    embeddings = embed_texts(client, texts)

    index_data["embeddings"] = embeddings

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(index_data, ensure_ascii=False), encoding="utf-8")
    print(f"Index saved to {args.output} ({len(index_data['chunks'])} chunks)")


if __name__ == "__main__":
    main()