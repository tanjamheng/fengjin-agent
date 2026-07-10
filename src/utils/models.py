"""模型下载 + FP16 量化一体化工具

CLI 和 server 共用。确保所有必需模型已下载并量化为 FP16。

健壮性设计：
- 下载直写目标目录（ModelScope 自带文件级校验 + 断点续传）
- 量化写入临时目录，成功后才原子替换（保护原始 FP32 不被半写破坏）
- .state 文件标记状态，崩溃后自动恢复

状态机：
  (无目录) ──下载──▶ fp32 ──量化──▶ fp16 ──▶ 就绪
  (目录存在,无.state) ──下载(续传)──▶ fp32 ──量化──▶ fp16
  (.state=fp32) ──量化──▶ fp16
  (.state=fp16) ──跳过
  (量化崩溃) ──.state 仍为 fp32 ──重新量化

崩溃恢复矩阵：
  下载中崩溃    → 目录存在,无.state → 下一次 snapshot_download 校验+续传
  下载完成      → .state=fp32
  量化中崩溃    → .state 仍为 fp32，临时目录残留 → 下一次重新量化
  量化完成      → .state=fp16 → 跳过
  原子替换窗口  → 旧目录已删/新目录在 tmp → 下一次重新下载或量化
"""

import gc
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..utils.logger import get_logger

log = get_logger("models")

# 项目根目录
_ROOT = Path(__file__).resolve().parent.parent.parent
MODELS_DIR = _ROOT / "models"
TMP_DIR = MODELS_DIR / ".tmp"

# 模型清单: (本地目录名, ModelScope model_id, 模型类型)
MODELS = [
    ("bge-m3", "BAAI/bge-m3", "sentence_transformer"),
    ("bge-reranker-v2-m3", "BAAI/bge-reranker-v2-m3", "cross_encoder"),
    ("Llama-Guard-3-1B", "LLM-Research/Llama-Guard-3-1B", "causal_lm"),
]

_STATE_FILE = ".state"
_STATE_FP32 = "fp32"
_STATE_FP16 = "fp16"

# 模型预估下载大小（GB，FP32 原始精度）——用于百分比进度估算
_KNOWN_MODEL_SIZES_GB = {
    "bge-m3": 4.5,
    "bge-reranker-v2-m3": 2.8,
    "Llama-Guard-3-1B": 3.5,
}


def _get_dir_size(path: Path) -> int:
    """目录总大小 (bytes)。不存在返回 0。"""
    if not path.exists():
        return 0
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _download_with_progress(
    ms_id: str,
    target_path: Path,
    model_name: str,
    op: str,  # "download" 或 "quantize"
    _progress: Callable[[str, str, str, Optional[int]], None],
    _emit: Callable[[str], None],
) -> None:
    """在后台线程执行 snapshot_download，主线程轮询目录大小推送百分比。"""
    from modelscope.hub.snapshot_download import snapshot_download

    estimated_gb = _KNOWN_MODEL_SIZES_GB.get(model_name, 3.0)
    estimated_bytes = int(estimated_gb * 1024 ** 3)
    initial_size = _get_dir_size(target_path)

    download_done = threading.Event()
    download_error: list = []

    def _run():
        try:
            os.environ.setdefault("MODELSCOPE_LOG_LEVEL", "ERROR")
            try:
                snapshot_download(ms_id, local_dir=str(target_path), show_progress=False)
            except TypeError:
                snapshot_download(ms_id, local_dir=str(target_path))
        except Exception as e:
            download_error.append(e)
        finally:
            download_done.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    last_pct = -1
    while not download_done.is_set():
        current = _get_dir_size(target_path)
        if current > initial_size and estimated_bytes > 0:
            pct = min(99, int((current - initial_size) / estimated_bytes * 100))
            if pct > 0 and pct - last_pct >= 1:  # 每 1% 推送一次
                last_pct = pct
                _progress(model_name, op, "progress", pct)
        time.sleep(0.8)

    thread.join()

    if download_error:
        raise download_error[0]


def _quantize_with_progress(
    name: str,
    model_type: str,
    target_path: Path,
    _progress: Callable[[str, str, str, Optional[int]], None],
    _emit: Callable[[str], None],
) -> bool:
    """在后台线程量化，时间估算 + 临时目录大小双重保障百分比推送。"""
    import glob as glob_mod

    result = [False]
    done = threading.Event()

    def _run():
        result[0] = _safe_quantize(name, model_type, target_path, _emit)
        done.set()

    original_size = _get_dir_size(target_path)
    original_gb = original_size / (1024 ** 3) if original_size > 0 else 0
    estimated_output = int(original_size * 0.55) if original_size > 0 else 0
    # 时间估算：每 GB 原始模型约需 5 秒量化
    estimated_seconds = max(5, original_gb * 5)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    t_start = time.monotonic()
    last_pct = -1
    tmp_pattern = str(MODELS_DIR / ".tmp" / f"q-{name}*")

    while not done.is_set():
        elapsed = time.monotonic() - t_start

        # 时间估算（加载+量化全程平滑推进）
        time_pct = min(90, int(elapsed / estimated_seconds * 100))

        # 目录大小（临时目录写入阶段更精准）
        size_pct = 0
        if estimated_output > 0:
            matches = glob_mod.glob(tmp_pattern)
            if matches:
                temp_size = _get_dir_size(Path(matches[0]))
                size_pct = min(95, int(temp_size / estimated_output * 100))

        # 取较大者：加载阶段靠时间估算，写入阶段靠目录大小
        pct = max(time_pct, size_pct)
        if pct > 0 and pct - last_pct >= 1:
            last_pct = pct
            _progress(name, "quantize", "progress", pct)
        time.sleep(0.8)

    thread.join()
    return result[0]


