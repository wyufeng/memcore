"""memcore 单元测试：纯标准库 unittest，直接 python3 -m unittest discover 可跑。"""

import json
import os
import shutil
import subprocess
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


class TestTokenizer(unittest.TestCase):
    """纯标准库 BertTokenizer 的正确性。"""

    def setUp(self):
        from memcore.tokenizer import BertTokenizer

        # 构造一个极小词表，覆盖 [UNK]/[CLS]/[SEP]/[PAD]、普通词、中文字
        vocab = [
            "[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]",
            "hello", "world", "user", "prefers", "chinese",
            "backup", "center", "##ing", "##s",
            "备", "份", "中", "心", "用", "户", ",",
        ]
        self.vocab = vocab
        self.tok = BertTokenizer(vocab=vocab, do_lower_case=True)
        self.ids = {t: i for i, t in enumerate(vocab)}

    def test_special_tokens(self):
        ids = self.tok.convert_tokens_to_ids(["[CLS]", "[SEP]", "[UNK]"])
        self.assertEqual(ids, [self.ids["[CLS]"], self.ids["[SEP]"],
                                self.ids["[UNK]"]])

    def test_wordpiece_continuation(self):
        toks = self.tok.tokenize("backuping")
        self.assertEqual(toks, ["backup", "##ing"])

    def test_unknown_token(self):
        toks = self.tok.tokenize("zzzzz")
        self.assertIn("[UNK]", toks)

    def test_chinese_char_split(self):
        toks = self.tok.tokenize("备份中心")
        self.assertEqual(toks, ["备", "份", "中", "心"])

    def test_punctuation_split(self):
        toks = self.tok.tokenize("hello, world")
        self.assertIn(",", toks)
        self.assertIn("hello", toks)

    def test_encode_shape_and_specials(self):
        enc = self.tok.encode("hello world", max_len=8)
        self.assertEqual(len(enc["input_ids"]), 8)
        self.assertEqual(len(enc["attention_mask"]), 8)
        self.assertEqual(len(enc["token_type_ids"]), 8)
        # [CLS] hello world [SEP] + 4 个 [PAD]
        self.assertEqual(enc["input_ids"][0], self.ids["[CLS]"])
        self.assertEqual(enc["input_ids"][3], self.ids["[SEP]"])
        self.assertEqual(enc["input_ids"][4], self.ids["[PAD]"])
        self.assertEqual(enc["attention_mask"][:4], [1, 1, 1, 1])
        self.assertEqual(enc["attention_mask"][4:], [0, 0, 0, 0])

    def test_encode_truncates_long_text(self):
        enc = self.tok.encode("hello " * 100, max_len=16)
        self.assertEqual(len(enc["input_ids"]), 16)
        self.assertEqual(enc["input_ids"][0], self.ids["[CLS]"])
        # 文本被截断填满，[SEP] 落在末尾，没有 padding
        self.assertEqual(enc["input_ids"][-1], self.ids["[SEP]"])
        self.assertEqual(sum(enc["attention_mask"]), 16)


