"""SQLite 存储层：记忆条目 + FTS5 全文索引 + 向量 BLOB + 关联关系。

全部用标准库 sqlite3，无 ORM。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 记忆条目
CREATE TABLE IF NOT EXISTS memories (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL DEFAULT 'fact',   -- fact/pref/decision/note/task/env
    scope        TEXT NOT NULL DEFAULT 'global', -- global / project:<name> / session:<id>
    title        TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL,
    tags         TEXT NOT NULL DEFAULT '[]',
    importance   REAL NOT NULL DEFAULT 0.5,
    confidence   REAL NOT NULL DEFAULT 1.0,
    source       TEXT NOT NULL DEFAULT 'manual', -- manual/auto/llm/hook
    session_id   TEXT,
    tokens       TEXT NOT NULL DEFAULT '',       -- 自定义分词结果，供 FTS 匹配
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    last_used_at REAL NOT NULL,
    use_count    INTEGER NOT NULL DEFAULT 0,
    pinned       INTEGER NOT NULL DEFAULT 0,
    archived     INTEGER NOT NULL DEFAULT 0,
    norm_hash    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_scope   ON memories(scope, archived);
CREATE INDEX IF NOT EXISTS idx_mem_kind    ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_mem_created ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mem_hash    ON memories(norm_hash);

-- 全文索引（外部内容表，随 memories 触发器同步）
-- tokens 列存放我们自己的分词结果（中文按字+二元组），避免 unicode61 把整段中文当成一个 token
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    title, content, tags, tokens,
    content='memories', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, title, content, tags, tokens)
    VALUES (new.rowid, new.title, new.content, new.tags, new.tokens);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, content, tags, tokens)
    VALUES ('delete', old.rowid, old.title, old.content, old.tags, old.tokens);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, content, tags, tokens)
    VALUES ('delete', old.rowid, old.title, old.content, old.tags, old.tokens);
    INSERT INTO memories_fts(rowid, title, content, tags, tokens)
    VALUES (new.rowid, new.title, new.content, new.tags, new.tokens);
END;

-- 向量（本地哈希向量或网关 embedding）
CREATE TABLE IF NOT EXISTS vectors (
    mem_id   TEXT PRIMARY KEY,
    dim      INTEGER NOT NULL,
    vec      BLOB NOT NULL,
    provider TEXT NOT NULL DEFAULT 'hash',
    updated_at REAL NOT NULL,
    FOREIGN KEY(mem_id) REFERENCES memories(id) ON DELETE CASCADE
);

-- 会话（一次 CLI 交互 / 一次 Agent 运行）
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    agent      TEXT NOT NULL DEFAULT 'unknown',
    project    TEXT,
    started_at REAL NOT NULL,
    ended_at   REAL,
    summary    TEXT
);

-- 原始对话片段（可选保留，便于重蒸馏）
CREATE TABLE IF NOT EXISTS episodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ep_sess ON episodes(session_id);
CREATE INDEX IF NOT EXISTS idx_ep_time ON episodes(created_at DESC);

-- 记忆间关联（相似合并/引用）
CREATE TABLE IF NOT EXISTS links (
    src     TEXT NOT NULL,
    dst     TEXT NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'related',
    score   REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (src, dst, kind)
);
"""


def connect(db_path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly and path.exists():
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    else:
        con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        con.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),)
        )
    else:
        cur = int(row["value"] or 0)
        if cur < SCHEMA_VERSION:
            con.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
    con.commit()


def now() -> float:
    return time.time()


def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    if "tags" in d and isinstance(d["tags"], str):
        try:
            d["tags"] = json.loads(d["tags"])
        except Exception:
            d["tags"] = []
    return d


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [row_to_dict(r) for r in rows]
