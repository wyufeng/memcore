"""向量化：本地哈希向量（默认，零依赖）+ 可选内网 embedding 网关。

本地哈希向量是 signed hashing trick 的字符 n-gram + 词元混合表示，
对中英文都可用，无需下载模型，适合内网离线环境。
"""

from __future__ import annotations

import array
import hashlib
import math
import re
import urllib.error
import urllib.request
from typing import List, Optional, Sequence

TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", re.U)

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was", "were",
    "be", "been", "it", "this", "that", "for", "on", "with", "as", "at", "by",
    "的", "了", "是", "在", "和", "有", "就", "都", "而", "及", "与", "也", "我",
    "你", "他", "她", "它", "这", "那", "个", "吗", "呢", "吧", "啊", "把",
}


def tokenize(text: str) -> List[str]:
    """中英文混合分词：英文按词，中文按字 + 二元组。"""
    if not text:
        return []
    lowered = text.lower()
    tokens = [t for t in TOKEN_RE.findall(lowered) if t not in STOPWORDS]
    # 中文相邻字构成二元组，提升短语命中率
    chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    for a, b in zip(chars, chars[1:]):
        tokens.append(a + b)
    return tokens


def _hash_to_dims(tokens: Sequence[str], dim: int) -> List[float]:
    vec = [0.0] * dim
    for tok in tokens:
        h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "little") % dim
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


class HashEmbedder:
    """本地哈希向量提供方。"""

    name = "hash"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, text: str) -> List[float]:
        return _hash_to_dims(tokenize(text), self.dim)

    def embed_many(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


class RemoteEmbedder:
    """OpenAI 兼容 embedding 网关（内网自建服务）。"""

    name = "remote"

    def __init__(self, base_url: str, api_key: str, model: str, dim: int = 256,
                 timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout = timeout

    def embed(self, text: str) -> List[float]:
        payload = {
            "model": self.model,
            "input": text,
        }
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=__import__("json").dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = __import__("json").loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return _hash_to_dims(tokenize(text), self.dim)
        try:
            vec = data["data"][0]["embedding"]
            return [float(x) for x in vec]
        except (KeyError, IndexError, TypeError, ValueError):
            return _hash_to_dims(tokenize(text), self.dim)

    def embed_many(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


def get_embedder(cfg) -> "HashEmbedder | RemoteEmbedder | object":
    """按配置选择 embedding 提供方。

    provider:
    - auto   : 有本地模型就用本地模型，否则退回哈希向量（默认）
    - local  : 强制本地模型；模型缺失则退回哈希向量
    - hash   : 只用内置哈希向量
    - remote : 只用内网 OpenAI 兼容网关；不可用时退回哈希向量
    """
    provider = (getattr(cfg, "embed_provider", "auto") or "auto").lower()

    if provider == "hash":
        return HashEmbedder(dim=cfg.embed_dim)

    if provider == "remote":
        if cfg.embed_base_url and cfg.embed_model:
            return RemoteEmbedder(
                cfg.embed_base_url, cfg.embed_api_key, cfg.embed_model,
                dim=cfg.embed_dim, timeout=cfg.llm_timeout,
            )
        return HashEmbedder(dim=cfg.embed_dim)

    # auto / local：先试本地 ONNX 模型
    try:
        from .embed_local import try_local_embedder

        local = try_local_embedder(cfg)
        if local is not None:
            return local
    except Exception:
        pass

    if provider == "local":
        # 明确要求 local 但不可用：退回远程网关（若配置了），否则哈希
        if cfg.embed_base_url and cfg.embed_model:
            return RemoteEmbedder(
                cfg.embed_base_url, cfg.embed_api_key, cfg.embed_model,
                dim=cfg.embed_dim, timeout=cfg.llm_timeout,
            )
        return HashEmbedder(dim=cfg.embed_dim)

    # auto：本地模型不可用时才考虑内网网关
    if cfg.embed_base_url and cfg.embed_model:
        return RemoteEmbedder(
            cfg.embed_base_url, cfg.embed_api_key, cfg.embed_model,
            dim=cfg.embed_dim, timeout=cfg.llm_timeout,
        )
    return HashEmbedder(dim=cfg.embed_dim)


def pack_vector(vec: Sequence[float]) -> bytes:
    arr = array.array("f", [float(x) for x in vec])
    if sys_is_big_endian():
        arr.byteswap()
    return arr.tobytes()


def unpack_vector(blob: bytes) -> List[float]:
    arr = array.array("f")
    arr.frombytes(blob)
    if sys_is_big_endian():
        arr.byteswap()
    return list(arr)


def sys_is_big_endian() -> bool:
    import sys

    return sys.byteorder == "big"


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """已归一化向量的余弦相似度；维度不同则返回 0。"""
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    for x, y in zip(a, b):
        dot += x * y
    return max(-1.0, min(1.0, dot))


def serialize_vecs(vecs: Sequence[Sequence[float]]) -> List[bytes]:
    return [pack_vector(v) for v in vecs]


def mean_vector(vecs: Sequence[Sequence[float]]) -> Optional[List[float]]:
    if not vecs:
        return None
    dim = len(vecs[0])
    acc = [0.0] * dim
    for v in vecs:
        if len(v) != dim:
            continue
        for i, x in enumerate(v):
            acc[i] += x
    norm = math.sqrt(sum(x * x for x in acc))
    if norm <= 0:
        return acc
    return [x / norm for x in acc]
