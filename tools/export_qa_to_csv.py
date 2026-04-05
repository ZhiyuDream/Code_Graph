import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))   # 核心库
sys.path.insert(0, str(_ROOT))             # 根目录（config.py）

#!/usr/bin/env python3
"""
将 llama_cpp_QA.xlsx 第一个 sheet 导出为 CSV，供后续题目分类与 QA 流水线设计使用。
依赖：pip install pandas openpyxl
"""
import sys
from pathlib import Path

# 默认 QA 文件在项目根目录
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_XLSX = ROOT / "llama_cpp_QA.xlsx"
DEFAULT_CSV = ROOT / "llama_cpp_QA.csv"


def main():
    xlsx_path = DEFAULT_XLSX
    csv_path = DEFAULT_CSV
    if len(sys.argv) > 1:
        xlsx_path = Path(sys.argv[1])
    if len(sys.argv) > 2:
        csv_path = Path(sys.argv[2])

    if not xlsx_path.exists():
        print(f"错误：未找到文件 {xlsx_path}", file=sys.stderr)
        sys.exit(1)

    try:
        import pandas as pd
    except ImportError:
        print("请先安装: pip install pandas openpyxl", file=sys.stderr)
        sys.exit(1)

    df = pd.read_excel(xlsx_path, sheet_name=0, engine="openpyxl")
    df.to_csv(csv_path, index=False, encoding="utf-8")
    print(f"已导出: {csv_path}")
    print(f"行数: {len(df)}, 列: {list(df.columns)}")


if __name__ == "__main__":
    main()
