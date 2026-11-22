"""混合检索：FTS5 BM25 词法分 + 向量语义分 + 重要度/时效/使用度加权。

流程：
1. 用查询词生成 FTS5 候选（BM25 排序）
2. 同时在向量表上做全量余弦扫描（内存量级通常 < 10k 条，够用且无额外依赖）
3. 两个候选集融合，按综合分排序
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import db as dbmod
from . import embed as embedmod
from .store import MemoryStore


def _fts_query(text: str) -> str:
    """把自然语言查询转成 FTS5 OR 查询（每个 token 用前缀匹配）。"""
    tokens = embedmod.tokenize(text or "")
    if not tokens:
        return ""
    parts = []
    seen = set()
    for t in tokens:
        t = t.replace('"', "")
        if not t or t in seen:
            continue
        seen.add(t)
        parts.append(f'"{t}"*')
    return " OR ".join(parts)


def lexical_scores(con: sqlite3.Connection, query: str, limit: int) -> Dict[str, float]:
    """BM25 归一化分：越靠前分越高，映射到 0..1。

    只检索 tokens 列——该列存的是我们自己分词后的结果，
    unicode61 会把整段中文当成一个 token，直接匹配 content 列会导致中文召回失败。
    """
    q = _fts_query(query)
    if not q:
        return {}
    try:
        rows = con.execute(
            "SELECT rowid, bm25(memories_fts) AS score FROM memories_fts "
            "WHERE memories_fts MATCH ? ORDER BY score LIMIT ?",
            (q, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    if not rows:
        return {}
    scores = [float(r["score"]) for r in rows]
    best = min(scores)  # bm25() 越小越相关（负值）
    worst = max(scores)
    spread = (worst - best) or 1.0
    out: Dict[str, float] = {}
    for r in rows:
        rel = (worst - float(r["score"])) / spread  # 0..1
        out[str(r["rowid"])] = max(0.0, min(1.0, rel))
    return out


def vector_scores(con: sqlite3.Connection, embedder, query: str,
                  limit: int, scope: Optional[str] = None) -> Dict[str, float]:
    """向量余弦相似度分（0..1，负值截为 0）。"""
    qvec = embedder.embed(query or "")
    if not qvec:
        return {}
    sql = ("SELECT v.mem_id AS mid, v.vec AS vec, v.dim AS dim FROM vectors v "
           "JOIN memories m ON m.id = v.mem_id WHERE m.archived=0")
    args: List[Any] = []
    if scope:
        sql += " AND (m.scope=? OR m.scope='global')"
        args.append(scope)
    rows = con.execute(sql, args).fetchall()
    scored: List[Tuple[str, float]] = []
    for r in rows:
        if int(r["dim"]) != len(qvec):
            continue
        vec = embedmod.unpack_vector(r["vec"])
        sim = embedmod.cosine(qvec, vec)
        scored.append((r["mid"], max(0.0, sim)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return {mid: s for mid, s in scored[: int(limit)]}


def _list_by_score(
    store: MemoryStore,
    limit: int,
    scope: Optional[str] = None,
    kind: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """无查询词时的降级检索：按 重要度 + 时效 + 使用度 排序返回。"""
    cfg = store.cfg
    now = dbmod.now()
    sql = "SELECT * FROM memories WHERE archived=0"
    args: List[Any] = []
    if scope:
        sql += " AND (scope=? OR scope='global')"
        args.append(scope)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    rows = store.con.execute(sql, args).fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        mem = dbmod.row_to_dict(r)
        mem.pop("rowid", None)
        if tags:
            have = set(mem.get("tags") or [])
            if not have.intersection({str(t) for t in tags}):
                continue
        age_days = max(0.0, (now - float(mem.get("last_used_at")
                                        or mem.get("updated_at") or now)) / 86400.0)
        s_rec = math.pow(0.5, age_days / max(1e-6, cfg.recency_halflife_days))
        s_use = 1.0 / (1.0 + math.log1p(float(mem.get("use_count") or 0)))
        pinned = 1.25 if mem.get("pinned") else 1.0
        mem["score"] = round(
            (cfg.w_importance * float(mem.get("importance") or 0.0)
             + cfg.w_recency * s_rec
             + cfg.w_usage * s_use) * pinned, 6)
        mem["score_parts"] = {"lexical": 0.0, "vector": 0.0, "recency": round(s_rec, 4),
                               "importance": round(float(mem.get("importance") or 0.0), 4)}
        mem["age_days"] = round(age_days, 2)
        out.append(mem)
    out.sort(key=lambda m: m["score"], reverse=True)
    return out[:limit]


def retrieve(
    store: MemoryStore,
    query: str,
    limit: Optional[int] = None,
    scope: Optional[str] = None,
    kind: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    use_vector: bool = True,
) -> List[Dict[str, Any]]:
    """综合检索，返回带 score 的记忆列表。"""
    cfg = store.cfg  # 由 attach_config 设置
    limit = int(limit or cfg.recall_limit)
    cand = int(cfg.recall_candidates)

    # 空查询：退化为"按 scope 列出全部"，保证注入场景（如会话开始）也有内容
    if not (query or "").strip():
        return _list_by_score(store, limit=limit, scope=scope, kind=kind, tags=tags)

    lex = lexical_scores(store.con, query, cand)
    vec = vector_scores(store.con, store.embedder, query, cand, scope) if use_vector else {}

    row_ids = set(lex) | set(vec)
    if not row_ids:
        return []

    placeholders = ",".join("?" for _ in row_ids)
    sql = f"SELECT * FROM memories WHERE rowid IN ({placeholders}) AND archived=0"
    args = list(row_ids)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    rows = store.con.execute(sql, args).fetchall()

    now = dbmod.now()
    results: List[Dict[str, Any]] = []
    for r in rows:
        mem = dbmod.row_to_dict(r)
        if scope and mem["scope"] not in (scope, "global"):
            continue
        if tags:
            have = set(mem.get("tags") or [])
            if not have.intersection({str(t) for t in tags}):
                continue

        rid = str(mem.get("rowid", "")) if "rowid" in mem else None
        # rowid 需要显式取出
        rid = mem.pop("rowid", None)
        s_lex = lex.get(str(rid), 0.0) if rid is not None else 0.0
        s_vec = vec.get(mem["id"], 0.0)

        age_days = max(0.0, (now - float(mem.get("last_used_at") or mem.get("updated_at") or now)) / 86400.0)
        s_rec = math.pow(0.5, age_days / max(1e-6, cfg.recency_halflife_days))
        s_imp = float(mem.get("importance") or 0.0)
        s_use = 1.0 / (1.0 + math.log1p(float(mem.get("use_count") or 0)))
        pinned = 1.25 if mem.get("pinned") else 1.0

        score = (
            cfg.w_lexical * s_lex
            + cfg.w_vector * s_vec
            + cfg.w_importance * s_imp
            + cfg.w_recency * s_rec
            + cfg.w_usage * s_use
        ) * pinned

        mem["score"] = round(score, 6)
        mem["score_parts"] = {
            "lexical": round(s_lex, 4),
            "vector": round(s_vec, 4),
            "recency": round(s_rec, 4),
            "importance": round(s_imp, 4),
        }
        mem["age_days"] = round(age_days, 2)
        results.append(mem)

    results.sort(key=lambda m: m["score"], reverse=True)
    return results[:limit]


def format_context(
    memories: Sequence[Dict[str, Any]],
    max_chars: int = 4000,
    title: str = "长期记忆",
) -> str:
    """把检索结果渲染成可直接注入 system prompt 的文本块。"""
    if not memories:
        return ""
    lines = [f"# {title}"]
    budget = max_chars
    used = len(lines[0])
    for i, m in enumerate(memories, 1):
        kind = m.get("kind", "fact")
        scope = m.get("scope", "global")
        tags = m.get("tags") or []
        tag_s = f" [{','.join(map(str, tags))}]" if tags else ""
        head = f"{i}. ({kind}{('@' + scope) if scope != 'global' else ''}{tag_s}) {m.get('title','') or ''}".rstrip()
        body = (m.get("content") or "").strip()
        block = f"{head}\n   {body}"
        if used + len(block) + 2 > budget:
            break
        lines.append(block)
        used += len(block) + 2
    return "\n".join(lines) + "\n" if len(lines) > 1 else ""
