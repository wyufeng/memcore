"""memcore 命令行入口。

子命令：
  remember   记一条记忆
  recall     检索记忆
  list       列出记忆
  forget     删除记忆
  distill    从文本提炼记忆
  context    输出可注入 prompt 的记忆块
  stats      统计
  mcp        启动 MCP stdio 服务
  install    生成本机配置（Claude Code / opencode）
  doctor     自检环境
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import db as dbmod
from . import distill as distillmod
from . import embed as embedmod
from . import retrieve as retrievemod
from .config import Config, load_config
from .store import MemoryStore

BANNER = "memcore - 内网免 sudo 长记忆内核"


def build_store(cfg: Optional[Config] = None) -> tuple:
    cfg = cfg or load_config()
    try:
        cfg.ensure_dirs()
    except OSError as exc:
        print(f"error: 无法创建数据目录 {cfg.home}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    try:
        con = dbmod.connect(cfg.db_path)
        dbmod.init_db(con)
    except sqlite3.Error as exc:
        print(f"error: 无法打开数据库 {cfg.db_path}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    store = MemoryStore(con, embedder=embedmod.get_embedder(cfg), cfg=cfg)
    return cfg, store


def project_scope(explicit: Optional[str] = None) -> str:
    """把 scope 参数归一化成 project:<目录名>。

    绝对路径会被转成目录名（/home/ubuntu -> project:ubuntu），
    这样 opencode 插件传绝对路径、用户手传目录名，结果一致。
    """
    if explicit:
        s = str(explicit)
        if s.startswith(("project:", "session:", "global")):
            return s
        p = Path(s).expanduser()
        name = p.name if (p.is_absolute() or len(p.parts) > 1) else s
        return f"project:{name}" if name else "global"
    cwd = Path.cwd().name
    return f"project:{cwd}" if cwd else "global"


def cmd_remember(args, cfg, store) -> int:
    mem = store.add(
        content=args.text, kind=args.kind, scope=project_scope(args.scope),
        title=args.title or "", tags=args.tags or [], importance=args.importance,
        source=args.source or "cli", dedup_threshold=cfg.dedup_threshold,
    )
    print(f"✓ [{mem['kind']}] {mem['content']}")
    print(f"  id={mem['id']} scope={mem['scope']} importance={mem['importance']:.2f}")
    return 0


def cmd_recall(args, cfg, store) -> int:
    hits = retrievemod.retrieve(
        store, args.query, limit=args.limit, scope=project_scope(args.scope),
        kind=args.kind, tags=args.tags)
    store.mark_used([m["id"] for m in hits])
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print("(没有找到相关记忆)")
        return 0
    for m in hits:
        tags = ",".join(map(str, m.get("tags") or []))
        print(f"[{m['kind']}|score={m['score']:.3f}"
              f"{'|tags=' + tags if tags else ''}] {m['content']}")
        print(f"    id={m['id']} scope={m['scope']} age={m['age_days']}d")
    return 0


def cmd_list(args, cfg, store) -> int:
    items = store.list(scope=args.scope, kind=args.kind, tags=args.tags,
                       include_archived=args.include_archived, limit=args.limit)
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    if not items:
        print("(记忆库为空)")
        return 0
    for m in items:
        tags = ",".join(map(str, m.get("tags") or []))
        print(f"{m['id']} [{m['kind']}|{m['scope']}|imp={m['importance']:.2f}"
              f"{'|pin' if m['pinned'] else ''}] {m['content']}"
              f"{'  tags=' + tags if tags else ''}")
    return 0


def cmd_forget(args, cfg, store) -> int:
    ok = store.delete(args.id, hard=args.hard)
    print("已删除" if ok else "未找到该记忆")
    return 0 if ok else 1


def cmd_distill(args, cfg, store) -> int:
    text = args.text
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    items = distillmod.distill(text, cfg, args.max_items)
    if not items:
        print("(没有提炼出可用记忆)")
        return 0
    saved = []
    for it in items:
        mem = store.add(content=it["content"], kind=it["kind"],
                        scope=project_scope(args.scope), title=it.get("title", ""),
                        importance=it["importance"], source="distill",
                        dedup_threshold=cfg.dedup_threshold)
        saved.append(mem)
    for m in saved:
        print(f"✓ [{m['kind']}] {m['content']}")
    print(f"共 {len(saved)} 条")
    return 0


def cmd_context(args, cfg, store) -> int:
    query = args.query or ""
    hits = retrievemod.retrieve(store, query, limit=args.limit or cfg.inject_limit,
                                scope=project_scope(args.scope))
    store.mark_used([m["id"] for m in hits])
    print(retrievemod.format_context(hits, cfg.inject_max_chars) or "(无)")
    return 0


def cmd_stats(args, cfg, store) -> int:
    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_decay(args, cfg, store) -> int:
    n = store.decay_floor_sweep(cfg.decay_halflife_days, cfg.decay_floor)
    print(f"归档了 {n} 条低强度记忆")
    return 0


def cmd_doctor(args, cfg, store) -> int:
    print(BANNER)
    print(f"  python     : {sys.version.split()[0]}")
    print(f"  home       : {cfg.home}")
    print(f"  db         : {cfg.db_path} ({'存在' if cfg.db_path.exists() else '待创建'})")
    print(f"  embedder   : {getattr(store.embedder, 'name', 'hash')}")
    print(f"  embed prov : {cfg.embed_provider}")
    print(f"  本地向量模型: {_local_model_status(cfg)}")
    ex = cfg.effective_extract()
    if ex["enabled"]:
        print(f"  抽取模型   : {ex['model']} @ {ex['base_url']} (temp={ex['temperature']})")
    else:
        print("  抽取模型   : 未配置（走规则抽取）")
    print(f"  llm        : {'已配置 ' + cfg.llm_model if cfg.use_llm and cfg.llm_model else '未配置（走规则抽取）'}")
    try:
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        print("  fts5       : 可用")
    except Exception as exc:
        print(f"  fts5       : 不可用 ({exc})")
    for host in ("claude", "opencode"):
        path = shutil.which(host)
        print(f"  {host:<11}: {path or '未安装'}")
    st = store.stats()
    print(f"  memories   : {st['active']} 活跃 / {st['archived']} 归档")
    return 0


def _local_model_status(cfg) -> str:
    """检查本地向量模型是否可用（不加载模型，只探路径与文件）。"""
    try:
        from .embed_local import describe_local_model
        info = describe_local_model(cfg)
    except Exception as exc:  # noqa: BLE001
        return f"检测失败 ({exc})"
    if not info.get("found"):
        return f"未找到（将退回哈希向量）{info.get('hint', '')}"
    return (
        f"{info.get('name', 'local')} @ {info.get('path')} "
        f"(dim={info.get('dim')}, size={info.get('size_mb')}MB)"
    )


# 允许通过 CLI 修改的配置项
CONFIG_KEYS = {
    "llm_base_url": str,
    "llm_api_key": str,
    "llm_model": str,
    "use_llm": bool,
    "extract_base_url": str,
    "extract_api_key": str,
    "extract_model": str,
    "extract_temperature": float,
    "extract_timeout": float,
    "extract_max_items": int,
    "extract_prompt_file": str,
    "embed_provider": str,
    "embed_model_path": str,
    "embed_max_len": int,
    "embed_query_prefix": str,
    "embed_threads": int,
    "embed_base_url": str,
    "embed_api_key": str,
    "embed_model": str,
    "embed_dim": int,
}


def _cast_config_value(key: str, raw: str):
    caster = CONFIG_KEYS[key]
    if caster is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if caster is int:
        return int(float(raw))
    if caster is float:
        return float(raw)
    return raw


def cmd_config(args, cfg, store) -> int:
    action = getattr(args, "action", "show")
    if action == "show":
        d = cfg.to_dict()
        keys = args.keys or sorted(CONFIG_KEYS.keys())
        for k in keys:
            if k in d:
                print(f"{k} = {d[k]!r}")
        print(f"# 配置文件: {cfg.config_path}")
        return 0

    if action == "set":
        if not args.key:
            print("error: 需要 key=value 参数", file=sys.stderr)
            return 2
        items = args.key if isinstance(args.key, list) else [args.key]
        changed = {}
        for item in items:
            if "=" not in item:
                print(f"error: 非法参数 {item!r}，应为 key=value", file=sys.stderr)
                return 2
            k, v = item.split("=", 1)
            k = k.strip()
            if k not in CONFIG_KEYS:
                print(f"error: 未知配置项 {k!r}", file=sys.stderr)
                return 2
            try:
                changed[k] = _cast_config_value(k, v)
            except (TypeError, ValueError) as exc:
                print(f"error: {k} 取值非法: {exc}", file=sys.stderr)
                return 2
        for k, v in changed.items():
            setattr(cfg, k, v)
        cfg.save()
        for k, v in changed.items():
            shown = "***" if k.endswith("api_key") and v else v
            print(f"{k} = {shown!r}")
        print(f"# 已保存到 {cfg.config_path}")
        return 0

    if action == "unset":
        if not args.key:
            print("error: 需要 key", file=sys.stderr)
            return 2
        for k in args.key:
            if k not in CONFIG_KEYS:
                print(f"error: 未知配置项 {k!r}", file=sys.stderr)
                return 2
            setattr(cfg, k, "" if CONFIG_KEYS[k] is str else CONFIG_KEYS[k](0))
        cfg.save()
        print(f"已清空 {args.key} -> {cfg.config_path}")
        return 0

    if action == "keys":
        for k in sorted(CONFIG_KEYS.keys()):
            print(k)
        return 0

    return 2


def cmd_reembed(args, cfg, store) -> int:
    """切换 embedding 提供方后重建全部向量。"""
    res = store.reembed(include_archived=not args.active_only)
    print(json.dumps(res, ensure_ascii=False))
    if res["failed"]:
        print(f"警告: {res['failed']} 条重建失败", file=sys.stderr)
    return 0


def cmd_mcp(args, cfg, store) -> int:
    from .mcp import MCPServer
    MCPServer(cfg).serve()
    return 0


def cmd_install(args, cfg, store) -> int:
    from .install import install_claude, install_opencode, print_instructions
    done = []
    if args.target in ("claude", "all"):
        done.append(("claude", install_claude(cfg, dry_run=args.dry_run)))
    if args.target in ("opencode", "all"):
        done.append(("opencode", install_opencode(cfg, dry_run=args.dry_run)))
    for name, res in done:
        print(f"[{name}] " + ("dry-run: " if args.dry_run else "") + json.dumps(res, ensure_ascii=False))
    print_instructions(cfg)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memcore", description=BANNER)
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("remember", help="记一条记忆")
    r.add_argument("text")
    r.add_argument("--kind", default="fact",
                   choices=["fact", "pref", "decision", "env", "task"])
    r.add_argument("--title", default="")
    r.add_argument("--tags", nargs="*", default=[])
    r.add_argument("--scope", default="")
    r.add_argument("--importance", type=float, default=0.5)
    r.add_argument("--source", default="")
    r.set_defaults(func=cmd_remember)

    c = sub.add_parser("recall", help="检索记忆")
    c.add_argument("query")
    c.add_argument("--limit", type=int, default=0)
    c.add_argument("--scope", default="")
    c.add_argument("--kind", default="")
    c.add_argument("--tags", nargs="*", default=[])
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_recall)

    l = sub.add_parser("list", help="列出记忆")
    l.add_argument("--scope", default="")
    l.add_argument("--kind", default="")
    l.add_argument("--tags", nargs="*", default=[])
    l.add_argument("--limit", type=int, default=50)
    l.add_argument("--include-archived", action="store_true")
    l.add_argument("--json", action="store_true")
    l.set_defaults(func=cmd_list)

    f = sub.add_parser("forget", help="删除记忆")
    f.add_argument("id")
    f.add_argument("--hard", action="store_true", help="物理删除")
    f.set_defaults(func=cmd_forget)

    d = sub.add_parser("distill", help="从文本提炼记忆")
    d.add_argument("text", nargs="?", default="")
    d.add_argument("--file", default="")
    d.add_argument("--scope", default="")
    d.add_argument("--max-items", type=int, default=12)
    d.set_defaults(func=cmd_distill)

    x = sub.add_parser("context", help="输出可注入 prompt 的记忆块")
    x.add_argument("query", nargs="?", default="")
    x.add_argument("--limit", type=int, default=0)
    x.add_argument("--scope", default="")
    x.set_defaults(func=cmd_context)

    s = sub.add_parser("stats", help="统计")
    s.set_defaults(func=cmd_stats)

    y = sub.add_parser("decay", help="按衰减强度归档冷记忆")
    y.set_defaults(func=cmd_decay)

    o = sub.add_parser("doctor", help="环境自检")
    o.set_defaults(func=cmd_doctor)

    rb = sub.add_parser("reembed", help="用当前向量模型重建全部向量")
    rb.add_argument("--active-only", action="store_true", help="只处理未归档记忆")
    rb.set_defaults(func=cmd_reembed)

    cfgp = sub.add_parser("config", help="查看/修改配置")
    cfgp.add_argument("action", nargs="?", default="show",
                      choices=["show", "set", "unset", "keys"])
    cfgp.add_argument("key", nargs="*", help="key=value（set）或 key（unset）")
    cfgp.add_argument("--keys", nargs="*", default=None, help="show 时只显示这些键")
    cfgp.set_defaults(func=cmd_config)

    m = sub.add_parser("mcp", help="启动 MCP stdio 服务")
    m.set_defaults(func=cmd_mcp)

    i = sub.add_parser("install", help="生成本机配置")
    i.add_argument("--target", default="all", choices=["claude", "opencode", "all"])
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(func=cmd_install)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    cfg, store = build_store()
    try:
        return int(args.func(args, cfg, store) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        if os.environ.get("MEMCORE_DEBUG"):
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
