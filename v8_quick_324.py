import os, sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

# 修改环境变量
os.environ['LLM_MODEL'] = 'deepseek-v4-pro'

# 导入原脚本的所有内容
from experiments.module_expansion.run_qa_v8_deepseek_react_fixed import *

def main_quick():
    rows = []
    with open('results/qav2_test_cleaned.csv', 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    
    rows = rows[:324]  # 只跑前324题
    
    print(f"DeepSeek ReAct Agent 快速验证: {len(rows)} 题")
    print(f"模型: deepseek-v4-pro")
    print(f"并行: 50 workers")
    print(f"估算耗时: ~{len(rows) * 60 / 50 / 60:.0f} 分钟")
    print()
    
    driver = get_neo4j_driver()
    from tools.core.llm_client import get_llm_client
    client = get_llm_client(provider='deepseek')
    
    results = []
    completed = 0
    
    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {
            executor.submit(process_single, driver, client, row, i): i
            for i, row in enumerate(rows)
        }
        
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
                completed += 1
                
                if completed % 20 == 0 or completed == len(rows):
                    print(f"  已完成 {completed}/{len(rows)} 题...")
                    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
                    with open('results/v8_quick_324.json', 'w', encoding='utf-8') as f:
                        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
                        
            except Exception as e:
                print(f"  处理题目时出错: {e}")
                completed += 1
    
    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
    with open('results/v8_quick_324.json', 'w', encoding='utf-8') as f:
        json.dump(sorted_results, f, ensure_ascii=False, indent=2)
    
    print(f"\n完成！共处理 {len(results)}/{len(rows)} 题")
    print(f"结果保存至: results/v8_quick_324.json")
    
    close_neo4j_driver()

if __name__ == "__main__":
    main_quick()
