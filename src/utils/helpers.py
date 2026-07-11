"""工具函数"""

from pathlib import Path
import sys


def get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).parent.parent.parent


def ensure_dir(path: Path) -> Path:
    """确保目录存在"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_device(device: str, model_name: str | None = None) -> str:
    """解析设备字符串。device="auto" 时委托 GPUBudgetManager 决策。

    - device 明确指定 (cpu/cuda) → 原样返回
    - device == "auto" + model_name → budget.allocate(model_name) (优先级预算)
    - device == "auto" + 无 model_name → torch.cuda.is_available() 兜底
    """
    if device != "auto":
        if device == "cuda":
            try:
                import torch
                if not torch.cuda.is_available():
                    return "cpu"
            except ImportError:
                return "cpu"
        return device

    if model_name:
        from .gpu_budget import _budget_manager
        if _budget_manager:
            return _budget_manager.allocate(model_name)

    # 兜底：无 budget 实例或旧式调用
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"