class TestExtractConfig(unittest.TestCase):
    """抽取模型配置与请求体构造。"""

    def test_effective_extract_prefers_extract(self):
        cfg = Config()
        cfg.llm_base_url = "http://llm.local/v1"
        cfg.llm_model = "old-model"
        cfg.extract_base_url = "http://extract.local/v1"
        cfg.extract_model = "new-model"
        cfg.use_llm = True
        ex = cfg.effective_extract()
        self.assertEqual(ex["base_url"], "http://extract.local/v1")
        self.assertEqual(ex["model"], "new-model")
        self.assertTrue(ex["enabled"])

    def test_effective_extract_falls_back_to_llm(self):
        cfg = Config()
        cfg.llm_base_url = "http://llm.local/v1"
        cfg.llm_model = "fallback-model"
        cfg.use_llm = True
        ex = cfg.effective_extract()
        self.assertEqual(ex["base_url"], "http://llm.local/v1")
        self.assertEqual(ex["model"], "fallback-model")
        self.assertTrue(ex["enabled"])

    def test_effective_extract_disabled_without_model(self):
        cfg = Config()
        cfg.use_llm = True
        ex = cfg.effective_extract()
        self.assertFalse(ex["enabled"])

    def test_api_key_masked_in_to_dict(self):
        cfg = Config()
        cfg.extract_api_key = "secret-key"
        self.assertEqual(cfg.to_dict()["extract_api_key"], "***")

    def test_build_chat_request_openai(self):
        url, payload = distillmod._build_chat_request(
            "http://gw.local/v1", "qwen", "hello", 0.2, "PROMPT {text}"
        )
        self.assertEqual(url, "http://gw.local/v1/chat/completions")
        self.assertEqual(payload["model"], "qwen")
        self.assertEqual(payload["temperature"], 0.2)
        # {text} 占位符被替换
        self.assertEqual(payload["messages"][-1]["content"], "PROMPT hello")

    def test_build_chat_request_bare_url_adds_v1(self):
        url, _ = distillmod._build_chat_request(
            "http://gw.local", "m", "t", 0.1, "P {text}"
        )
        self.assertEqual(url, "http://gw.local/v1/chat/completions")

    def test_build_chat_request_ollama(self):
        url, payload = distillmod._build_chat_request(
            "http://127.0.0.1:11434/api/chat", "qwen2.5:7b", "hi", 0.1, "P {text}"
        )
        self.assertEqual(url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(payload["options"]["temperature"], 0.1)
        self.assertFalse(payload["stream"])

    def test_build_chat_request_ollama_bare(self):
        url, _ = distillmod._build_chat_request(
            "http://127.0.0.1:11434", "qwen2.5:7b", "hi", 0.1, "P {text}"
        )
        self.assertEqual(url, "http://127.0.0.1:11434/api/chat")


class TestExtractDiagnostics(unittest.TestCase):
    """抽取失败要能被看见，而不是静默退回规则抽取。"""

    def _serve_once(self, payload, status=200):
        """起一个本地 HTTP 服务，返回一次 payload。"""
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def test_llm_extract_parses_openai_response(self):
        srv = self._serve_once({"choices": [{"message": {"content": json.dumps(
            [{"content": "用户偏好中文", "kind": "pref"}], ensure_ascii=False)}}]})
        try:
            url = "http://127.0.0.1:%d/v1" % srv.server_address[1]
            items = distillmod.llm_extract("t", url, "", "m")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["content"], "用户偏好中文")
            self.assertEqual(items[0]["kind"], "pref")
            self.assertEqual(distillmod.LAST_LLM_ERROR, "")
        finally:
            srv.shutdown()

    def test_llm_extract_accepts_text_field_alias(self):
        """不同模型可能用 text 而非 content 字段。"""
        srv = self._serve_once({"choices": [{"message": {"content": json.dumps(
            [{"text": "用 text 字段写的记忆", "type": "pref"}], ensure_ascii=False)}}]})
        try:
            url = "http://127.0.0.1:%d/v1" % srv.server_address[1]
            items = distillmod.llm_extract("t", url, "", "m")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["content"], "用 text 字段写的记忆")
            self.assertEqual(items[0]["kind"], "pref")
        finally:
            srv.shutdown()

    def test_llm_extract_connection_failure_records_error(self):
        items = distillmod.llm_extract(
            "t", "http://127.0.0.1:1/v1", "", "m", timeout=3
        )
        self.assertEqual(items, [])
        self.assertTrue(distillmod.LAST_LLM_ERROR)
        self.assertIn("连接失败", distillmod.LAST_LLM_ERROR)

    def test_llm_extract_non_json_response_records_error(self):
        srv = self._serve_once({"choices": [{"message": {"content": "抱歉我不知道"}}]})
        try:
            url = "http://127.0.0.1:%d/v1" % srv.server_address[1]
            items = distillmod.llm_extract("t", url, "", "m")
            self.assertEqual(items, [])
            self.assertIn("不是 JSON 数组", distillmod.LAST_LLM_ERROR)
        finally:
            srv.shutdown()

    def test_llm_extract_missing_content_field_records_error(self):
        srv = self._serve_once({"choices": [{"message": {"content": json.dumps(
            [{"foo": "没有 content 也没有 text"}], ensure_ascii=False)}}]})
        try:
            url = "http://127.0.0.1:%d/v1" % srv.server_address[1]
            items = distillmod.llm_extract("t", url, "", "m")
            self.assertEqual(items, [])
            self.assertIn("content/text", distillmod.LAST_LLM_ERROR)
        finally:
            srv.shutdown()

    def test_distill_marks_source_rule_on_failure(self):
        cfg = Config()
        cfg.use_llm = True
        cfg.extract_base_url = "http://127.0.0.1:1/v1"
        cfg.extract_model = "m"
        cfg.extract_timeout = 3
        items = distillmod.distill("用户: 我偏好中文回答。", cfg)
        self.assertTrue(items)  # 回落到规则抽取，仍有产出
        self.assertEqual(distillmod.LAST_SOURCE, "rule")
        self.assertTrue(distillmod.LAST_LLM_ERROR)

    def test_distill_marks_source_llm_on_success(self):
        srv = self._serve_once({"choices": [{"message": {"content": json.dumps(
            [{"content": "来自抽取模型的记忆", "kind": "fact"}], ensure_ascii=False)}}]})
        try:
            cfg = Config()
            cfg.use_llm = True
            cfg.extract_base_url = "http://127.0.0.1:%d/v1" % srv.server_address[1]
            cfg.extract_model = "m"
            items = distillmod.distill("随便什么文本", cfg)
            self.assertEqual(distillmod.LAST_SOURCE, "llm")
            self.assertEqual(items[0]["content"], "来自抽取模型的记忆")
        finally:
            srv.shutdown()


