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
        stripped = s.strip()
        if stripped in ("/", "\\"):
            # 根目录没有名字，退回当前工作目录，避免落到 global
            name = Path.cwd().name
        else:
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
    # 抽取来源提示：配了抽取模型却没生效时必须看得见
    if getattr(distillmod, "LAST_SOURCE", "rule") == "llm":
        print("（来源：抽取模型）")
    else:
        err = getattr(distillmod, "LAST_LLM_ERROR", "")
        try:
            eff = cfg.effective_extract()
            if eff.get("enabled"):
                print(f"（来源：内置规则抽取 —— 抽取模型未生效{f'：{err}' if err else ''}）")
        except Exception:
            pass
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
        if getattr(args, "probe", False):
            print(f"  抽取模型探活: {_probe_extract(ex)}")
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
        path = _find_host(host)
        print(f"  {host:<11}: {path or '未安装'}")
    st = store.stats()
    print(f"  memories   : {st['active']} 活跃 / {st['archived']} 归档")
    if getattr(args, "mem", False):
        print()
        for line in mem_report():
            print(f"  {line}")
    return 0


def mem_report() -> list:
    """内存体检：报告系统内存、swap、以及本进程/本地模型的内存占用。

    在 1~2GB 的小内存机器上，memcore 本身（~110MB）+ 宿主（opencode ~420MB）
    很容易把系统压到 "Under memory pressure"。这个报告用来快速判断
    内存到底被谁吃了，而不是靠猜。
    """
    lines = []

    total_mb = avail_mb = None
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            info = {}
            for line in fh:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.split()[0])  # kB
        total_mb = info.get("MemTotal", 0) / 1024
        avail_mb = info.get("MemAvailable", 0) / 1024
        used_mb = (info.get("MemTotal", 0) - info.get("MemAvailable", 0)) / 1024
        lines.append(
            f"系统内存    : 总 {total_mb:.0f}MB / 已用 {used_mb:.0f}MB / 可用 {avail_mb:.0f}MB"
        )
    except Exception:
        lines.append("系统内存    : 无法读取 /proc/meminfo（非 Linux）")

    try:
        swap_total = swap_free = 0
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("SwapTotal:"):
                    swap_total = int(line.split()[1])
                elif line.startswith("SwapFree:"):
                    swap_free = int(line.split()[1])
        if swap_total:
            used = (swap_total - swap_free) / 1024
            lines.append(
                f"swap        : 总 {swap_total/1024:.0f}MB / 已用 {used:.0f}MB"
                + ("  ⚠ 已在换页，OOM 风险高" if used > 32 else "")
            )
        else:
            lines.append("swap        : 未配置（无 swap 缓冲，内存耗尽会直接触发 OOM kill）")
    except Exception:
        pass

    lines.append(f"memcore 进程: RSS {_self_rss_mb():.0f}MB")

    try:
        from .embed_local import try_local_embedder

        before = _self_rss_mb()
        emb = try_local_embedder(None)
        after = _self_rss_mb()
        if emb is not None:
            lines.append(
                f"本地向量模型: 加载后进程 RSS {after:.0f}MB"
                f"（模型自身约 +{max(0.0, after - before):.0f}MB，dim={emb.dim}）"
            )
            del emb
        else:
            lines.append("本地向量模型: 无法加载（onnxruntime 缺失或模型文件损坏）")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"本地向量模型: 测量失败 ({exc})")

    if total_mb and avail_mb is not None:
        if total_mb < 2048:
            lines.append(
                f"⚠ 风险      : 机器总内存仅 {total_mb:.0f}MB，"
                "宿主 + memcore + 其它服务易触发内存压力；"
                "建议 embed_threads=1，并停用重复的嵌入服务"
            )
        elif avail_mb < 256:
            lines.append(f"⚠ 风险      : 可用内存仅 {avail_mb:.0f}MB，接近耗尽")

    return lines


def _self_rss_mb() -> float:
    """当前进程 RSS（MB），跨平台，无需 psutil。"""
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
            fn = ctypes.windll.kernel32.K32GetProcessMemoryInfo
            fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
            fn.restype = ctypes.c_int
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if fn(handle, ctypes.byref(pmc), pmc.cb):
                return pmc.WorkingSetSize / (1024 * 1024)
        except Exception:
            pass
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024
    except Exception:
        return 0.0


def _find_host(name: str) -> str:
    """查找宿主可执行文件。

    非交互 shell 不读 .bashrc，npm 全局 bin 常常不在 PATH 里，
    只用 shutil.which 会误判成"未安装"。
    """
    p = shutil.which(name)
    if p:
        return p
    home = Path.home()
    for d in (home / ".npm-global" / "bin", home / ".bun" / "bin",
              home / ".local" / "bin", home / ".volta" / "bin",
              home / ".yarn" / "bin"):
        cand = d / name
        try:
            if cand.exists() and os.access(str(cand), os.X_OK):
                return str(cand)
        except OSError:
            continue
    return ""


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


def _probe_extract(ex: dict) -> str:
    """真实调用一次抽取模型，验证端点可达且能解析出条目。"""
    try:
        items = distillmod.llm_extract(
            "用户: 我偏好使用简体中文。",
            ex.get("base_url", ""),
            ex.get("api_key", ""),
            ex.get("model", ""),
            timeout=min(float(ex.get("timeout") or 60.0), 30.0),
            max_items=3,
            temperature=float(ex.get("temperature", 0.1)),
        )
    except Exception as exc:  # noqa: BLE001
        return f"探活异常 ({exc})"
    if items:
        return f"可用（返回 {len(items)} 条）"
    err = getattr(distillmod, "LAST_LLM_ERROR", "") or "未知原因"
    return f"不可用（{err}）"


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
    o.add_argument("--probe", action="store_true",
                   help="额外真实调用一次抽取模型，验证端点可达")
    o.add_argument("--mem", action="store_true",
                   help="内存体检：系统内存/swap/本地模型占用与 OOM 风险")
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
