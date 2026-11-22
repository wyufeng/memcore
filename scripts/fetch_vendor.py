#!/usr/bin/env python3
"""把第三方 wheel 与本地 embedding 模型一次性下载到 vendor/ 目录（仅开发机需要跑）。

目标机器安装时不再联网：install.sh 直接从 vendor/ 取件。

用法：
    python3 scripts/fetch_vendor.py

产物：
    vendor/wheels/*.whl
    vendor/models/bge-small-zh-v1.5/**
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WHEELS = ROOT / "vendor" / "wheels"
MODELS = ROOT / "vendor" / "models" / "bge-small-zh-v1.5"

UA = {"User-Agent": "Mozilla/5.0 (memcore vendor fetcher)"}

# (包名, 版本, 需要覆盖的 python tag)
WHEEL_SPECS = [
    ("numpy", "1.26.4", ["cp39", "cp310", "cp311", "cp312"]),
    ("onnxruntime", "1.16.3", ["cp39", "cp310", "cp311"]),
    ("onnxruntime", "1.19.2", ["cp312"]),
]

MODEL_FILES = [
    ("https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/config.json", "config.json"),
    ("https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/tokenizer_config.json", "tokenizer_config.json"),
    ("https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/special_tokens_map.json", "special_tokens_map.json"),
    ("https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/vocab.txt", "vocab.txt"),
    ("https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/onnx/model_int8.onnx", "onnx/model_int8.onnx"),
]


def fetch(url: str, dest: Path, retries: int = 4) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"skip {dest.name} ({dest.stat().st_size/1e6:.1f} MB)")
        return
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
            print(f"ok {dest.name} ({dest.stat().st_size/1e6:.1f} MB)")
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"retry {attempt+1}/{retries} {dest.name}: {exc}", file=sys.stderr)
    raise RuntimeError(f"下载失败 {url}: {last}")


def pypi_urls(pkg: str, version: str, tags: list) -> list:
    meta = json.loads(
        urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/{version}/json", timeout=60).read()
    )
    out = []
    for u in meta["urls"]:
        fn = u["filename"]
        if not fn.endswith(".whl"):
            continue
        if "x86_64" not in fn or "linux" not in fn:
            continue
        if "musllinux" in fn:
            continue
        if not any(t in fn for t in tags):
            continue
        out.append((fn, u["url"]))
    return out


def main() -> int:
    WHEELS.mkdir(parents=True, exist_ok=True)
    jobs = []
    for pkg, version, tags in WHEEL_SPECS:
        for fn, url in pypi_urls(pkg, version, tags):
            jobs.append((url, WHEELS / fn))
    for url, dest in MODEL_FILES:
        jobs.append((url, MODELS / dest))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda j: fetch(*j), jobs))

    total = sum(f.stat().st_size for f in WHEELS.rglob("*") if f.is_file())
    total += sum(f.stat().st_size for f in MODELS.rglob("*") if f.is_file())
    print(f"\nvendor 体积合计 {total/1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
