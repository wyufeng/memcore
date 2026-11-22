"""MCP (Model Context Protocol) stdio 服务端。

实现 JSON-RPC 2.0 over stdio，兼容 MCP 2024-11-05 / 2025-06-18 协议，
暴露记忆相关工具给 Claude Code / opencode 等宿主使用。

注意：stdout 只能写协议帧，所有日志必须走 stderr。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import traceback
from typing import Any, Dict, List, Optional

from . import db as dbmod
from . import distill as distillmod
from . import embed as embedmod
from . import retrieve as retrievemod
from .config import Config, load_config
from .store import MemoryStore

PROTOCOL_VERSION = "2024-11-05"

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "memcore_remember",
        "description": "写入一条长期记忆（偏好/决定/环境事实/任务）。会自动去重合并。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "记忆正文，一句话"},
                "kind": {"type": "string",
                         "enum": ["fact", "pref", "decision", "env", "task"],
                         "description": "记忆类型，默认 fact"},
                "title": {"type": "string", "description": "可选短标题"},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "可选标签"},
                "scope": {"type": "string",
                          "description": "global 或 project:<name>，默认 global"},
                "importance": {"type": "number", "description": "0-1，默认 0.5"},
                "session_id": {"type": "string", "description": "可选会话 ID"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "memcore_recall",
        "description": "按当前上下文检索相关长期记忆，返回带相关性分数的列表。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索文本"},
                "limit": {"type": "integer", "description": "返回条数，默认 8"},
                "scope": {"type": "string", "description": "global 或 project:<name>"},
                "kind": {"type": "string", "description": "按类型过滤"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "as_context": {"type": "boolean",
                               "description": "true 时返回可直接注入 prompt 的文本块"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memcore_list",
        "description": "列出已存储的记忆（可按类型/作用域/标签过滤）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string"},
                "kind": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
                "include_archived": {"type": "boolean"},
            },
        },
    },
    {
        "name": "memcore_forget",
        "description": "按 ID 删除或归档记忆。默认软删除（归档），hard=true 物理删除。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "hard": {"type": "boolean", "description": "true 物理删除"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "memcore_update",
        "description": "更新已有记忆的内容/重要度/标签/置顶状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "content": {"type": "string"},
                "kind": {"type": "string"},
                "scope": {"type": "string"},
                "title": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "importance": {"type": "number"},
                "pinned": {"type": "boolean"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "memcore_distill",
        "description": "把一段原始对话/文本提炼成结构化记忆条目并写入。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "原始文本"},
                "scope": {"type": "string"},
                "session_id": {"type": "string"},
                "max_items": {"type": "integer"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "memcore_stats",
        "description": "统计记忆库规模、类型分布与使用情况。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memcore_session_start",
        "description": "开启一次会话，返回会话 ID。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "宿主名，如 claude-code / opencode"},
                "project": {"type": "string"},
            },
        },
    },
]


def _log(msg: str) -> None:
    print(f"[memcore] {msg}", file=sys.stderr, flush=True)


class MCPServer:
    def __init__(self, cfg: Optional[Config] = None) -> None:
        self.cfg = cfg or load_config()
        try:
            self.cfg.ensure_dirs()
            self.con = dbmod.connect(self.cfg.db_path)
            dbmod.init_db(self.con)
        except (OSError, sqlite3.Error) as exc:
            _log(f"fatal: 无法初始化数据库 {self.cfg.db_path}: {exc}")
            raise
        embedder = embedmod.get_embedder(self.cfg)
        self.store = MemoryStore(self.con, embedder=embedder, cfg=self.cfg)

    # ---------- JSON-RPC ----------

    def handle(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = req.get("method")
        rid = req.get("id")
        params = req.get("params") or {}

        if method == "initialize":
            return self._ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "memcore", "version": "0.1.0"},
                "instructions": (
                    "memcore 提供跨会话长期记忆。开始任务前用 memcore_recall 取回相关记忆；"
                    "发现稳定偏好/决定/环境事实时用 memcore_remember 或 memcore_distill 写入。"
                ),
            })
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return self._ok(rid, {})
        if method == "tools/list":
            return self._ok(rid, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                result = self.call_tool(name, args)
            except Exception as exc:  # noqa: BLE001
                _log(f"tool {name} failed: {exc}")
                result = {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
            return self._ok(rid, result)
        if method == "resources/list":
            return self._ok(rid, {"resources": []})
        if method == "prompts/list":
            return self._ok(rid, {"prompts": []})
        if rid is None:
            return None
        return self._err(rid, -32601, f"method not found: {method}")

    @staticmethod
    def _ok(rid: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    @staticmethod
    def _err(rid: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    # ---------- 工具实现 ----------

    def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name == "memcore_remember":
            mem = self.store.add(
                content=args.get("content", ""),
                kind=args.get("kind", "fact"),
                scope=args.get("scope") or self.cfg.default_scope,
                title=args.get("title", ""),
                tags=args.get("tags") or [],
                importance=float(args.get("importance", 0.5)),
                source=args.get("source", "agent"),
                session_id=args.get("session_id"),
                dedup_threshold=self.cfg.dedup_threshold,
            )
            return self._text(f"已记住 [{mem['kind']}] {mem['content']} (id={mem['id']})", extra=mem)

        if name == "memcore_recall":
            query = args.get("query", "")
            scope = args.get("scope")
            hits = retrievemod.retrieve(
                self.store, query,
                limit=int(args.get("limit") or self.cfg.recall_limit),
                scope=scope, kind=args.get("kind"), tags=args.get("tags"),
            )
            self.store.mark_used([m["id"] for m in hits])
            if args.get("as_context"):
                txt = retrievemod.format_context(hits, self.cfg.inject_max_chars)
                return self._text(txt or "(没有找到相关记忆)")
            if not hits:
                return self._text("(没有找到相关记忆)")
            lines = []
            for m in hits:
                tags = ",".join(map(str, m.get("tags") or []))
                lines.append(
                    f"[{m['kind']}|score={m['score']:.3f}"
                    f"{'|tags=' + tags if tags else ''}] {m['content']}"
                )
            return self._text("\n".join(lines), extra={"count": len(hits)})

        if name == "memcore_list":
            items = self.store.list(
                scope=args.get("scope"), kind=args.get("kind"), tags=args.get("tags"),
                include_archived=bool(args.get("include_archived")),
                limit=int(args.get("limit") or 50),
            )
            if not items:
                return self._text("(记忆库为空)")
            lines = [f"{m['id']} [{m['kind']}|{m['scope']}|imp={m['importance']:.2f}] {m['content']}"
                     for m in items]
            return self._text("\n".join(lines), extra={"count": len(items)})

        if name == "memcore_forget":
            ok = self.store.delete(args.get("id", ""), hard=bool(args.get("hard")))
            return self._text("已删除" if ok else "未找到该记忆", extra={"ok": ok})

        if name == "memcore_update":
            mem = self.store.update(args.get("id", ""), **{
                k: v for k, v in args.items() if k != "id"
            })
            if not mem:
                return self._text("未找到该记忆")
            return self._text(f"已更新 {mem['id']}", extra=mem)

        if name == "memcore_distill":
            text = args.get("text", "")
            items = distillmod.distill(text, self.cfg, int(args.get("max_items") or 12))
            saved = []
            for it in items:
                mem = self.store.add(
                    content=it["content"], kind=it["kind"],
                    scope=args.get("scope") or self.cfg.default_scope,
                    title=it.get("title", ""),
                    importance=it["importance"],
                    source="distill",
                    session_id=args.get("session_id"),
                    dedup_threshold=self.cfg.dedup_threshold,
                )
                saved.append(mem["id"])
            return self._text(f"提炼出 {len(saved)} 条记忆", extra={"ids": saved})

        if name == "memcore_stats":
            st = self.store.stats()
            lines = [f"活跃记忆: {st['active']}", f"已归档: {st['archived']}"]
            for k, v in st["by_kind"].items():
                lines.append(f"  {k}: {v}")
            return self._text("\n".join(lines), extra=st)

        if name == "memcore_session_start":
            sid = self.store.start_session(
                agent=args.get("agent", "unknown"), project=args.get("project"))
            return self._text(f"session_id={sid}", extra={"session_id": sid})

        raise ValueError(f"unknown tool: {name}")

    @staticmethod
    def _text(text: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }
        if extra is not None:
            payload["structuredContent"] = extra
        return payload

    # ---------- 主循环 ----------

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        _log(f"memcore mcp server ready (db={self.cfg.db_path})")
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            try:
                resp = self.handle(req)
            except Exception as exc:  # noqa: BLE001
                _log(traceback.format_exc())
                rid = req.get("id") if isinstance(req, dict) else None
                resp = self._err(rid, -32603, str(exc)) if rid is not None else None
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                stdout.flush()


def main() -> None:
    cfg = load_config()
    MCPServer(cfg).serve()


if __name__ == "__main__":
    main()
