"""一键下载 + 量化所有模型到本地 models/ 目录

下载 → 立即 FP16 量化 → 保留量化版。
FENGJIN_GUARD_MODEL_ENABLED=false 时跳过 Llama Guard 模型。

用法：
    python scripts/download_models.py
"""

import sys
from pathlib import Path

# 确保 src/ 在 import 路径中
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main():
    from src.utils.models import ensure_models

    print(f"模型目录: {PROJECT_ROOT / 'models'}\n")

    ok = ensure_models(msg=print)
    if ok:
        print("\n全部模型就绪！")
    else:
        print("\n部分模型未就绪，请检查网络后重试。")
        sys.exit(1)


if __name__ == "__main__":
    main()
