# 踩坑记录

这个项目在真机（Linux x86_64 / opencode 1.14.25 / Python 3.12）上开发时踩过的坑，
按"症状 → 原因 → 解法"记录，避免重复踩。

---

## 1. FTS5 内置分词器让中文检索完全失效

**症状**：记忆写得进去，但 `recall "增量快照"` 永远召回不到中文记忆。

**原因**：FTS5 的 `unicode61` 分词器把 CJK 字符当作「非字母数字」之外的连续段，
一整句中文会变成**一个 token**。查询词「增量」和索引里的「备份中心使用 rsync 做增量快照」
是两个不同的 token，匹配不上。

**解法**：加一列 `tokens`，存自己分词后的结果（中文按字 + 相邻二字组，英文按词），
FTS5 索引这一列。查询时同样用自己的分词器生成 `"词"*` 前缀匹配。

```sql
ALTER TABLE memories ADD COLUMN tokens TEXT NOT NULL DEFAULT '';
-- FTS5 定义改为 title, content, tags, tokens
```

配套的触发器也要带上 `tokens` 列，且 `update` 时必须重建 tokens
（title/content/tags 任一变化都会影响分词结果）。

---

## 2. opencode 对未知配置键零容忍

**症状**：安装器写完配置，opencode 直接拒绝启动：
```
Error: Configuration is invalid at /home/ubuntu/.config/opencode/opencode.json
↳ Unrecognized key: "plugins"
```

**原因**：opencode 1.14.x 用 `plugin` 键，1.15+ 改成 `plugins` 键。
为了"兼容两种版本"两个都写，结果整个配置作废 —— MCP 配置也跟着一起失效。

**解法**：不要靠猜。安装器写完配置后跑一次 `opencode mcp list`，
检测到 `Unrecognized key` 就换一个键重试。这个"写入→校验→纠正"的循环是通用的做法。

```python
for key in ("plugin", "plugins"):
    trial = dict(data); trial[key] = [..., "./plugins"]
    _write_json(path, trial)
    if not _validate_opencode_config(path):   # 跑 opencode mcp list 看报错
        return key
```

---

## 3. opencode 插件目录单复数两套

**症状**：插件文件放对了目录，opencode 却不加载。

**原因**：
- 1.14.x 扫描 `.opencode/plugin/`（**单数**）
- 1.15+ 扫描 `.opencode/plugins/`（**复数**）

全局目录同理：`~/.config/opencode/plugin/` vs `~/.config/opencode/plugins/`。

**解法**：两个目录都放一份插件文件。实测 1.14.25 的 `debug config` 输出里，
两个目录的文件都被自动发现了。

---

## 4. opencode 1.15+ 会跳过「指向单个文件」的插件路径

**症状**：`plugins: ["./plugins/memcore.js"]` 配了也不生效。

**原因**：文档明确写了 "Configured local paths must be directories. A path to a single file is skipped."

**解法**：路径指向**目录**（`"./plugins"`），插件本体放在目录里。

---

## 5. opencode 插件的 system prompt 注入钩子名

**症状**：用 `chat.params` 往 system prompt 塞记忆，宿主不认。

**原因**：1.14.x 的 `chat.params` 的 output 里**没有** system 字段，
只有 temperature / topP / maxOutputTokens / options。

**解法**：用 `experimental.chat.system.transform`：

```js
"experimental.chat.system.transform": async (input, output) => {
  // output.system 是 string[]
  output.system.push(memoryText);
}
```

同时保留 `chat.params` 分支做兜底（把内容塞进 `output.options`）。

---

## 6. 裸环境变量名会撞车

**症状**：`unable to open database file`，且 db 路径变成了 `.`。

**原因**：为了兼容用户习惯，代码支持了不带前缀的 `DB_PATH` 环境变量。
但目标机器上恰好存在 `DB_PATH=.`，直接把数据库路径指向了当前目录。

**解法**：只认 `MEMCORE_` 前缀。`MEMCORE_HOME` 作为特例单独兼容。
配置文件里的 `home` / `db_path` 字段仍然生效。

---

## 7. 作用域写法不统一导致召回失败

**症状**：插件里传的是绝对路径 `/home/ubuntu`，记忆存的是 `project:ubuntu`，召回为空。

**原因**：`project_scope()` 之前只做了前缀判断，绝对路径原样透传，
而存储侧取的是 `Path.cwd().name`（目录名）。两边对不上。

**解法**：scope 归一化统一取 basename：

```python
p = Path(s).expanduser()
name = p.name if (p.is_absolute() or len(p.parts) > 1) else s
return f"project:{name}"
```

---

## 8. 空查询时检索返回空，注入场景拿不到内容

**症状**：会话开始时还没��户输入，`memcore context` 查不到东西，注入的 system prompt 是空的。

**原因**：检索链路依赖查询词生成 FTS 候选，空查询自然无候选。

**解法**：空查询时降级为「按 scope 列出全部」，按 重要度 + 时效 + 使用度 排序。
会话开始这种"还不知道要问什么"的场景，本来就该看到项目全貌。

---

## 9. FTS5 外部内容表的 delete 触发器必须列全所有列

**症状**：`delete` 一条记忆后，FTS 索引里还留着它（`memories_fts` 与 `memories` 不同步）。

**原因**：外部内容表（`content='memories'`）的删除触发器要显式给出**所有列**的旧值。
新增了 `tokens` 列之后，触发器没同步更新，索引状态就乱了。