def _recover_orphaned_quantizations(emit: Callable[[str], None]) -> None:
    """恢复量化完成但未 swap 的临时目录（原子替换窗口崩溃）

    正常流程：量化写入 tmp/q-xxx → 写 .state=fp16 → atomic_swap(tmp→target)
    若在 atomic_swap 中崩溃（rmtree 完成但 move 未完成），
    target 已删，tmp/q-xxx 完整且含 .state=fp16 → 此处恢复 swap。
    """
    if not TMP_DIR.exists():
        return

    for tmp_entry in list(TMP_DIR.iterdir()):
        if not tmp_entry.is_dir():
            continue
        if not tmp_entry.name.startswith("q-"):
            # 非量化临时目录（不应出现，安全删除）
            shutil.rmtree(str(tmp_entry), ignore_errors=True)
            continue

        state_file = tmp_entry / _STATE_FILE
        if not state_file.exists() or state_file.read_text().strip() != _STATE_FP16:
            # 量化未完成或损坏 → 清理
            shutil.rmtree(str(tmp_entry), ignore_errors=True)
            continue

        # 量化已完成但未 swap → 恢复到正确位置
        model_name = tmp_entry.name[2:]  # 去掉 "q-" 前缀
        target = MODELS_DIR / model_name
        if target.exists():
            # target 已存在（可能另一个进程恢复了）→ 清理临时目录
            shutil.rmtree(str(tmp_entry), ignore_errors=True)
            continue

        emit(f"  ↻ 恢复未完成的 swap: {model_name}")
        try:
            shutil.move(str(tmp_entry), str(target))
            emit(f"    {model_name} (FP16 已恢复)")
        except Exception as e:
            emit(f"    ✗ 恢复失败: {e}")


def ensure_models(
    msg: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[str, str], None]] = None,
) -> bool:
    """确保所有模型已下载且量化为 FP16

    Args:
        msg: 日志回调（兼容旧接口）
        progress_callback: 进度回调 (step_id, status) — 用于 launcher 模式

    健壮性：
    - 下载直写目标目录，ModelScope 自带文件级校验 + 断点续传
    - 量化写入临时目录，成功后才原子替换（保护原始不被半写破坏）
    - .state 文件追踪阶段，崩溃后自动恢复
    """
    def _emit(text: str):
        if msg:
            msg(text)
        else:
            log.info(text)

    def _progress(model_name: str, op: str, status: str, percent: Optional[int] = None):
        """发送进度：step_id = 'model_{op}:{model_name}'"""
        if progress_callback:
            step_id = f"model_{op}:{model_name}"
            progress_callback(step_id, status, percent)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # 恢复上次量化完成但未 swap 的临时目录（原子替换窗口崩溃）
    _recover_orphaned_quantizations(_emit)

    all_ok = True

    for local_name, ms_id, model_type in MODELS:
        # Llama Guard 条件跳过 — 与 _scan_preprocess_plan 保持一致
        if local_name == "Llama-Guard-3-1B":
            guard_enabled = os.environ.get("FENGJIN_GUARD_MODEL_ENABLED", "false").lower() == "true"
            if not guard_enabled:
                _emit(f"  - {local_name} (未启用，跳过)")
                continue

        target_path = MODELS_DIR / local_name
        state_file = target_path / _STATE_FILE

        # ── 读取当前状态 ──
        current_state = None
        if state_file.exists():
            current_state = state_file.read_text().strip()

        # ── fp16：已完成 ──
        if current_state == _STATE_FP16:
            _emit(f"  ✓ {local_name} (FP16 就绪)")
            continue

        # ── fp32：需要量化 ──
        if current_state == _STATE_FP32:
            _emit(f"  ⟳ {local_name} (FP32 → FP16) ...")
            _progress(local_name, "quantize", "start")
            ok = _quantize_with_progress(local_name, model_type, target_path, _progress, _emit)
            if not ok:
                _progress(local_name, "quantize", "failed")
                all_ok = False
            else:
                _progress(local_name, "quantize", "done")
            continue

        # ── 其他：需要下载（含续传） ──
        if current_state is not None:
            _emit(f"  ⚠ {local_name} .state 异常 ({current_state})，重新下载")

        _emit(f"  ⬇ 下载 {local_name} ({ms_id}) ...")
        _progress(local_name, "download", "start")
        target_path.mkdir(parents=True, exist_ok=True)
        try:
            # 后台线程下载 + 主线程轮询目录大小推送百分比
            _download_with_progress(ms_id, target_path, local_name, "download", _progress, _emit)
        except Exception as e:
            _emit(f"    ✗ 下载失败: {e}")
            _emit(f"    （目录已保留，下次启动自动续传）")
            _progress(local_name, "download", "failed")
            all_ok = False
            continue

        _progress(local_name, "download", "done")

        # 下载成功 → 写状态
        state_file.write_text(_STATE_FP32)
        _emit(f"    下载完成 ({_dir_size_gb(target_path):.1f} GB)")

        # ── 立即量化 ──
        _progress(local_name, "quantize", "start")
        ok = _quantize_with_progress(local_name, model_type, target_path, _progress, _emit)
        if not ok:
            _progress(local_name, "quantize", "failed")
            all_ok = False
        else:
            _progress(local_name, "quantize", "done")

    # 清理临时目录
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    return all_ok


