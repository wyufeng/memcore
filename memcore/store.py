"""记忆仓库：增删改查、去重合并、衰减强度、使用统计。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence

from . import db as dbmod
from . import embed as embedmod

KINDS = ("fact", "pref", "decision", "note", "task", "env")

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[\s，。！？；：、,.!?;:'\"()\[\]{}<>·—\-]+")


def normalize(text: str) -> str:
    """归一化：去标点、压空白、小写。用于近似去重。"""
    t = _PUNCT.sub(" ", (text or "").lower())
    return _WS.sub(" ", t).strip()


def norm_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:24]


def token_set(text: str) -> set:
    return set(embedmod.tokenize(text or ""))


def build_tokens(title: str, content: str, tags: Sequence[str]) -> str:
    """给 FTS 用的分词串：空格分隔的自定义 token（含中文二元组）。"""
    parts = [title or "", content or ""]
    parts.extend(str(t) for t in (tags or []))
    toks = embedmod.tokenize(" ".join(parts))
    # 去重但保持顺序
    seen = set()
    out = []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return " ".join(out)


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


class MemoryStore:
    def __init__(self, con: sqlite3.Connection, embedder=None, cfg=None) -> None:
        self.con = con
        self.embedder = embedder or embedmod.HashEmbedder()
        self.cfg = cfg  # 供 retrieve 读取权重等参数

    # ---------- 写 ----------

    def add(
        self,
        content: str,
        kind: str = "fact",
        scope: str = "global",
        title: str = "",
        tags: Optional[Sequence[str]] = None,
        importance: float = 0.5,
        confidence: float = 1.0,
        source: str = "manual",
        session_id: Optional[str] = None,
        pinned: bool = False,
        dedup_threshold: float = 0.88,
    ) -> Dict[str, Any]:
        """写入一条记忆；若与已有条目高度相似则合并（更新 + 提升重要度）。"""
        content = (content or "").strip()
        if not content:
            raise ValueError("记忆内容不能为空")

        kind = kind if kind in KINDS else "fact"
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        nh = norm_hash(content)
        ts = dbmod.now()

        # 精确重复
        row = self.con.execute(
            "SELECT * FROM memories WHERE norm_hash=? AND archived=0 LIMIT 1", (nh,)
        ).fetchone()
        if row is not None:
            return self._merge_into(row, content, tags, importance, source, session_id)

        # 近似重复：词集 Jaccard
        if dedup_threshold > 0:
            cand_toks = token_set(content)
            for r in self.con.execute(
                "SELECT * FROM memories WHERE archived=0 AND kind=? ORDER BY updated_at DESC LIMIT 300",
                (kind,),
            ):
                if jaccard(cand_toks, token_set(r["content"])) >= dedup_threshold:
                    return self._merge_into(r, content, tags, importance, source, session_id)

        mem_id = uuid.uuid4().hex[:16]
        title_s = title or content[:40]
        tokens = build_tokens(title_s, content, tags)
        self.con.execute(
            "INSERT INTO memories(id,kind,scope,title,content,tags,importance,confidence,"
            "source,session_id,tokens,created_at,updated_at,last_used_at,use_count,pinned,archived,norm_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (
                mem_id, kind, scope, title_s, content,
                json.dumps(tags, ensure_ascii=False), float(importance), float(confidence),
                source, session_id, tokens, ts, ts, ts, 0, 1 if pinned else 0, nh,
            ),
        )
        self._store_vector(mem_id, content, ts)
        self.con.commit()
        return self.get(mem_id)

    def _merge_into(self, row: sqlite3.Row, content: str, tags: Sequence[str],
                    importance: float, source: str, session_id: Optional[str]) -> Dict[str, Any]:
        mem_id = row["id"]
        old_tags = json.loads(row["tags"] or "[]")
        merged = list(dict.fromkeys(list(old_tags) + list(tags)))
        new_importance = min(1.0, max(row["importance"], importance) + 0.05)
        ts = dbmod.now()
        self.con.execute(
            "UPDATE memories SET tags=?, importance=?, updated_at=?, use_count=use_count+1,"
            " last_used_at=?, source=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), new_importance, ts, ts, source, mem_id),
        )
        self.con.commit()
        return self.get(mem_id)

    def _store_vector(self, mem_id: str, text: str, ts: Optional[float] = None) -> None:
        try:
            vec = self.embedder.embed(text)
            self.con.execute(
                "INSERT INTO vectors(mem_id,dim,vec,provider,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(mem_id) DO UPDATE SET dim=excluded.dim, vec=excluded.vec,"
                " provider=excluded.provider, updated_at=excluded.updated_at",
                (mem_id, len(vec), embedmod.pack_vector(vec),
                 getattr(self.embedder, "name", "hash"), ts or dbmod.now()),
            )
        except Exception:
            pass  # 向量失败不影响主流程

    def update(self, mem_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = {"kind", "scope", "title", "content", "tags", "importance",
                   "confidence", "pinned", "archived"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "tags":
                v = json.dumps([str(t) for t in v], ensure_ascii=False)
            if k in ("pinned", "archived"):
                v = 1 if v else 0
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return self.get(mem_id)
        ts = dbmod.now()
        sets.append("updated_at=?")
        vals.append(ts)
        vals.append(mem_id)
        self.con.execute(f"UPDATE memories SET {','.join(sets)} WHERE id=?", vals)
        if "content" in fields and fields["content"]:
            self.con.execute("UPDATE memories SET norm_hash=? WHERE id=?",
                             (norm_hash(fields["content"]), mem_id))
            self._store_vector(mem_id, fields["content"], ts)
        # tokens 依赖 title/content/tags，任一变化都要重建
        cur = self.con.execute("SELECT title,content,tags FROM memories WHERE id=?", (mem_id,)).fetchone()
        if cur is not None:
            try:
                tags_now = json.loads(cur["tags"] or "[]")
            except Exception:
                tags_now = []
            self.con.execute("UPDATE memories SET tokens=? WHERE id=?",
                             (build_tokens(cur["title"], cur["content"], tags_now), mem_id))
        self.con.commit()
        return self.get(mem_id)

    def delete(self, mem_id: str, hard: bool = False) -> bool:
        if hard:
            cur = self.con.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        else:
            cur = self.con.execute(
                "UPDATE memories SET archived=1, updated_at=? WHERE id=?", (dbmod.now(), mem_id))
        self.con.commit()
        return cur.rowcount > 0

    # ---------- 读 ----------

    def get(self, mem_id: str) -> Optional[Dict[str, Any]]:
        row = self.con.execute("SELECT * FROM memories WHERE id=?", (mem_id,)).fetchone()
        return dbmod.row_to_dict(row) if row else None

    def list(
        self,
        scope: Optional[str] = None,
        kind: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM memories WHERE 1=1"
        args: List[Any] = []
        if not include_archived:
            sql += " AND archived=0"
        if scope:
            sql += " AND (scope=? OR scope='global')"
            args.append(scope)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY pinned DESC, importance DESC, updated_at DESC LIMIT ? OFFSET ?"
        args.extend([int(limit), int(offset)])
        rows = self.con.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dbmod.row_to_dict(r)
            if tags:
                have = set(d.get("tags") or [])
                if not have.intersection({str(t) for t in tags}):
                    continue
            out.append(d)
        return out

    def count(self, include_archived: bool = False) -> int:
        sql = "SELECT COUNT(*) c FROM memories"
        if not include_archived:
            sql += " WHERE archived=0"
        return int(self.con.execute(sql).fetchone()["c"])

    # ---------- 生命周期 ----------

    def strength(self, mem: Dict[str, Any], halflife_days: float) -> float:
        """记忆强度：随时间衰减，被使用/pinned 后回升。"""
        now = dbmod.now()
        age_days = max(0.0, (now - float(mem.get("last_used_at") or mem.get("updated_at") or now)) / 86400.0)
        decay = math.pow(0.5, age_days / max(1e-6, halflife_days))
        usage = 1.0 + math.log1p(float(mem.get("use_count") or 0)) * 0.25
        pinned = 1.6 if mem.get("pinned") else 1.0
        return min(2.0, decay * usage * pinned)

    def decay_floor_sweep(self, halflife_days: float, floor: float) -> int:
        """把长期不用且强度低于下限的记忆归档。"""
        rows = self.con.execute(
            "SELECT * FROM memories WHERE archived=0 AND pinned=0"
        ).fetchall()
        archived = 0
        for r in rows:
            d = dbmod.row_to_dict(r)
            if self.strength(d, halflife_days) < floor:
                self.con.execute(
                    "UPDATE memories SET archived=1, updated_at=? WHERE id=?",
                    (dbmod.now(), d["id"]))
                archived += 1
        self.con.commit()
        return archived

    def mark_used(self, mem_ids: Sequence[str]) -> None:
        if not mem_ids:
            return
        ts = dbmod.now()
        self.con.executemany(
            "UPDATE memories SET use_count=use_count+1, last_used_at=? WHERE id=?",
            [(ts, mid) for mid in mem_ids],
        )
        self.con.commit()

    # ---------- 会话 / 片段 ----------

    def start_session(self, agent: str = "unknown", project: Optional[str] = None,
                      session_id: Optional[str] = None) -> str:
        sid = session_id or uuid.uuid4().hex[:16]
        self.con.execute(
            "INSERT OR REPLACE INTO sessions(id,agent,project,started_at) VALUES(?,?,?,?)",
            (sid, agent, project, dbmod.now()),
        )
        self.con.commit()
        return sid

    def end_session(self, session_id: str, summary: Optional[str] = None) -> None:
        self.con.execute(
            "UPDATE sessions SET ended_at=?, summary=? WHERE id=?",
            (dbmod.now(), summary, session_id),
        )
        self.con.commit()

    def add_episode(self, role: str, text: str, session_id: Optional[str] = None) -> None:
        if not (text or "").strip():
            return
        self.con.execute(
            "INSERT INTO episodes(session_id,role,text,created_at) VALUES(?,?,?,?)",
            (session_id, role, text[:8000], dbmod.now()),
        )
        self.con.commit()

    def recent_episodes(self, limit: int = 200, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if session_id:
            rows = self.con.execute(
                "SELECT * FROM episodes WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit)).fetchall()
        else:
            rows = self.con.execute(
                "SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return dbmod.rows_to_dicts(rows)

    def stats(self) -> Dict[str, Any]:
        by_kind = {r["kind"]: r["c"] for r in self.con.execute(
            "SELECT kind, COUNT(*) c FROM memories WHERE archived=0 GROUP BY kind")}
        by_scope = {r["scope"]: r["c"] for r in self.con.execute(
            "SELECT scope, COUNT(*) c FROM memories WHERE archived=0 GROUP BY scope")}
        archived = int(self.con.execute(
            "SELECT COUNT(*) c FROM memories WHERE archived=1").fetchone()["c"])
        sessions = int(self.con.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"])
        episodes = int(self.con.execute("SELECT COUNT(*) c FROM episodes").fetchone()["c"])
        vectors = int(self.con.execute("SELECT COUNT(*) c FROM vectors").fetchone()["c"])
        oldest = self.con.execute(
            "SELECT MIN(created_at) t FROM memories WHERE archived=0").fetchone()["t"]
        newest = self.con.execute(
            "SELECT MAX(updated_at) t FROM memories WHERE archived=0").fetchone()["t"]
        return {
            "active": self.count(),
            "archived": archived,
            "by_kind": by_kind,
            "by_scope": by_scope,
            "sessions": sessions,
            "episodes": episodes,
            "vectors": vectors,
            "oldest": oldest,
            "newest": newest,
        }
