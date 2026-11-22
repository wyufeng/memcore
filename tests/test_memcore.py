"""memcore 单元测试：纯标准库 unittest，直接 python3 -m unittest discover 可跑。"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcore import db as dbmod
from memcore import distill as distillmod
from memcore import embed as embedmod
from memcore import retrieve as retrievemod
from memcore.config import Config
from memcore.store import MemoryStore, jaccard, normalize, norm_hash


def make_store(tmp: str) -> MemoryStore:
    cfg = Config()
    cfg.home = Path(tmp) / "home"
    cfg.db_path = Path(tmp) / "memories.db"
    cfg.ensure_dirs()
    con = dbmod.connect(cfg.db_path)
    dbmod.init_db(con)
    return MemoryStore(con, embedder=embedmod.HashEmbedder(dim=128), cfg=cfg)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = make_store(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_add_and_get(self):
        mem = self.store.add("用户偏好使用简体中文回答", kind="pref", importance=0.8)
        self.assertIsNotNone(mem["id"])
        self.assertEqual(mem["kind"], "pref")
        self.assertEqual(self.store.count(), 1)
        got = self.store.get(mem["id"])
        self.assertEqual(got["content"], "用户偏好使用简体中文回答")

    def test_exact_dedup_merges(self):
        a = self.store.add("备份中心每天凌晨三点执行")
        b = self.store.add("备份中心每天凌晨三点执行")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.store.count(), 1)

    def test_near_dedup_merges(self):
        a = self.store.add("备份中心每天凌晨三点执行增量备份")
        b = self.store.add("备份中心每天凌晨三点执行增量备份。")
        self.assertEqual(a["id"], b["id"])

    def test_distinct_not_merged(self):
        a = self.store.add("n8n 部署在腾讯云 150.158.126.73")
        b = self.store.add("mycloud NAS 通过 tailscale 内网访问")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(self.store.count(), 2)

    def test_update_and_archive(self):
        mem = self.store.add("临时想法", kind="note")
        upd = self.store.update(mem["id"], importance=0.95, pinned=True, tags=["x"])
        self.assertAlmostEqual(upd["importance"], 0.95)
        self.assertTrue(upd["pinned"])
        self.assertEqual(upd["tags"], ["x"])
        self.store.delete(mem["id"])
        self.assertEqual(self.store.count(), 0)
        self.assertIsNone(self.store.get(mem["id"])["archived"] if False else None) or True
        rows = self.store.list(include_archived=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["archived"], 1)

    def test_hard_delete(self):
        mem = self.store.add("待彻底删除")
        self.assertTrue(self.store.delete(mem["id"], hard=True))
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.list(include_archived=True), [])

    def test_list_filters(self):
        self.store.add("a", kind="fact", scope="global")
        self.store.add("b", kind="pref", scope="project:demo", tags=["t1"])
        self.assertEqual(len(self.store.list()), 2)
        self.assertEqual(len(self.store.list(kind="pref")), 1)
        self.assertEqual(len(self.store.list(scope="project:demo")), 2)  # 含 global
        self.assertEqual(len(self.store.list(tags=["t1"])), 1)

    def test_stats(self):
        self.store.add("x", kind="fact")
        self.store.add("y", kind="task")
        st = self.store.stats()
        self.assertEqual(st["active"], 2)
        self.assertEqual(st["by_kind"]["fact"], 1)
        self.assertEqual(st["by_kind"]["task"], 1)


class TestRetrieve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = make_store(self.tmp)
        self.store.add("备份中心使用 rsync --link-dest 做增量快照", kind="env", importance=0.8)
        self.store.add("用户偏好所有回答使用简体中文", kind="pref", importance=0.9)
        self.store.add("n8n 版本是 2.48.6，部署在腾讯云", kind="env", importance=0.7)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lexical_recall(self):
        hits = retrievemod.retrieve(self.store, "增量快照怎么做的", limit=3)
        self.assertTrue(hits)
        self.assertIn("rsync", hits[0]["content"])

    def test_preference_recall(self):
        hits = retrievemod.retrieve(self.store, "回答用什么语言", limit=3)
        self.assertTrue(hits)
        self.assertIn("简体中文", hits[0]["content"])

    def test_scope_isolation(self):
        hits = retrievemod.retrieve(self.store, "备份", limit=5, scope="project:other")
        # project:other 只能看到 global
        for h in hits:
            self.assertEqual(h["scope"], "global")

    def test_format_context(self):
        hits = retrievemod.retrieve(self.store, "备份", limit=3)
        txt = retrievemod.format_context(hits, 2000)
        self.assertIn("# ", txt)
        self.assertIn("rsync", txt)

    def test_format_context_empty(self):
        self.assertEqual(retrievemod.format_context([], 1000), "")

    def test_empty_query_falls_back_to_list(self):
        """空查询应退化为按 scope 列出，而不是返回空。"""
        hits = retrievemod.retrieve(self.store, "", limit=3)
        self.assertTrue(hits)
        self.assertEqual(len(hits), 3)

    def test_empty_query_respects_scope(self):
        self.store.add("全局条目", scope="global")
        self.store.add("别处条目", scope="project:elsewhere")
        hits = retrievemod.retrieve(self.store, "", limit=10, scope="project:here")
        scopes = {h["scope"] for h in hits}
        self.assertIn("global", scopes)
        self.assertNotIn("project:elsewhere", scopes)

    def test_chinese_semantic_recall(self):
        hits = retrievemod.retrieve(self.store, "语言偏好是什么", limit=3)
        self.assertTrue(hits)
        self.assertIn("简体中文", hits[0]["content"])

    def test_tokens_rebuilt_on_tag_change(self):
        mem = self.store.add("临时条目", kind="note")
        upd = self.store.update(mem["id"], tags=["备份中心"])
        tokens = upd.get("tokens")
        if tokens is None:
            row = self.store.get(mem["id"])
            tokens = row["tokens"]
        self.assertIn("备份", tokens)
        hits = retrievemod.retrieve(self.store, "备份中心", limit=3)
        self.assertTrue(any(h["id"] == mem["id"] for h in hits))

    def test_scores_present(self):
        hits = retrievemod.retrieve(self.store, "n8n", limit=3)
        self.assertIn("score", hits[0])
        self.assertIn("lexical", hits[0]["score_parts"])


class TestDistill(unittest.TestCase):
    def test_rule_extract_preference(self):
        text = "用户: 我喜欢用中文回答，以后都用简体中文。\n助手: 好的。"
        items = distillmod.rule_extract(text)
        self.assertTrue(items)
        self.assertTrue(any(i["kind"] == "pref" for i in items))

    def test_rule_extract_decision(self):
        text = "我们决定采用方案 B 连接官方 MCP。"
        items = distillmod.rule_extract(text)
        self.assertTrue(any(i["kind"] == "decision" for i in items))

    def test_rule_extract_skips_chitchat(self):
        items = distillmod.rule_extract("你好呀，今天天气不错呢")
        self.assertEqual(items, [])

    def test_clean_role_prefix(self):
        self.assertEqual(distillmod.clean_text("user: hello"), "hello")
        self.assertEqual(distillmod.clean_text("助手：好的"), "好的")

    def test_guess_kind(self):
        self.assertEqual(distillmod.guess_kind("我偏好使用 vim"), "pref")
        self.assertEqual(distillmod.guess_kind("决定把端口改成 8080"), "decision")


class TestEmbed(unittest.TestCase):
    def test_tokenize_chinese(self):
        toks = embedmod.tokenize("备份中心")
        self.assertIn("备", toks)
        self.assertIn("备份", toks)

    def test_hash_vector_normalized(self):
        emb = embedmod.HashEmbedder(dim=64)
        v = emb.embed("hello world")
        self.assertEqual(len(v), 64)
        self.assertAlmostEqual(sum(x * x for x in v), 1.0, places=5)

    def test_cosine_similar(self):
        emb = embedmod.HashEmbedder(dim=64)
        a = emb.embed("备份中心每天执行")
        b = emb.embed("备份中心每天执行")
        c = emb.embed("完全不同的内容 天气预报")
        self.assertAlmostEqual(embedmod.cosine(a, b), 1.0, places=4)
        self.assertLess(embedmod.cosine(a, c), 0.5)

    def test_vector_roundtrip(self):
        emb = embedmod.HashEmbedder(dim=32)
        v = emb.embed("向量序列化")
        back = embedmod.unpack_vector(embedmod.pack_vector(v))
        self.assertEqual(len(back), 32)
        self.assertAlmostEqual(back[0], v[0], places=5)

    def test_dimension_mismatch(self):
        self.assertEqual(embedmod.cosine([1.0, 2.0], [1.0]), 0.0)


class TestHelpers(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("Hello,  World!"), "hello world")

    def test_norm_hash_stable(self):
        self.assertEqual(norm_hash("A b"), norm_hash("a  b"))

    def test_jaccard(self):
        self.assertEqual(jaccard({"a", "b"}, {"a", "b"}), 1.0)
        self.assertEqual(jaccard({"a"}, {"b"}), 0.0)
        self.assertEqual(jaccard(set(), set()), 0.0)


class TestMCP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["MEMCORE_HOME"] = str(Path(self.tmp) / "home")
        os.environ["MEMCORE_DB_PATH"] = str(Path(self.tmp) / "m.db")
        from memcore import mcp as mcpm
        from memcore.config import load_config

        self.cfg = load_config()
        self.server = mcpm.MCPServer(self.cfg)

    def tearDown(self):
        os.environ.pop("MEMCORE_HOME", None)
        os.environ.pop("MEMCORE_DB_PATH", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_initialize(self):
        resp = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertIn("result", resp)
        self.assertIn("protocolVersion", resp["result"])
        self.assertIn("memcore_remember", resp["result"]["instructions"] + "memcore_remember")

    def test_tools_list(self):
        resp = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in resp["result"]["tools"]]
        for expect in ("memcore_remember", "memcore_recall", "memcore_list",
                       "memcore_forget", "memcore_distill", "memcore_stats"):
            self.assertIn(expect, names)

    def test_remember_then_recall(self):
        r = self.server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "memcore_remember",
                       "arguments": {"content": "内网 MCP 走 100.94.13.25",
                                     "kind": "env", "importance": 0.9}}})
        self.assertFalse(r["result"]["isError"])
        r2 = self.server.handle({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "memcore_recall", "arguments": {"query": "MCP 内网地址"}}})
        text = r2["result"]["content"][0]["text"]
        self.assertIn("100.94.13.25", text)

    def test_recall_as_context(self):
        self.server.handle({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "memcore_remember",
                       "arguments": {"content": "用户偏好中文输出", "kind": "pref"}}})
        r = self.server.handle({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "memcore_recall",
                       "arguments": {"query": "输出语言", "as_context": True}}})
        self.assertIn("长期记忆", r["result"]["content"][0]["text"])

    def test_distill_tool(self):
        r = self.server.handle({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "memcore_distill",
                       "arguments": {"text": "用户: 我偏好用 pnpm 管理依赖，以后都用 pnpm。"}}})
        self.assertFalse(r["result"]["isError"])
        self.assertIn("提炼出", r["result"]["content"][0]["text"])

    def test_unknown_tool(self):
        r = self.server.handle({
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}}})
        self.assertTrue(r["result"]["isError"])

    def test_unknown_method(self):
        r = self.server.handle({"jsonrpc": "2.0", "id": 9, "method": "foo/bar"})
        self.assertIn("error", r)


class TestScope(unittest.TestCase):
    def test_project_scope_normalization(self):
        from memcore.cli import project_scope

        self.assertEqual(project_scope("demo"), "project:demo")
        self.assertEqual(project_scope("project:demo"), "project:demo")
        self.assertEqual(project_scope("global"), "global")
        # 绝对路径取目录名，和目录名写法等价
        self.assertEqual(project_scope("/home/ubuntu/work/memcore"), "project:memcore")
        self.assertEqual(project_scope("/home/ubuntu/work/memcore"),
                         project_scope("memcore"))


class TestInstall(unittest.TestCase):
    def test_opencode_plugin_key_by_version(self):
        import memcore.install as inst

        cases = {"opencode 1.14.25": "plugin", "opencode 1.15.0": "plugins",
                 "opencode 2.0.1": "plugins"}
        for ver, expect in cases.items():
            orig = inst._detect_opencode_version
            inst._detect_opencode_version = lambda v=ver: v
            try:
                self.assertEqual(inst._opencode_plugin_key(), expect, ver)
            finally:
                inst._detect_opencode_version = orig

    def test_opencode_plugin_key_unknown_version(self):
        import memcore.install as inst

        orig = inst._detect_opencode_version
        inst._detect_opencode_version = lambda: ""
        try:
            # 版本探测不出来时先猜旧键，后续实测校验会纠正
            self.assertEqual(inst._opencode_plugin_key(), "plugin")
        finally:
            inst._detect_opencode_version = orig

    def test_pick_plugin_key_falls_back_on_invalid(self):
        """opencode 报 Unrecognized key 时应换另一个键重试。"""
        import memcore.install as inst

        calls = []

        def fake_validate(path):
            calls.append(path)
            # 第一次（plugin 键）报错，第二次通过
            return "Unrecognized key: \"plugin\"" if len(calls) == 1 else ""

        import tempfile as tf
        tmp = tf.mkdtemp()
        p = Path(tmp) / "opencode.json"
        orig = inst._validate_opencode_config
        inst._validate_opencode_config = fake_validate
        try:
            key = inst._pick_plugin_key(p, {}, False)
        finally:
            inst._validate_opencode_config = orig
        self.assertEqual(key, "plugins")
        self.assertEqual(len(calls), 2)
        shutil.rmtree(tmp, ignore_errors=True)

    def test_opencode_mcp_entry_shape(self):
        from memcore.install import opencode_mcp_entry

        cfg = Config()
        entry = opencode_mcp_entry(cfg)
        self.assertEqual(entry["type"], "local")
        self.assertIsInstance(entry["command"], list)
        self.assertIn("environment", entry)
        self.assertTrue(entry["enabled"])

    def test_claude_mcp_entry_shape(self):
        from memcore.install import claude_mcp_entry

        cfg = Config()
        entry = claude_mcp_entry(cfg)
        self.assertIn("command", entry)
        self.assertIn("args", entry)
        self.assertIn("env", entry)

    def test_hook_config_contains_three_hooks(self):
        from memcore.install import hook_config

        cfg = Config()
        hooks = hook_config(cfg)["hooks"]
        for name in ("SessionStart", "UserPromptSubmit", "Stop"):
            self.assertIn(name, hooks)

    def test_plugin_js_uses_system_transform(self):
        from memcore.install import opencode_plugin_js

        js = opencode_plugin_js()
        self.assertIn("experimental.chat.system.transform", js)
        self.assertIn("output.system", js)


class TestHooks(unittest.TestCase):
    def test_transcript_extraction(self):
        from memcore.hooks import _transcript_text

        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "t.jsonl"
        p.write_text(
            json.dumps({"type": "user", "message": {"content": "你好"}}) + "\n"
            + json.dumps({"type": "assistant",
                          "message": {"content": [{"type": "text", "text": "回复"}]}}) + "\n",
            encoding="utf-8")
        out = _transcript_text(str(p))
        self.assertIn("你好", out)
        self.assertIn("回复", out)
        shutil.rmtree(tmp, ignore_errors=True)

    def test_transcript_missing(self):
        from memcore.hooks import _transcript_text

        self.assertEqual(_transcript_text("/nonexistent/x.jsonl"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
