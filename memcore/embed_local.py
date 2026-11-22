"""本地向量模型：ONNX Runtime + 内置 bge-small-zh-v1.5（int8）。

要点：
- 依赖 numpy / onnxruntime 二进制由 vendor/wheels 提供，安装时离线解压，不联网
- 分词用本仓库的纯 Python BertTokenizer（memcore/tokenizer.py），不需要 transformers
- BGE 官方推荐 CLS pooling + L2 归一化，这里照做
- 模型文件缺失或运行库不可用时，embed.py 会退回哈希向量，功能不中断
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .tokenizer import BertTokenizer

DEFAULT_MODEL_DIR = "bge-small-zh-v1.5"


def _find_model_dir(cfg=None) -> Optional[Path]:
    """定位本地模型目录。

    优先级：MEMCORE_EMBED_MODEL_PATH > 配置 embed_model_path >
            $MEMCORE_HOME/models/<name> > 仓库内 vendor/models/<name>
    """
    candidates: List[Path] = []
    env = os.environ.get("MEMCORE_EMBED_MODEL_PATH")
    if env:
        candidates.append(Path(env).expanduser())

    name = DEFAULT_MODEL_DIR
    if cfg is not None:
        configured = getattr(cfg, "embed_model_path", "") or ""
        if configured:
            p = Path(configured).expanduser()
            candidates.append(p)
            if p.name != name:
                name = p.name
        home = getattr(cfg, "home", None)
        if home:
            candidates.append(Path(home) / "models" / name)

    repo_root = Path(__file__).resolve().parent.parent
    candidates.append(repo_root / "vendor" / "models" / name)

    for c in candidates:
        if c and c.is_dir() and (c / "vocab.txt").exists():
            return c
    return None


def _resolve_onnx_file(model_dir: Path) -> Optional[Path]:
    """优先用 int8 量化模型，其次其他量化版本，最后 fp32。"""
    onnx_dir = model_dir / "onnx"
    prefer = [
        "model_int8.onnx",
        "model_quantized.onnx",
        "model_uint8.onnx",
        "model_q4.onnx",
        "model.onnx",
    ]
    for fname in prefer:
        p = onnx_dir / fname
        if p.exists():
            return p
    if onnx_dir.is_dir():
        cands = sorted(onnx_dir.glob("*.onnx"))
        if cands:
            return cands[0]
    p = model_dir / "model.onnx"
    return p if p.exists() else None


class LocalOnnxEmbedder:
    """本地 ONNX embedding 模型。"""

    name = "local-onnx"

    def __init__(
        self,
        model_dir: Path,
        max_len: int = 256,
        query_prefix: str = "",
        num_threads: int = 2,
    ) -> None:
        import numpy as np  # 延迟导入：没有 numpy 时不影响其它功能
        import onnxruntime as ort

        self._np = np
        self.model_dir = Path(model_dir)
        self.max_len = int(max_len)
        self.query_prefix = query_prefix or ""

        onnx_file = _resolve_onnx_file(self.model_dir)
        if onnx_file is None:
            raise FileNotFoundError(f"模型目录下没有 onnx 文件: {self.model_dir}")

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(num_threads)
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(onnx_file), sess_options=opts, providers=["CPUExecutionProvider"]
        )

        self.input_names = {i.name for i in self.session.get_inputs()}
        tokenizer_cfg = {}
        cfg_path = self.model_dir / "tokenizer_config.json"
        if cfg_path.exists():
            try:
                tokenizer_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                tokenizer_cfg = {}
        self.tokenizer = BertTokenizer.from_vocab_file(
            str(self.model_dir / "vocab.txt"),
            do_lower_case=bool(tokenizer_cfg.get("do_lower_case", False)),
        )
        self.dim = self._detect_dim()

    def _detect_dim(self) -> int:
        for o in self.session.get_outputs():
            shape = list(o.shape)
            if shape and isinstance(shape[-1], int):
                return int(shape[-1])
        return 512

    def _encode(self, text: str) -> Dict[str, List[int]]:
        return self.tokenizer.encode(text, max_len=self.max_len)

    def embed(self, text: str, is_query: bool = False) -> List[float]:
        content = (text or "").strip()
        if is_query and self.query_prefix:
            content = self.query_prefix + content
        if not content:
            content = " "
        enc = self._encode(content)
        np = self._np
        feed = {
            "input_ids": np.array([enc["input_ids"]], dtype=np.int64),
            "attention_mask": np.array([enc["attention_mask"]], dtype=np.int64),
        }
        if "token_type_ids" in self.input_names:
            feed["token_type_ids"] = np.array([enc["token_type_ids"]], dtype=np.int64)
        outputs = self.session.run(None, feed)
        hidden = outputs[0]
        if isinstance(hidden, list):
            hidden = hidden[0]
        cls_vec = hidden[0][0]
        vec = [float(x) for x in cls_vec]
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    def embed_many(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


def describe_local_model(cfg=None) -> Dict[str, object]:
    """只探测本地模型文件是否存在（不加载模型，不依赖 numpy/onnxruntime）。

    用于 doctor 输出和安装自检，开销极小。
    """
    model_dir = _find_model_dir(cfg)
    if model_dir is None:
        return {
            "found": False,
            "hint": "（执行 sh scripts/install.sh 会把 vendor/models 复制到 ~/.local/share/memcore/models）",
        }
    onnx_file = _resolve_onnx_file(model_dir)
    info: Dict[str, object] = {
        "found": True,
        "name": model_dir.name,
        "path": str(model_dir),
        "onnx": str(onnx_file) if onnx_file else None,
    }
    if onnx_file is not None:
        size_mb = round(onnx_file.stat().st_size / (1024 * 1024), 1)
        info["size_mb"] = size_mb
        # 从 config.json 推断隐层维度
        dim = None
        cfg_path = model_dir / "config.json"
        if cfg_path.exists():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                dim = data.get("hidden_size")
            except Exception:
                dim = None
        info["dim"] = dim
    else:
        info["hint"] = "（目录下没有 .onnx 文件）"
    return info


def try_local_embedder(cfg=None) -> Optional[LocalOnnxEmbedder]:
    """尝试构造本地模型 embedder；任何问题都返回 None（调用方退回哈希向量）。"""
    model_dir = _find_model_dir(cfg)
    if model_dir is None:
        return None
    try:
        max_len = int(getattr(cfg, "embed_max_len", 256) or 256)
    except Exception:
        max_len = 256
    try:
        prefix = getattr(cfg, "embed_query_prefix", "") or ""
    except Exception:
        prefix = ""
    try:
        threads = int(getattr(cfg, "embed_threads", 2) or 2)
    except Exception:
        threads = 2
    try:
        return LocalOnnxEmbedder(model_dir, max_len=max_len, query_prefix=prefix,
                                 num_threads=threads)
    except Exception:
        return None
