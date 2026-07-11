"""GPU 显存预算管理器

启动时暖机测量 CUDA baseline → 按优先级贪心分配 → 结果冻结为只读表。
调用方通过 allocate(name) 查表获取设备决策，O(1)。

优先级体系:
  P0 CRITICAL  — 没有它核心功能崩溃 (bge-m3: RAG + 记忆)
  P1 IMPORTANT — 有它体验更好，没有也能跑 (reranker)
  P2 OPTIONAL  — 锦上添花 (Llama Guard 语义层)
  P99 FALLBACK — 未注册模型默认 CPU

加载阶段 OOM 兜底: safe_model_load()   → GPU → CPU → skip 三级降级
系统内存兜底:       check_system_memory() → ok / degraded / refuse
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import os
from typing import Optional

from .logger import get_logger

log = get_logger("gpu_budget")


# ── 优先级 ──────────────────────────────────────────────────

class Priority(IntEnum):
    CRITICAL = 0   # P0: 没有它核心功能崩溃
    IMPORTANT = 1  # P1: 有它体验更好，没有也能跑
    OPTIONAL = 2   # P2: 锦上添花
    FALLBACK = 99  # P99: 未在表中注册的模型 → 默认 CPU


# ── 注册表 ──────────────────────────────────────────────────

@dataclass
class ModelEntry:
    name: str
    vram_mb: int           # 预估显存 (含 30% buffer)
    priority: Priority
    description: str
    fallback: str = "cpu"  # 降级目标: "cpu" | "skip"


GPU_MODEL_REGISTRY: list[ModelEntry] = [
    ModelEntry("bge-m3",               1350, Priority.CRITICAL,  "RAG检索+记忆搜索"),
    ModelEntry("bge-reranker-v2-m3",   1350, Priority.IMPORTANT, "CrossEncoder重排序"),
    ModelEntry("llama-guard-3-1b",     2600, Priority.OPTIONAL,  "安全护栏P1语义检测"),
]


def _is_model_enabled(entry: ModelEntry) -> bool:
    """返回本次进程应参与预算的模型。

    bge-m3 同时服务 RAG、角色漂移和可随时启用的记忆功能，始终保留预算。
    Llama Guard 是明确 opt-in 的 P1 语义检测；未启用时既不加载也不占用预算。
    """
    if entry.name == "llama-guard-3-1b":
        return os.environ.get("FENGJIN_GUARD_MODEL_ENABLED", "false").lower() == "true"
    return True


# ── 系统内存兜底 ──────────────────────────────────────────────

def check_system_memory() -> str:
    """返回 "ok" | "degraded" | "refuse"

    阈值:
      > 2GB  → ok        正常加载所有模型
      1-2GB → degraded  只加载 P0，P1/P2 跳过
      < 1GB → refuse    不加载任何本地模型，仅云端 LLM 可用
    """
    try:
        import psutil
        avail_gb = psutil.virtual_memory().available / (1024 ** 3)
    except ImportError:
        log.warning("psutil 未安装，跳过系统内存检查")
        return "ok"
    except Exception as e:
        log.warning("系统内存检查失败: {}，跳过", e)
        return "ok"
    if avail_gb < 1:
        return "refuse"
    elif avail_gb < 2:
        return "degraded"
    return "ok"


# ── 预算管理器 ───────────────────────────────────────────────

class GPUBudgetManager:
    """GPU 显存预算管理器（模块级单例）

    启动时调用一次 __init__，之后通过 allocate() 查表。
    """

    # 安全垫：预留给 forward pass 中间张量 + 分配器碎片
    SAFETY_MARGIN_MB = 400

    def __init__(self, mem_status: str = "ok"):
        self._reservations: dict[str, str] = {}  # 第一行——防 AttributeError

        try:
            import torch
        except ImportError:
            log.info("PyTorch 未安装，全 CPU 模式")
            return

        if not torch.cuda.is_available():
            log.info("无 CUDA 设备，全 CPU 模式")
            return

        # ── 暖机 + 测量可用显存（驱动级，感知所有进程）──
        try:
            torch.cuda.empty_cache()
            torch.zeros(1).cuda()
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
            # 驱动级 API：free 包含其他进程占用的显存，比 memory_reserved() 准确
            free_bytes, total_bytes = torch.cuda.mem_get_info()
            total_mb = int(total_bytes / (1024 ** 2))
            free_mb = int(free_bytes / (1024 ** 2))
        except torch.cuda.OutOfMemoryError:
            log.warning("GPU 暖机 OOM：显存已被其他程序占满，全 CPU 模式")
            torch.cuda.empty_cache()
            return
        except RuntimeError as e:
            if "out of memory" in str(e).lower():
                log.warning("GPU 暖机 OOM：显存已被其他程序占满，全 CPU 模式")
                torch.cuda.empty_cache()
                return
            raise

        available = free_mb - self.SAFETY_MARGIN_MB

        log.info(
            "暖机完成: total={}MB free={}MB safety={}MB → 可用预算={}MB",
            total_mb, free_mb, self.SAFETY_MARGIN_MB, max(available, 0),
        )

        if available <= 0:
            log.warning("可用显存预算为 0，全 CPU 模式")
            return

        # ── 系统内存状态 → 调整允许的最高优先级 ──
        # refuse (< 1GB): 所有模型强制 skip
        # degraded (1-2GB): 只加载 P0
        active_models = [m for m in GPU_MODEL_REGISTRY if _is_model_enabled(m)]
        if mem_status == "refuse":
            for entry in active_models:
                self._reservations[entry.name] = "skip"
            log.info("系统内存不足 1GB，所有已启用本地模型跳过")
            return
        elif mem_status == "degraded":
            max_priority = Priority.CRITICAL  # 只 P0
        else:
            max_priority = Priority.FALLBACK  # P0~P99 全放行

        # ── 贪心分配 ──
        remaining = available
        sorted_models = sorted(
            [
                m for m in active_models if m.priority <= max_priority
            ],
            key=lambda m: m.priority,
        )

        for entry in sorted_models:
            if remaining >= entry.vram_mb:
                self._reservations[entry.name] = "cuda"
                remaining -= entry.vram_mb
                log.info(
                    "{} (P{}): alloc {}MB → GPU (剩余 {}MB)",
                    entry.name, int(entry.priority), entry.vram_mb, remaining,
                )
            else:
                self._reservations[entry.name] = entry.fallback  # "cpu" or "skip"
                log.info(
                    "{} (P{}): alloc {}MB → {} (不足 {}MB)",
                    entry.name, int(entry.priority), entry.vram_mb,
                    entry.fallback.upper(), entry.vram_mb - remaining,
                )

        disabled_names = [m.name for m in GPU_MODEL_REGISTRY if not _is_model_enabled(m)]
        if disabled_names:
            log.info("未启用模型不参与显存预算: {}", ", ".join(disabled_names))

        # 未显式注册的模型自动 P99 → 默认 CPU (由 allocate 的 .get default 处理)
        gpu_count = sum(1 for v in self._reservations.values() if v == "cuda")
        cpu_count = sum(1 for v in self._reservations.values() if v == "cpu")
        skip_count = sum(1 for v in self._reservations.values() if v == "skip")
        log.info(
            "决策完成: {} GPU / {} CPU / {} skip (系统内存: {})",
            gpu_count, cpu_count, skip_count, mem_status,
        )

    def allocate(self, model_name: str) -> str:
        """查询模型应使用的设备。返回 "cuda" | "cpu" | "skip"

        注册表中的模型 → _reservations 查表
        未注册 → 默认 "cpu" (P99)
        """
        return self._reservations.get(model_name, "cpu")

    def summary(self) -> str:
        """多行摘要（日志用）"""
        if not self._reservations:
            return "[gpu_budget] 全 CPU 模式（无可用 GPU 预算）"
        lines = ["[gpu_budget] 分配结果:"]
        for name, device in sorted(self._reservations.items()):
            lines.append(f"  {name}: {device}")
        return "\n".join(lines)


# ── 模块级单例 ───────────────────────────────────────────────

import threading

_budget_manager: Optional[GPUBudgetManager] = None
_init_lock = threading.Lock()

_MEMORY_OOM_MARKERS = (
    "out of memory",
    "not enough memory",
    "cannot allocate memory",
    "defaultcpuallocator",
)


def is_memory_oom(error: BaseException) -> bool:
    """统一识别 CUDA 与 PyTorch CPU 分配器报告的内存耗尽。"""
    return isinstance(error, MemoryError) or any(
        marker in str(error).lower() for marker in _MEMORY_OOM_MARKERS
    )


def init_budget(mem_status: str = "ok") -> GPUBudgetManager:
    """初始化全局预算管理器（预处理前调用一次）"""
    global _budget_manager
    with _init_lock:
        if _budget_manager is not None:
            log.warning("预算已初始化，跳过重复调用 (mem_status={})", mem_status)
            return _budget_manager
        _budget_manager = GPUBudgetManager(mem_status=mem_status)
    return _budget_manager


def recalc_budget(mem_status: Optional[str] = None) -> GPUBudgetManager:
    """系统加载前重新计算预算。

    预处理会改变内存占用，因此默认重新检测系统内存；调用方也可传入已测得的
    状态。绝不能将低内存状态无条件提升为 ``ok``。
    """
    global _budget_manager
    effective_mem_status = mem_status or check_system_memory()
    with _init_lock:
        _budget_manager = GPUBudgetManager(mem_status=effective_mem_status)
    return _budget_manager


# ── OOM 兜底 ─────────────────────────────────────────────────

def safe_model_load(
    model_name: str,
    fn_gpu,
    fn_cpu,
    fallback: str = "cpu",
    planned_device: str = "cuda",
):
    """模型加载时的三级降级链。返回 (model, actual_device)

    ① 尝试 GPU 加载 → OOM?
    ② 尝试 CPU 加载 → MemoryError?
    ③ 跳过此模型，功能不可用
    """
    import torch

    if planned_device == "skip":
        log.info("{} 按预算跳过加载", model_name)
        return None, "skip"

    # 预算已决定 CPU 时，不能再以“GPU 尝试”的名义调用加载器。
    if planned_device != "cuda":
        try:
            return fn_cpu(), "cpu"
        except Exception as e:
            if not is_memory_oom(e):
                raise
            if fallback == "skip":
                log.error("{} CPU 加载内存不足，跳过此功能", model_name)
                return None, "skip"
            raise

    # 一级: GPU
    try:
        model = fn_gpu()
        try:
            actual = str(model.device)
        except Exception:
            actual = "cuda"  # 无法读取设备时的回退
        return model, actual
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        log.warning("{} GPU 加载 OOM，降级 CPU", model_name)
    except RuntimeError as e:
        if not is_memory_oom(e):
            raise
        torch.cuda.empty_cache()
        log.warning("{} GPU 加载 OOM (RuntimeError)，降级 CPU", model_name)

    # 二级: CPU
    try:
        model = fn_cpu()
        return model, "cpu"
    except Exception as e:
        if not is_memory_oom(e):
            raise
        if fallback == "skip":
            log.error("{} 无法加载 (显存+内存均不足)，跳过此功能", model_name)
            return None, "skip"
        raise  # fallback == "cpu" 但内存不足 → 致命错误


def gpu_oom_guard(model_name: str, fn_gpu, fn_cpu):
    """GPU 推理 OOM 时降级 CPU 重试（工具函数，供未来接入推理路径使用）。

    当前运行时推理 OOM 由各模块的 except Exception 捕获并返回降级结果。
    """
    import torch

    try:
        return fn_gpu()
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        log.warning("{} GPU 推理 OOM，降级 CPU 重试", model_name)
    except RuntimeError as e:
        if "out of memory" not in str(e).lower():
            raise
        torch.cuda.empty_cache()
        log.warning("{} GPU 推理 OOM (RuntimeError)，降级 CPU 重试", model_name)

    try:
        return fn_cpu()
    except MemoryError:
        log.error("{} CPU 推理也 OOM，跳过本次操作", model_name)
        return None
