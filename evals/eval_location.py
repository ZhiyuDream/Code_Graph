#!/usr/bin/env python3
"""Location-based evaluation for v2 benchmark - 评估答案是否准确指出文件位置"""
import json
import sys
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import OPENAI_API_KEY, OPENAI_BASE_URL, LLM_MODEL
from openai import OpenAI

LOCATION_JUDGE_PROMPT = """你是一个代码审查质量评审。请判断「生成答案」是否**合理地**引用了关键证据中的代码位置。

## 核心判断原则（请像人类评审一样灵活，不要机械比对字符串）

### 1. 路径匹配要灵活
- **绝对路径 vs 相对路径不算错**：例如 `/root/data/zzy/llama.cpp/common/sampling.cpp` 和 `common/sampling.cpp` 指向的是同一个文件，应视为匹配。
- **忽略项目根目录前缀**：Agent 在 sandbox 中运行，看到的路径带绝对前缀是正常的，不要因此判错。
- **只认文件名也算对**：如果生成答案只写了 `sampling.cpp:185` 而证据是 `common/sampling.cpp:185`，只要文件名对得上，就应视为合理引用。

### 2. 行号要有容错
- **行号偏差 ±50 行以内视为找对地方**：代码可能因版本不同而偏移，只要 Agent 引用的行号与证据行号相差在 50 行以内，且确实是在讨论同一个函数/同一段代码，就应接受。
- **只引用文件名没写行号**：如果生成答案明确提到了证据中的文件名（即使没给行号），且结合上下文能判断它确实找到了那个文件，可以算部分正确；但如果关键证据有明确行号要求而答案完全没提任何行号，则算引用不够精确。

### 3. 允许引用额外文件
- **引用证据之外的文件不算错**：生成答案引用了更多相关文件（如头文件、测试文件）来支撑结论，这是好事，不应因此判错。只要关键证据中的路径被覆盖到了即可。
- **只引用定义位置而非调用位置**：如果证据同时包含函数定义和调用位置，Agent 只引用了定义位置，只要它确实分析了对的代码，可以算 CORRECT（不完全精确但不至于判错）。

### 4. 底线要求
- **必须引用具体代码位置**：生成答案不能只给概括性结论（如"该函数没有参数"），必须至少给出具体的文件路径（或文件名）+ 大致行号范围。
- **路径不能张冠李戴**：如果生成答案引用的文件与问题完全无关（比如问题问的是 `common/sampling.cpp`，答案却在说 `examples/main.cpp`），才算 INCORRECT。

## 关键证据中的文件路径
{evidence_files}

【问题】
{question}

【参考答案】
{reference}

【生成答案】
{generated}

## 输出格式
必须首行输出：结果: CORRECT 或 结果: INCORRECT
第二行起：简要说明理由（1-2句话，指出具体哪些路径匹配了，哪些没匹配）
"""

client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL or None)


def location_judge(item: dict) -> tuple[bool, str, int]:
    """评估答案是否准确指出文件位置。"""
    # 提取证据中的文件路径
    evidence = item.get("evidence", "")
    evidence_files = set()
    for m in re.findall(r'`([^`]+?)`', evidence):
        if ':' in m and ('/' in m or '\\' in m):
            fp = m.rsplit(':', 1)[0]
            if '-' in fp:
                fp = fp.rsplit('-', 1)[0]
            evidence_files.add(fp.replace('\\', '/'))

    evidence_str = '\n'.join(f'- {f}' for f in sorted(evidence_files)) if evidence_files else '（无明确文件路径）'

    prompt = LOCATION_JUDGE_PROMPT.format(
        evidence_files=evidence_str,
        question=str(item.get("question", ""))[:500],
        reference=str(item.get("reference", ""))[:800],
        generated=str(item.get("generated", ""))[:1500]
    )

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200
        )
        text = resp.choices[0].message.content.strip()
        first_line = text.split('\n')[0].upper()
        is_correct = "CORRECT" in first_line and "INCORRECT" not in first_line
        return is_correct, text, item.get("index", 0)
    except Exception as e:
        print(f"    API错误: {e}")
        return False, f"评估错误: {e}", item.get("index", 0)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True, help="待评估的 benchmark 结果 JSON")
    parser.add_argument("-o", "--output", type=Path, required=True, help="输出评估结果 JSON")
    parser.add_argument("-w", "--workers", type=int, default=20, help="并行 worker 数")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"加载 {len(data)} 题，开始 location-based 评估 ({args.workers} workers)...")

    correct = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(location_judge, item): item for item in data}

        for future in as_completed(futures):
            is_correct, reason, idx = future.result()
            item = futures[future]
            item["eval_location_correct"] = is_correct
            item["eval_location_reason"] = reason
            if is_correct:
                correct += 1
            completed += 1

            if completed % 10 == 0:
                print(f"  [{completed}/{len(data)}] 当前正确: {correct}/{completed} = {correct/completed*100:.1f}%")
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 评估完成！Location 正确: {correct}/{len(data)} = {correct/len(data)*100:.1f}%")


if __name__ == "__main__":
    main()