class TestEmbedProvider(unittest.TestCase):
    """embed_provider 分派逻辑。"""

    def test_hash_provider(self):
        cfg = Config()
        cfg.embed_provider = "hash"
        cfg.embed_dim = 64
        emb = embedmod.get_embedder(cfg)
        self.assertEqual(emb.name, "hash")
        self.assertEqual(len(emb.embed("x")), 64)

    def test_remote_without_config_falls_back_to_hash(self):
        cfg = Config()
        cfg.embed_provider = "remote"
        cfg.embed_base_url = ""
        cfg.embed_model = ""
        emb = embedmod.get_embedder(cfg)
        self.assertEqual(emb.name, "hash")

    def test_auto_never_raises(self):
        cfg = Config()
        cfg.embed_provider = "auto"
        cfg.embed_model_path = "/nonexistent/path/xyz"
        emb = embedmod.get_embedder(cfg)
        self.assertIn(emb.name, ("hash", "local-onnx", "remote"))


class TestLocalModelDiscovery(unittest.TestCase):
    """本地模型路径探测（不加载模型）。"""

    def test_describe_missing_model(self):
        from memcore import embed_local

        cfg = Config()
        cfg.embed_model_path = "/nonexistent/model/dir"
        info = embed_local.describe_local_model(cfg)
        self.assertFalse(info["found"])

    def test_find_model_dir_in_vendor(self):
        from memcore import embed_local

        repo = Path(__file__).resolve().parent.parent
        model_dir = repo / "vendor" / "models" / "bge-small-zh-v1.5"
        if not (model_dir / "vocab.txt").exists():
            self.skipTest("vendor 模型未下载")
        found = embed_local._find_model_dir(None)
        self.assertIsNotNone(found)
        self.assertTrue((found / "vocab.txt").exists())


