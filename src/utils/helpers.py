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


def resolve_device(device: str) -> str:
    """解析设备字符串：auto→CUDA(可用时)否则CPU；cuda→CUDA(可用时)否则CPU"""
    try:
        import torch
        cuda_ok = torch.cuda.is_available()
    except ImportError:
        cuda_ok = False
    if device in ("cuda", "auto") and not cuda_ok:
        return "cpu"
    if device == "auto" and cuda_ok:
        return "cuda"
    return device