def _safe_quantize(
    name: str, model_type: str, target_path: Path,
    emit: Callable[[str], None],
) -> bool:
    """加载 FP32 → 量化到临时目录 → 原子替换

    量化全程写入临时目录。即使崩溃，原始 FP32 目录完全不受影响。
    .state 保持 fp32，下次启动自动重新量化。
    """
    t0 = time.monotonic()
    q_tmp = _mkdtemp(f"q-{name}")

    try:
        if model_type == "sentence_transformer":
            _quantize_sentence_transformer(str(target_path), q_tmp)
        elif model_type == "cross_encoder":
            _quantize_cross_encoder(str(target_path), q_tmp)
        elif model_type == "causal_lm":
            _quantize_causal_lm(str(target_path), q_tmp)

        # 量化成功后写状态 → 原子替换
        Path(q_tmp, _STATE_FILE).write_text(_STATE_FP16)
        _atomic_swap(q_tmp, target_path)

        t_elapsed = time.monotonic() - t0
        gc.collect()  # 回收 FP32 加载时的峰值内存（~4GB）
        emit(f"    量化完成 ({t_elapsed:.0f}s), 磁盘 {_dir_size_gb(target_path):.1f} GB")
        return True
    except Exception as e:
        emit(f"    ✗ 量化失败: {e}")
        # 清理量化临时目录
        shutil.rmtree(q_tmp, ignore_errors=True)
        # 量化失败可能是模型文件损坏 → 删除 .state 强制下次重新下载
        _state = target_path / _STATE_FILE
        if _state.exists():
            _state.unlink()
        return False


def _atomic_swap(src: str, dst: Path) -> None:
    """原子替换目录：删除 dst → 重命名 src → dst

    非严格 POSIX 原子（Windows 不支持目录级 renameat2），
    但 rm+mv 在同一 FS 上极快（毫秒级），崩溃窗口极窄。
    即使在此窗口崩溃：dst 已删或残留，src 在 tmp/，
    下次启动均可从 .state 感知并恢复。
    """
    if dst.exists():
        shutil.rmtree(str(dst), ignore_errors=True)
    shutil.move(src, str(dst))


def _mkdtemp(prefix: str) -> str:
    """在临时目录下创建子目录"""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=str(TMP_DIR))


# ── 量化实现 ──────────────────────────────────────────────

def _quantize_sentence_transformer(src_path: str, dst_path: str) -> None:
    """SentenceTransformer: FP32 → FP16 → 保存到临时目录"""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(src_path, device="cpu")
    model.half()
    model.save(dst_path, safe_serialization=True)


def _quantize_cross_encoder(src_path: str, dst_path: str) -> None:
    """CrossEncoder: FP32 → FP16 → 保存到临时目录"""
    from sentence_transformers import CrossEncoder

    model = CrossEncoder(src_path, device="cpu")
    model.model.half()
    model.model.save_pretrained(dst_path, safe_serialization=True)
    if hasattr(model, "tokenizer") and model.tokenizer is not None:
        model.tokenizer.save_pretrained(dst_path)


def _quantize_causal_lm(src_path: str, dst_path: str) -> None:
    """Llama Guard: FP32 → FP16 → 保存到临时目录"""
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM

    tokenizer = AutoTokenizer.from_pretrained(src_path)
    model = AutoModelForCausalLM.from_pretrained(
        src_path,
        torch_dtype=torch.float16,
        device_map="cpu",
    )
    model.save_pretrained(dst_path, safe_serialization=True)
    tokenizer.save_pretrained(dst_path)


def _dir_size_gb(path: Path) -> float:
    """目录大小（GB）"""
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total / (1024 ** 3)