**解法**：改 schema 后，三个触发器（AI/AD/AU）都要同步加列。

---

## 10. MCP 服务端不能因为路径问题崩在启动阶段

**症状**：宿主启动 MCP server，路径不可写时进程直接退出，宿主报"连接失败"，
而不是给出可读的错误。

**解法**：`MCPServer.__init__` 捕获 `OSError` / `sqlite3.Error`，
打印到 stderr（stdout 只能写协议帧）并给出明确的路径信息。
CLI 侧同理，返回退出码 2 而不是抛栈。

---

## 11. 安装器要先探测再写，不要无条件写死路径

**症状**：`sh scripts/install.sh` 在某些机器上把 `PATH` 写进了 `.zshrc`，
但机器只有 `.bashrc`。

**解法**：按 `.bashrc` → `.zshrc` → `.profile` 顺序探测第一个存在的文件，
都没有就建 `.bashrc`。写之前先 grep 避免重复写入。

---

## 12. `zip` 命令在精简版 git-bash 里不存在

**症状**：打包技能/工程时报 `zip: command not found`。

**解法**：用 Python 的 `shutil.make_archive` 替代，跨平台且不依赖外部命令。

---

## 13. 目标机不能联网：第三方依赖必须随仓库分发

**症状**：在开发机上 `pip install numpy onnxruntime` 好好的，拷到内网机器上就装不上——
没有 PyPI 源，也没法 sudo。

**原因**：Python 生态的二进制依赖（numpy / onnxruntime）默认都从 PyPI 拉，
内网机器既没源也没有编译工具链。

**解法**：在开发机把 wheel 下好放进 `vendor/wheels/`，连同模型文件一起提交进 git，
目标机 `git clone` 后用 `pip install --no-index --find-links vendor/wheels` 离线安装。

```
vendor/wheels/numpy-1.26.4-cp39-cp310-cp311-cp312-manylinux_2_17_x86_64.whl
vendor/wheels/onnxruntime-1.16.3-cp39-cp310-cp311-...whl
vendor/wheels/onnxruntime-1.19.2-cp312-...whl
```

wheel 要覆盖多个 Python 版本（cp39/cp310/cp311/cp312），
因为目标机 Python 版本不确定。onnxruntime 1.19 才有 cp312 的 wheel，
1.16.3 最高到 cp311 —— 所以两个版本都要备。

**另外**：pip 可能压根不存在（精简镜像常见），安装器要有手动解压 wheel 的兜底路径。

---

## 14. HuggingFace 直连被拦，模型下不下来

**症状**：`urlopen error Tunnel connection failed: 502 Bad Gateway`，访问 `huggingface.co`。

**原因**：内网出口只放行了部分域名，HF 直连走不通。

**解法**：走镜像 `https://hf-mirror.com`。但**必须伪装 User-Agent**，
否则下载小文件会 403：

```python
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
```

模型源用 `Xenova/bge-small-zh-v1.5`（ONNX 导出版，带 `onnx/model_int8.onnx`），
比官方仓库的 pytorch_model.bin 轻得多。

---

## 15. 不要为分词引入 transformers / tokenizers

**症状**：为了跑本地 embedding 模型，很自然地想到 `pip install transformers`，
然后发现它要拖 torch（几百 MB）、`tokenizers`（Rust 扩展，还要编译）。

**原因**：transformers 的价值在通用性，但我们只需要标准 BERT WordPiece 的一种实现。

**解法**：自己写纯标准库 BertTokenizer（BasicTokenizer + WordPiece），
`memcore/tokenizer.py` 不到 200 行。这样整个向量化链路只剩两个二进制依赖：
numpy + onnxruntime。

关键细节：
- WordPiece 贪心最长匹配，续接片段加 `##` 前缀
- 中文按单字切分（`_tokenize_chinese_chars`）
- 未命中整词退 `[UNK]`
- `do_lower_case` 从 `tokenizer_config.json` 读

---

## 16. 切换向量模型后必须重建旧向量

**症状**：装上本地模型后，`doctor` 显示 embedder 是 `local-onnx`，
但召回质量还是老样子，语义检索几乎不生效。

**原因**：旧记忆的向量是 256 维哈希向量，新模型是 512 维。
检索时维度不匹配的向量会被**静默跳过**（`if dim != len(qvec): continue`），
不报错也不提示。

**解法**：提供 `memcore reembed` 命令，用当前 embedder 重建全部向量。
切换 provider 或换模型后必须跑一次。

```bash
memcore config set embed_provider=local
memcore reembed
```

---

## 附：真机验证清单

在 Linux 目标机上跑完这几项才算真的可用：

```bash
python3 -m unittest discover -s tests        # 单元测试
sh scripts/install.sh                        # 安装（全程离线）
memcore doctor                               # 环境自检（应显示本地向量模型就绪）
python3 -c "import numpy, onnxruntime; print('ok')"   # 二进制依赖可用
memcore remember "示例" --kind pref          # 写入
memcore recall "示例"                        # 检索
opencode mcp list                            # MCP 是否 connected
python3 -m memcore.hooks session_start       # hooks 注入（stdin 传 JSON）
node /tmp/plugtest.mjs                       # 插件注入是否进 system prompt
```

验证"真的没联网"（断网或防火墙下执行）：

```bash
pip install --no-index --find-links vendor/wheels numpy onnxruntime   # 应成功
```
