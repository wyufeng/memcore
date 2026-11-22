"""Claude Code hooks 实现。

Claude Code 通过 stdin 传入 JSON（session_id / transcript_path / cwd / prompt），
我们据此注入记忆或回写记忆。所有输出写到 stdout，宿主会把它当作附加上下文。

用法：
    python3 -m memcore.hooks session_start
    python3 -m memcore.hooks prompt_submit
    python3 -m memcore.hooks stop
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import db as dbmod
from . import distill as distillmod
from . import embed as embedmod
from . import retrieve as retrievemod
from .config import load_config
from .store import MemoryStore


def _read_stdin() -> Dict[str, Any]:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _store(cfg) -> MemoryStore:
    cfg.ensure_dirs()
    con = dbmod.connect(cfg.db_path)
    dbmod.init_db(con)
    return MemoryStore(con, embedder=embedmod.get_embedder(cfg), cfg=cfg)


def _scope_of(payload: Dict[str, Any]) -> str:
    cwd = payload.get("cwd") or payload.get("workspace") or os.getcwd()
    name = Path(cwd).name or "global"
    return f"project:{name}"


def _transcript_text(path: Optional[str], max_chars: int = 40000) -> str:
    """从 Claude Code 的 transcript jsonl 里抽取可读文本。"""
    if not path:
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    chunks: List[str] = []
    total = 0
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    for line in lines[-400:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        role = obj.get("type") or obj.get("role") or ""
        msg = obj.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text += part.get("text") or ""
        elif isinstance(obj.get("text"), str):
            text = obj["text"]
        text = (text or "").strip()
        if text:
            chunks.append(f"{role}: {text}")
            total += len(text)
            if total > max_chars:
                break
    return "\n".join(chunks)[-max_chars:]


def hook_session_start(payload: Dict[str, Any]) -> int:
    cfg = load_config()
    if not cfg.auto_capture:
        return 0
    store = _store(cfg)
    scope = _scope_of(payload)
    query = payload.get("prompt") or payload.get("cwd") or ""
    hits = retrievemod.retrieve(store, query, limit=cfg.inject_limit, scope=scope)
    store.mark_used([m["id"] for m in hits])
    sid = payload.get("session_id")
    if sid:
        store.start_session(agent="claude-code", project=scope, session_id=sid)
    text = retrievemod.format_context(hits, cfg.inject_max_chars, "memcore 长期记忆（自动注入）")
    if text:
        sys.stdout.write(text)
    return 0


def hook_prompt_submit(payload: Dict[str, Any]) -> int:
    cfg = load_config()
    if not cfg.auto_capture:
        return 0
    prompt = payload.get("prompt") or ""
    if not prompt.strip():
        return 0
    store = _store(cfg)
    scope = _scope_of(payload)
    sid = payload.get("session_id")
    store.add_episode("user", prompt, sid)
    hits = retrievemod.retrieve(store, prompt, limit=cfg.inject_limit, scope=scope)
    store.mark_used([m["id"] for m in hits])
    text = retrievemod.format_context(hits, cfg.inject_max_chars, "memcore 相关记忆")
    if text:
        sys.stdout.write(text)
    return 0


def hook_stop(payload: Dict[str, Any]) -> int:
    cfg = load_config()
    if not cfg.auto_capture:
        return 0
    store = _store(cfg)
    sid = payload.get("session_id")
    scope = _scope_of(payload)
    text = _transcript_text(payload.get("transcript_path"))
    if not text.strip():
        return 0
    items = distillmod.distill(text, cfg, max_items=10)
    if not items:
        return 0
    for it in items:
        store.add(content=it["content"], kind=it["kind"], scope=scope,
                  title=it.get("title", ""), importance=it["importance"],
                  source="hook", session_id=sid, dedup_threshold=cfg.dedup_threshold)
    if sid:
        store.end_session(sid, summary=f"distilled {len(items)} memories")
    return 0


HOOKS = {
    "session_start": hook_session_start,
    "prompt_submit": hook_prompt_submit,
    "stop": hook_stop,
}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print("usage: python3 -m memcore.hooks <session_start|prompt_submit|stop>", file=sys.stderr)
        return 2
    name = argv[0]
    fn = HOOKS.get(name)
    if not fn:
        print(f"unknown hook: {name}", file=sys.stderr)
        return 2
    payload = _read_stdin()
    try:
        return int(fn(payload) or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"[memcore] hook {name} failed: {exc}", file=sys.stderr)
        return 0  # hook 失败不应打断宿主


if __name__ == "__main__":
    sys.exit(main())