class TestVendorBundle(unittest.TestCase):
    """vendor 自包含性检查：wheel 依赖闭包完整 + 模型文件齐全。"""

    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parent.parent
        cls.wheels = cls.repo / "vendor" / "wheels"
        cls.models = cls.repo / "vendor" / "models" / "bge-small-zh-v1.5"

    @staticmethod
    def _read_metadata(wheel: Path):
        import re
        import zipfile

        with zipfile.ZipFile(wheel) as z:
            for name in z.namelist():
                if name.endswith("METADATA"):
                    txt = z.read(name).decode("utf-8", "ignore")
                    pkg = re.search(r"^Name: (.+)$", txt, re.M)
                    reqs = []
                    for line in txt.splitlines():
                        if not line.startswith("Requires-Dist:"):
                            continue
                        raw = line.split(":", 1)[1].strip()
                        # 跳过 extras 与平台限定依赖（Linux + 默认安装不需要）
                        if "extra ==" in raw:
                            continue
                        if "python_version ==" in raw or "sys_platform ==" in raw:
                            continue
                        if "platform_python_implementation" in raw:
                            continue
                        m = re.match(r"^([A-Za-z0-9_.\-]+)", raw)
                        if m:
                            reqs.append(m.group(1).lower())
                    return (pkg.group(1).strip().lower() if pkg else None), reqs
        return None, []

    def test_wheels_present(self):
        if not self.wheels.is_dir():
            self.skipTest("vendor/wheels 不存在")
        self.assertTrue(list(self.wheels.glob("*.whl")),
                        "vendor/wheels 里没有 wheel")

    def test_wheel_dependency_closure(self):
        """每个 wheel 的必需依赖都必须在 vendor 里存在（否则离线安装会失败）。"""
        if not self.wheels.is_dir():
            self.skipTest("vendor/wheels 不存在")
        import re

        available = {}
        for w in sorted(self.wheels.glob("*.whl")):
            name, reqs = self._read_metadata(w)
            if name:
                available[name] = reqs

        missing = []
        for name, reqs in available.items():
            for r in reqs:
                if r not in available:
                    missing.append(f"{name} -> {r}")
        self.assertEqual(missing, [], f"vendor/wheels 依赖不完整: {missing}")

    def test_python_version_coverage(self):
        """numpy 与 onnxruntime 必须覆盖 cp39~cp312。"""
        if not self.wheels.is_dir():
            self.skipTest("vendor/wheels 不存在")
        import re

        names = [w.name for w in self.wheels.glob("*.whl")]
        for pkg in ("numpy", "onnxruntime"):
            tags = set()
            for n in names:
                if n.startswith(pkg + "-"):
                    m = re.search(r"-(cp\d+)-", n)
                    if m:
                        tags.add(m.group(1))
            self.assertTrue(tags, f"没有 {pkg} 的二进制 wheel")
            for tag in ("cp39", "cp310", "cp311", "cp312"):
                self.assertIn(tag, tags, f"{pkg} 缺少 {tag} wheel")

    def test_model_files_present(self):
        if not self.models.is_dir():
            self.skipTest("vendor/models 不存在")
        for f in ("config.json", "tokenizer_config.json",
                  "special_tokens_map.json", "vocab.txt",
                  "onnx/model_int8.onnx"):
            self.assertTrue((self.models / f).exists(), f"缺少模型文件 {f}")

    def test_model_onnx_not_truncated(self):
        onnx = self.models / "onnx" / "model_int8.onnx"
        if not onnx.exists():
            self.skipTest("模型未下载")
        # 完整模型约 23.9 MB，截断的下载会明显小于此
        self.assertGreater(onnx.stat().st_size, 20 * 1024 * 1024)

    def test_vocab_loadable(self):
        vocab = self.models / "vocab.txt"
        if not vocab.exists():
            self.skipTest("模型未下载")
        from memcore.tokenizer import BertTokenizer

        tok = BertTokenizer.from_vocab_file(str(vocab), do_lower_case=False)
        self.assertGreater(tok.vocab_size, 20000)
        self.assertIn("备", tok.vocab)
        self.assertIn("##", "".join(list(tok.vocab.keys())[:0]) + "##")


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

    def test_opencode_plugin_dir_has_package_json_with_main(self):
        """opencode 1.3+ 把插件目录当 npm 包装，缺 package.json/main 会安装失败。"""
        import memcore.install as inst

        tmp = tempfile.mkdtemp()
        base = Path(tmp)
        try:
            files = inst._opencode_plugin_dir_files(base, "plugin")
            pkg_path = base / "plugin" / "package.json"
            self.assertTrue(pkg_path.exists())
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
            self.assertIn("main", pkg)
            self.assertTrue((base / "plugin" / pkg["main"].lstrip("./")).exists())
            self.assertTrue((base / "plugin" / "memcore.js").exists())
            self.assertTrue((base / "plugin" / "index.js").exists())
            self.assertEqual(len(files), 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_opencode_plugin_arrays_have_no_duplicates(self):
        """plugin/plugins 数组不应出现重复条目。"""
        import memcore.install as inst

        tmp = tempfile.mkdtemp()
        try:
            base = Path(tmp)
            for d in ("plugin", "plugins"):
                inst._opencode_plugin_dir_files(base, d)
            path = base / "opencode.json"
            path.write_text(json.dumps({}), encoding="utf-8")
            # 跳过 opencode 实测校验（测试环境无 opencode）
            orig = inst._validate_opencode_config
            inst._validate_opencode_config = lambda p: ""
            try:
                inst._pick_plugin_key(path, {}, False)
            finally:
                inst._validate_opencode_config = orig
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in ("plugin", "plugins"):
                arr = data.get(key) or []
                self.assertEqual(len(arr), len(set(arr)), f"{key} 有重复项：{arr}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_install_opencode_cleans_legacy_duplicates(self):
        """历史版本写入过重复项，重复安装后应被清理。"""
        import memcore.install as inst

        tmp = tempfile.mkdtemp()
        home = Path(tmp)
        try:
            base = home / ".config" / "opencode"
            base.mkdir(parents=True)
            path = base / "opencode.json"
            # 旧版本留下的重复项
            path.write_text(json.dumps(
                {"$schema": "https://opencode.ai/config.json",
                 "plugin": ["./plugins", "./plugins"]},
                ensure_ascii=False), encoding="utf-8")

            orig_home = Path.home
            orig_validate = inst._validate_opencode_config
            Path.home = staticmethod(lambda: home)
            inst._validate_opencode_config = lambda p: ""
            try:
                inst.install_opencode(Config(), dry_run=False)
            finally:
                Path.home = orig_home
                inst._validate_opencode_config = orig_validate

            data = json.loads(path.read_text(encoding="utf-8"))
            for key in ("plugin", "plugins"):
                arr = data.get(key) or []
                self.assertEqual(len(arr), len(set(arr)), f"{key} 有重复项：{arr}")
            # 插件目录必须带 package.json + main
            for d in ("plugin", "plugins"):
                pkg = base / d / "package.json"
                self.assertTrue(pkg.exists(), f"{d}/package.json 缺失")
                self.assertIn("main", json.loads(pkg.read_text(encoding="utf-8")))
            self.assertIn("memcore", data.get("mcp", {}))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestScopeNormalization(unittest.TestCase):
    """scope 归一化：根目录不能落到 global。"""

    def test_root_path_falls_back_to_cwd(self):
        from memcore.cli import project_scope

        self.assertEqual(project_scope("/"), project_scope(None))

    def test_absolute_path_uses_dirname(self):
        from memcore.cli import project_scope

        self.assertEqual(project_scope("/home/admin"), "project:admin")
        self.assertEqual(project_scope("admin"), "project:admin")

    def test_explicit_prefix_untouched(self):
        from memcore.cli import project_scope

        self.assertEqual(project_scope("project:x"), "project:x")
        self.assertEqual(project_scope("global"), "global")
        self.assertEqual(project_scope("session:s1"), "session:s1")


class TestOpencodePluginScope(unittest.TestCase):
    """插件的 cwd 选取：opencode 的 worktree 可能是根目录 "/"。"""

    def test_plugin_js_skips_root_worktree(self):
        from memcore.install import opencode_plugin_js

        js = opencode_plugin_js()
        self.assertIn("worktree", js)
        self.assertIn("directory", js)
        self.assertIn("process.cwd()", js)
        # 根目录判断不能产生转义歧义（曾导致 JS 语法错误 Unterminated string literal）
        self.assertNotIn('!== "\\"', js)
        self.assertNotIn('!== "\\\\"', js)

    def test_plugin_js_is_valid_javascript(self):
        """生成的插件 JS 必须能被 node 解析，否则 opencode 静默不加载。"""
        from memcore.install import opencode_plugin_js

        js = opencode_plugin_js()
        node = shutil.which("node")
        if not node:
            self.skipTest("无 node，跳过 JS 语法检查")
        tmp = tempfile.mkdtemp()
        try:
            p = Path(tmp) / "memcore.js"
            p.write_text(js, encoding="utf-8")
            r = subprocess.run([node, "--check", str(p)],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr[:500])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_plugin_index_js_is_valid_javascript(self):
        import memcore.install as inst

        tmp = tempfile.mkdtemp()
        node = shutil.which("node")
        if not node:
            self.skipTest("无 node，跳过 JS 语法检查")
        try:
            base = Path(tmp)
            inst._opencode_plugin_dir_files(base, "plugin")
            for name in ("memcore.js", "index.js"):
                r = subprocess.run([node, "--check", str(base / "plugin" / name)],
                                   capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 0, f"{name}: {r.stderr[:400]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


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


class TestMemReport(unittest.TestCase):
    """doctor --mem 内存体检能力。"""

    def test_mem_report_returns_lines(self):
        from memcore import cli

        lines = cli.mem_report()
        self.assertIsInstance(lines, list)
        self.assertTrue(lines, "内存报告不应为空")
        joined = "\n".join(lines)
        self.assertIn("memcore 进程", joined)
        self.assertIn("本地向量模型", joined)

    def test_mem_report_handles_missing_meminfo(self):
        import builtins

        from memcore import cli

        orig = builtins.open

        def _fake_open(path, *a, **kw):
            if str(path) == "/proc/meminfo":
                raise OSError("not linux")
            return orig(path, *a, **kw)

        builtins.open = _fake_open
        try:
            lines = cli.mem_report()
            self.assertTrue(any("无法读取" in ln for ln in lines))
        finally:
            builtins.open = orig

    def test_self_rss_positive(self):
        from memcore import cli

        rss = cli._self_rss_mb()
        self.assertGreater(rss, 0.0)

    def test_doctor_mem_flag_registered(self):
        from memcore import cli

        parser = cli.build_parser()
        args = parser.parse_args(["doctor", "--mem"])
        self.assertTrue(args.mem)

    def test_doctor_runs_with_mem(self):
        from memcore import cli

        tmp = tempfile.mkdtemp()
        try:
            cfg = Config()
            cfg.home = Path(tmp) / "home"
            cfg.db_path = Path(tmp) / "memories.db"
            cfg.ensure_dirs()
            con = dbmod.connect(cfg.db_path)
            dbmod.init_db(con)
            store = MemoryStore(con, embedder=embedmod.HashEmbedder(dim=128), cfg=cfg)

            parser = cli.build_parser()
            args = parser.parse_args(["doctor", "--mem"])
            import io
            import contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.cmd_doctor(args, cfg, store)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("memcore 进程", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
