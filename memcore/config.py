"""配置：全部走环境变量 + JSON 配置文件，无需 root 权限。

优先级：环境变量 > 配置文件 > 内置默认值。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Optional

APP_NAME = "memcore"

DEFAULT_HOME = Path.home() / ".local" / "share" / APP_NAME
DEFAULT_CONFIG_PATH = Path.home() / ".config" / APP_NAME / "config.json"


_BARE_ENV_OK: set = set()


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(f"MEMCORE_{name.upper()}")
    if v in (None, "") and name.upper() in _BARE_ENV_OK:
        v = os.environ.get(name.upper())
    if v in (None, "") and name.upper() == "HOME_DIR":
        v = os.environ.get("MEMCORE_HOME")
    return v if v not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    """运行时配置。"""

    home: Path = field(default_factory=lambda: Path(_env("HOME_DIR", str(DEFAULT_HOME))))
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", "")))
    config_path: Path = field(default_factory=lambda: Path(_env("CONFIG_PATH", str(DEFAULT_CONFIG_PATH))))

    # 检索权重
    w_lexical: float = field(default_factory=lambda: _env_float("W_LEXICAL", 1.0))
    w_vector: float = field(default_factory=lambda: _env_float("W_VECTOR", 0.8))
    w_importance: float = field(default_factory=lambda: _env_float("W_IMPORTANCE", 0.5))
    w_recency: float = field(default_factory=lambda: _env_float("W_RECENCY", 0.35))
    w_usage: float = field(default_factory=lambda: _env_float("W_USAGE", 0.25))
    recency_halflife_days: float = field(default_factory=lambda: _env_float("RECENCY_HALFLIFE", 30.0))

    # 检索规模
    recall_limit: int = field(default_factory=lambda: int(_env_float("RECALL_LIMIT", 8)))
    recall_candidates: int = field(default_factory=lambda: int(_env_float("RECALL_CANDIDATES", 60)))
    dedup_threshold: float = field(default_factory=lambda: _env_float("DEDUP_THRESHOLD", 0.88))

    # 记忆衰减：半衰期(天) 与强度下限
    decay_halflife_days: float = field(default_factory=lambda: _env_float("DECAY_HALFLIFE", 45.0))
    decay_floor: float = field(default_factory=lambda: _env_float("DECAY_FLOOR", 0.15))

    # 可选：内网 LLM 网关（OpenAI 兼容）。留空则走纯本地规则蒸馏。
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "") or "")
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", "") or "")
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "") or "")
    llm_timeout: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT", 60.0))
    use_llm: bool = field(default_factory=lambda: _env_bool("USE_LLM", False))

    # 可选：内网 embedding 网关。留空则用本地哈希向量。
    embed_base_url: str = field(default_factory=lambda: _env("EMBED_BASE_URL", "") or "")
    embed_api_key: str = field(default_factory=lambda: _env("EMBED_API_KEY", "") or "")
    embed_model: str = field(default_factory=lambda: _env("EMBED_MODEL", "") or "")
    embed_dim: int = field(default_factory=lambda: int(_env_float("EMBED_DIM", 256)))

    # 自动抓取
    auto_ingest: bool = field(default_factory=lambda: _env_bool("AUTO_INGEST", True))
    auto_capture: bool = field(default_factory=lambda: _env_bool("AUTO_CAPTURE", True))
    inject_limit: int = field(default_factory=lambda: int(_env_float("INJECT_LIMIT", 12)))
    inject_max_chars: int = field(default_factory=lambda: int(_env_float("INJECT_MAX_CHARS", 4000)))

    # 项目作用域：默认按当前工作目录名做项目隔离
    default_scope: str = field(default_factory=lambda: _env("DEFAULT_SCOPE", "global") or "global")

    def __post_init__(self) -> None:
        self.home = Path(self.home).expanduser()
        if not self.db_path or str(self.db_path) in (".", "./"):
            self.db_path = self.home / "memories.db"
        else:
            self.db_path = Path(self.db_path).expanduser()
        self.config_path = Path(self.config_path).expanduser()

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["home"] = str(self.home)
        d["db_path"] = str(self.db_path)
        d["config_path"] = str(self.config_path)
        # 不把密钥写进明文配置
        for k in ("llm_api_key", "embed_api_key"):
            d[k] = "***" if d.get(k) else ""
        return d

    def save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "home": str(self.home),
            "db_path": str(self.db_path),
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "embed_base_url": self.embed_base_url,
            "embed_model": self.embed_model,
            "use_llm": self.use_llm,
            "w_lexical": self.w_lexical,
            "w_vector": self.w_vector,
            "w_importance": self.w_importance,
            "w_recency": self.w_recency,
            "recency_halflife_days": self.recency_halflife_days,
            "recall_limit": self.recall_limit,
            "decay_halflife_days": self.decay_halflife_days,
        }
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def load_config(config_path: Optional[str] = None) -> Config:
    """读取配置：先看 JSON 文件，再用环境变量覆盖。"""
    path = Path(config_path).expanduser() if config_path else Config().config_path
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    cfg = Config()
    for key, value in data.items():
        if not hasattr(cfg, key):
            continue
        if key in ("home", "db_path", "config_path"):
            value = Path(value).expanduser()
        setattr(cfg, key, value)

    # 环境变量优先
    cfg.home = Path(_env("HOME_DIR", str(cfg.home))).expanduser()
    cfg.db_path = Path(_env("DB_PATH", str(cfg.db_path))).expanduser()
    cfg.llm_base_url = _env("LLM_BASE_URL", cfg.llm_base_url) or ""
    cfg.llm_api_key = _env("LLM_API_KEY", cfg.llm_api_key) or ""
    cfg.llm_model = _env("LLM_MODEL", cfg.llm_model) or ""
    cfg.embed_base_url = _env("EMBED_BASE_URL", cfg.embed_base_url) or ""
    cfg.embed_api_key = _env("EMBED_API_KEY", cfg.embed_api_key) or ""
    cfg.embed_model = _env("EMBED_MODEL", cfg.embed_model) or ""
    cfg.use_llm = _env_bool("USE_LLM", cfg.use_llm)
    cfg.auto_ingest = _env_bool("AUTO_INGEST", cfg.auto_ingest)
    cfg.auto_capture = _env_bool("AUTO_CAPTURE", cfg.auto_capture)
    cfg.recall_limit = int(_env_float("RECALL_LIMIT", cfg.recall_limit))
    cfg.w_lexical = _env_float("W_LEXICAL", cfg.w_lexical)
    cfg.w_vector = _env_float("W_VECTOR", cfg.w_vector)
    cfg.w_importance = _env_float("W_IMPORTANCE", cfg.w_importance)
    cfg.w_recency = _env_float("W_RECENCY", cfg.w_recency)
    cfg.decay_halflife_days = _env_float("DECAY_HALFLIFE", cfg.decay_halflife_days)
    return cfg
