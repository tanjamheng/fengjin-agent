"""一键下载所有模型到本地 models/ 目录

支持 Windows / macOS / Linux。
模型来源：ModelScope（国内下载速度快）。

用法：
    python scripts/download_models.py
"""

import sys
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
MODELS_DIR = PROJECT_ROOT / "models"

# 模型清单：(本地子目录名, ModelScope model_id)
MODELS = [
    ("bge-m3", "BAAI/bge-m3"),
    ("bge-reranker-v2-m3", "BAAI/bge-reranker-v2-m3"),
    ("Llama-Guard-3-1B", "LLM-Research/Llama-Guard-3-1B"),
]


def main():
    try:
        from modelscope.hub.snapshot_download import snapshot_download
    except ImportError:
        print("错误：请先安装 modelscope")
        print("  pip install modelscope")
        sys.exit(1)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"模型目录: {MODELS_DIR}\n")

    for name, ms_id in MODELS:
        local_path = MODELS_DIR / name
        if local_path.exists() and any(local_path.iterdir()):
            print(f"[跳过] {name} 已存在")
            continue

        print(f"[下载] {name} ({ms_id}) ...")
        try:
            snapshot_download(ms_id, local_dir=str(local_path))
            print(f"[完成] {name}\n")
        except Exception as e:
            print(f"[失败] {name}: {e}\n")
            sys.exit(1)

    print("全部模型就绪！")


if __name__ == "__main__":
    main()
