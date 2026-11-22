"""测量本地 ONNX 向量模型的内存占用。

用途：阿里云服务器疑似 OOM，量化 memcore 本地向量化路径的真实 RSS 开销，
用于判断"模型是不是 OOM 元凶"。

用法：
    python scripts/measure_mem.py [--model-dir PATH] [--iters N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import resource
except ImportError:  # Windows 没有 resource 模块
    resource = None  # type: ignore
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _k32_get_process_memory_info():
    """Windows 上取 GetProcessMemoryInfo 的正确签名（psapi 转发版会返回 0）。"""
    import ctypes
    from ctypes import wintypes

    fn = ctypes.windll.kernel32.K32GetProcessMemoryInfo
    fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
    fn.restype = ctypes.c_int
    return fn


def rss_mb() -> float:
    """当前进程 RSS（MB），兼容 Linux / macOS / Windows，无需 psutil。"""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            _k32_get_process_memory_info()(
                handle, ctypes.byref(pmc), pmc.cb
            )
            return pmc.WorkingSetSize / (1024 * 1024)
        except Exception:
            pass
    if resource is None:
        return 0.0
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return usage / (1024 * 1024)
    return usage / 1024


def peak_rss_mb() -> float:
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmHWM:"):
                        return int(line.split()[1]) / 1024
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            _k32_get_process_memory_info()(
                handle, ctypes.byref(pmc), pmc.cb
            )
            return pmc.PeakWorkingSetSize / (1024 * 1024)
        except Exception:
            pass
    if resource is None:
        return 0.0
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return usage / (1024 * 1024)
    return usage / 1024


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=str(REPO_ROOT / "vendor" / "models" / "bge-small-zh-v1.5"))
    ap.add_argument("--iters", type=int, default=32)
    ap.add_argument("--threads", type=int, default=2)
    args = ap.parse_args()

    report = {"platform": sys.platform, "threads": args.threads}

    report["rss_baseline_mb"] = round(rss_mb(), 1)
    report["peak_before_mb"] = round(peak_rss_mb(), 1)

    import numpy as np
    import onnxruntime as ort

    report["rss_after_import_ort_mb"] = round(rss_mb(), 1)

    from memcore.embed_local import LocalOnnxEmbedder

    t0 = time.time()
    emb = LocalOnnxEmbedder(Path(args.model_dir), num_threads=args.threads)
    load_s = time.time() - t0

    report["rss_after_load_model_mb"] = round(rss_mb(), 1)
    report["peak_after_load_mb"] = round(peak_rss_mb(), 1)
    report["model_load_seconds"] = round(load_s, 2)
    report["dim"] = emb.dim

    # 预热
    emb.embed("预热", is_query=True)

    t0 = time.time()
    for i in range(args.iters):
        emb.embed(f"第 {i} 条测试记忆：这是一段用于测量嵌入推理内存的中文文本。", is_query=True)
    elapsed = time.time() - t0

    report["rss_after_infer_mb"] = round(rss_mb(), 1)
    report["peak_final_mb"] = round(peak_rss_mb(), 1)
    report["infer_iters"] = args.iters
    report["infer_ms_per_text"] = round(elapsed / args.iters * 1000, 1)
    report["delta_load_mb"] = round(report["rss_after_load_model_mb"] - report["rss_after_import_ort_mb"], 1)
    report["delta_infer_mb"] = round(report["rss_after_infer_mb"] - report["rss_after_load_model_mb"], 1)

    # 线程数对内存/速度的影响
    report["note"] = "对比 --threads 不同取值即可看出 ORT 线程池带来的内存增量"

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
