# memcore

**内网离线、免 sudo 的长记忆内核**，为 Claude Code / opencode 这类 CLI Agent 提供跨会话记忆。

- **完全离线自包含**：git clone 即可用。第三方 wheel 与本地向量模型全部随仓库提供，安装过程不联网
- 不需要 root：全部写入用户目录 `~/.local`、`~/.config`、`~/.local/share`
- 本地向量化：内置 bge-small-zh-v1.5（int8 ONNX，纯 Python 分词），不依赖任何公网 API
- 抽取模型可配置：内网 OpenAI 兼容网关 / Ollama / llama.cpp 都行，配不配置都能用
- 双宿主接入：Claude Code（hooks + MCP）与 opencode（插件 + MCP）

---

## 为什么需要它

CLI Agent 每次开新会话都是"失忆"的：偏好、技术决定、环境地址、踩过的坑都得重讲一遍。
memcore 把这些沉淀成一个可检索的记忆库，在会话开始/提问时自动回注，在会话结束时自动沉淀。

## 架构

```
        Claude Code / opencode
                  │  MCP (stdio, JSON-RPC 2.0)
                  ▼
        ┌──────────────────────┐
        │  memcore.mcp         │  8 个工具
        ├──────────────────────┤
        │  retrieve (混合检索)  │  FTS5 BM25  ⊕  向量余弦  ⊕  重要度/时效/使用度
        ├──────────────────────┤
        │  distill (记忆抽取)   │  规则抽取 ⊕ 可配置抽取模型（内网 LLM）
        ├──────────────────────┤
        │  store  (记忆仓库)    │  精确/近似去重合并、衰减归档、置顶
        └──────────────────────┘
                  │
      ┌───────────┴────────────┐
      ▼                        ▼
  本地向量模型              内网抽取模型
  bge-small-zh-v1.5        OpenAI兼容/Ollama/
  ONNX int8 / 512维        llama.cpp（可配）
      │                        │
      └────── 不可用自动降级 ─────┘
                  │
             SQLite (WAL)
        memories / memories_fts / vectors / sessions / episodes / links
```

### 记忆类型

| kind | 含义 | 示例 |
|---|---|---|
| `fact` | 客观事实 | "n8n 版本是 2.48.6" |
| `pref` | 用户偏好 | "以后都用简体中文回答" |
| `decision` | 已做的决定 | "决定用内网 Tailscale 地址，不用公网 IP" |
| `env` | 环境事实 | "MCP 端点是 http://100.94.13.25/n8n/mcp-server/http" |
| `task` | 待办 | "下一步补上回归测试" |

### 作用域隔离

- `global`：跨项目通用
- `project:<目录名>`：只在同名目录下检索时命中
- 检索 `project:X` 时，`global` 的记忆始终可见（经验是通用的）

---

## 安装（Linux，免 sudo，全程离线）

### 方式一：克隆后安装（推荐）

```bash
git clone https://github.com/wyufeng/memcore.git
cd memcore
sh scripts/install.sh
```

**整个过程不联网。** numpy / onnxruntime 从仓库里的 `vendor/wheels` 离线安装，
本地向量模型从 `vendor/models` 复制到 `~/.local/share/memcore/models`。

### 方式二：直接拷贝（无 git 环境）

```bash
cp -r memcore ~/.local/lib/memcore
mkdir -p ~/.local/bin
cat > ~/.local/bin/memcore <<'EOF'
#!/bin/sh
MEMCORE_HOME="${MEMCORE_HOME:-$HOME/.local/share/memcore}"
MEMCORE_DB_PATH="${MEMCORE_DB_PATH:-$MEMCORE_HOME/memories.db}"
MEMCORE_EMBED_MODEL_PATH="${MEMCORE_EMBED_MODEL_PATH:-$HOME/.local/share/memcore/models/bge-small-zh-v1.5}"
export MEMCORE_HOME MEMCORE_DB_PATH MEMCORE_EMBED_MODEL_PATH
PYTHONPATH="$HOME/.local/lib/memcore${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -m memcore "$@"
EOF
chmod +x ~/.local/bin/memcore
```

（这种方式不含本地模型，会退回内置哈希向量。想要语义检索请用方式一。）

安装器会：

1. 探测 `python3`（≥3.8）与 SQLite FTS5，不满足直接报错退出
2. 从 `vendor/wheels` 离线安装 numpy / onnxruntime（pip `--no-index`，pip 不可用时自动改为手动解压 wheel）
3. 拷贝包到 `~/.local/lib/memcore`，生成 `~/.local/bin/memcore` 启动脚本
4. 把 `vendor/models` 复制到 `~/.local/share/memcore/models`
5. 把 `~/.local/bin` 写进 `~/.bashrc`（或 `.zshrc`/`.profile`）
6. 检测到 `claude` / `opencode` 时自动写入接入配置
7. 跑一次 `memcore doctor` 自检

卸载：`sh scripts/uninstall.sh`（加 `--purge` 连数据一起删）

### 要求

- Linux x86_64 / macOS，Python 3.8+
- SQLite 需带 FTS5（CPython 官方发行版默认都有）
- 不需要 Node —— Node 只在你想跑 opencode 时才需要
- 磁盘约 140 MB（wheel + 模型）

### vendor 目录说明

仓库自带以下内容，保证目标机零下载：

```
vendor/
├── wheels/                        # 二进制依赖（manylinux x86_64）
│   ├── numpy-1.26.4-cp{39,310,311,312}-manylinux_2_17_x86_64.whl
│   └── onnxruntime-1.16.3-cp{39,310,311} / 1.19.2-cp312-*.whl
└── models/
    └── bge-small-zh-v1.5/         # 本地向量模型（int8 ONNX，23.9 MB）
        ├── config.json
        ├── tokenizer_config.json
        ├── special_tokens_map.json
        ├── vocab.txt
        └── onnx/model_int8.onnx
```

wheel 按 Python 版本自动匹配（cp39 / cp310 / cp311 / cp312 都有）。

> 想换更大的向量模型（如 bge-base-zh-v1.5）？把模型目录放进 `vendor/models/`，
> 然后 `memcore config set embed_model_path=<绝对路径>`，再 `memcore reembed` 重建向量。

---

## 使用

### 命令行

```bash
memcore remember "内网环境不能走公网 IP" --kind pref --tags 网络 --importance 0.9
memcore remember "MCP 端点是 http://100.94.13.25/n8n/mcp-server/http" --kind env

memcore recall "MCP 地址" --limit 5
memcore recall "语言偏好" --json

memcore list --kind pref
memcore context "当前要做的任务"        # 输出可直接注入 prompt 的记忆块
memcore distill "用户: 我偏好用 pnpm..."  # 从对话提炼
memcore forget <id>                      # 软删除（归档）
memcore forget <id> --hard               # 物理删除
memcore stats
memcore decay                            # 按衰减强度归档冷记忆
memcore reembed                          # 切换向量模型后重建全部向量
memcore doctor                           # 环境自检

memcore config show                      # 查看全部配置
memcore config keys                      # 列出可配置项
memcore config set extract_base_url=http://10.0.0.10:8000/v1 extract_model=qwen2.5-7b use_llm=true
memcore config unset extract_api_key
```

### MCP 工具

| 工具 | 作用 |
|---|---|
| `memcore_remember` | 写入一条记忆（自动去重合并） |
| `memcore_recall` | 按上下文检索，返回带分数的列表；`as_context=true` 返回可直接注入的文本 |
| `memcore_list` | 列出记忆，可按类型/作用域/标签过滤 |
| `memcore_forget` | 删除或归档 |
| `memcore_update` | 修改内容/重要度/标签/置顶 |
| `memcore_distill` | 把一段原始对话提炼成结构化记忆 |
| `memcore_stats` | 记忆库统计 |
| `memcore_session_start` | 开启会话，返回 session_id |

---

## 宿主接入

### Claude Code

安装器已自动写入 `~/.claude.json` 的 `mcpServers.memcore`：

```json
{
  "mcpServers": {
    "memcore": {
      "command": "memcore",
      "args": ["mcp"],
      "env": { "MEMCORE_HOME": "~/.local/share/memcore" }
    }
  }
}
```

自动注入记忆还需要 hooks。安装器会生成 `~/.claude/memcore-hooks.json`，把它的 `hooks` 字段合并进 `~/.claude/settings.json`：

| Hook | 行为 |
|---|---|
| `SessionStart` | 会话开始时检索并注入相关记忆 |
| `UserPromptSubmit` | 每次提问时补充检索相关记忆 |
| `Stop` | 会话结束时把对话蒸馏进记忆库 |

也可以直接用 `/memcore` 之类的斜杠命令手动调 `memcore context`。

### opencode

安装器写入 `~/.config/opencode/opencode.json` 的 `mcp.memcore`，并生成插件
`~/.config/opencode/plugins/memcore.js`（同时放一份到 `plugin/`，两个版本都能发现）。

插件在会话开始时通过 `experimental.chat.system.transform` 把记忆注入 system prompt。

> **版本差异**：opencode 1.14.x 用 `plugin` 键，1.15+ 改用 `plugins` 键。
> opencode 对未知键做**严格校验**，写错一个键会让整个配置失效。
> 安装器会自动探测版本并用 `opencode mcp list` 实测校验，写错就自动换键重试。

验证接入是否成功：

```bash
opencode mcp list
# ●  ✓ memcore connected
```

---

## 配置

配置文件：`~/.config/memcore/config.json`（首次可由 `memcore install` 生成）。所有项都能用环境变量覆盖：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `MEMCORE_HOME` | `~/.local/share/memcore` | 数据目录 |
| `MEMCORE_DB_PATH` | `$MEMCORE_HOME/memories.db` | 数据库路径 |
| `MEMCORE_LLM_BASE_URL` | 空 | 内网 OpenAI 兼容网关（用于 LLM 抽取） |
| `MEMCORE_LLM_API_KEY` | 空 | 网关密钥 |
| `MEMCORE_LLM_MODEL` | 空 | 模型名 |
| `MEMCORE_USE_LLM` | `false` | 开启后优先用 LLM 抽取 |
| `MEMCORE_EXTRACT_BASE_URL` | 空 | **抽取模型地址**（独立配置，优先于 `LLM_BASE_URL`） |
| `MEMCORE_EXTRACT_API_KEY` | 空 | 抽取模型密钥（不写入明文配置文件） |
| `MEMCORE_EXTRACT_MODEL` | 空 | **抽取模型名** |
| `MEMCORE_EXTRACT_TEMPERATURE` | `0.1` | 抽取温度（越低越稳定） |
| `MEMCORE_EXTRACT_TIMEOUT` | `60` | 抽取超时（秒） |
| `MEMCORE_EXTRACT_MAX_ITEMS` | `12` | 单次抽取最多产出几条 |
| `MEMCORE_EXTRACT_PROMPT_FILE` | 空 | 自定义抽取提示词文件（支持 `{text}` 占位） |
| `MEMCORE_EMBED_PROVIDER` | `auto` | 向量提供方：`auto`/`local`/`hash`/`remote` |
| `MEMCORE_EMBED_MODEL_PATH` | 自动探测 | 本地向量模型目录 |
| `MEMCORE_EMBED_MAX_LEN` | `256` | 分词最大长度 |
| `MEMCORE_EMBED_THREADS` | `2` | ONNX 推理线程数 |
| `MEMCORE_EMBED_BASE_URL` | 空 | 内网 embedding 网关（备用） |
| `MEMCORE_EMBED_MODEL` | 空 | embedding 模型名（备用） |
| `MEMCORE_RECALL_LIMIT` | `8` | 默认召回条数 |
| `MEMCORE_W_LEXICAL` | `1.0` | 词法分权重 |
| `MEMCORE_W_VECTOR` | `0.8` | 语义分权重 |
| `MEMCORE_W_IMPORTANCE` | `0.5` | 重要度权重 |
| `MEMCORE_W_RECENCY` | `0.35` | 时效权重 |
| `MEMCORE_DECAY_HALFLIFE` | `45` | 记忆强度半衰期（天） |

> 环境变量一律要求 `MEMCORE_` 前缀。裸 `DB_PATH` 这类通用名会被忽略，
> 避免和系统自带的环境变量撞车（踩过坑：`DB_PATH=.` 会让数据库路径变成当前目录）。

### 配置抽取模型（可选）

抽取模型是**独立配置**的，和向量模型互不影响。没配也能用（走内置规则抽取），
配了就用你指定的模型做高质量抽取。

三种部署形态都支持，写个地址就行：

```bash
# 1) OpenAI 兼容网关（vLLM / Xinference / OneAPI / 各家内网网关）
memcore config set extract_base_url=http://10.0.0.10:8000/v1 \
               extract_model=qwen2.5-14b-instruct use_llm=true

# 2) Ollama 原生接口（自动识别 11434 端口或 /api 路径）
memcore config set extract_base_url=http://10.0.0.10:11434 \
               extract_model=qwen2.5:7b use_llm=true

# 3) llama.cpp server
memcore config set extract_base_url=http://10.0.0.10:8080 \
               extract_model=qwen2.5-7b-instruct use_llm=true
```

地址规则：

| 写法 | 实际请求地址 |
|---|---|
| `http://host:8000/v1` | `http://host:8000/v1/chat/completions` |
| `http://host:8000` | `http://host:8000/v1/chat/completions`（自动补 `/v1`） |
| `http://host:11434` | `http://host:11434/api/chat`（Ollama 原生） |
| `http://host:11434/api` | `http://host:11434/api/chat` |

密钥**不会写进明文配置文件**，用环境变量传：

```bash
export MEMCORE_EXTRACT_API_KEY="sk-xxx"
```

抽取模型不可用时自动退回本地规则抽取，不会中断使用。

自定义提示词（可选，支持 `{text}` 占位）：

```bash
memcore config set extract_prompt_file=/etc/memcore/extract_prompt.txt
```

---

## 检索是怎么算分的

```
score = ( w_lexical  × BM25归一化
        + w_vector   × 余弦相似度
        + w_importance × 重要度
        + w_recency  × 0.5^(age/半衰期)
        + w_usage    × 1/(1+ln(1+使用次数))
        ) × (置顶 ? 1.25 : 1)
```

- **BM25**：FTS5 全文索引。索引的是**自定义分词结果**（中文按字 + 二元组，英文按词），
  因为 FTS5 内置的 `unicode61` 会把一整段中文当成单个 token，中文召回会直接失效。
- **向量**：默认本地 bge-small-zh-v1.5（int8 ONNX，512 维，CLS pooling + L2 归一化）。
  分词用仓库自带的纯 Python BertTokenizer，不需要 transformers / torch。
  模型或 onnxruntime 缺失时自动退回内置哈希向量（signed hashing trick），功能不中断。
- **时效**：越久没用分数越低；被使用过会回升。

## 记忆生命周期

- **去重合并**：写入时先查归一化哈希（精确重复），再做词集 Jaccard 近似比较（阈值默认 0.88），
  命中就合并：重要度取大值再 +0.05，标签并集，使用次数 +1
- **衰减**：`strength = 0.5^(天数/半衰期) × (1 + ln(1+使用次数)×0.25) × 置顶系数`
- **归档**：`memcore decay` 会把强度低于下限的冷记忆归档（软删除，可在 `list --include-archived` 看到）
- **置顶**：`memcore_update` 设 `pinned=true` 的记忆不参与衰减归档，且检索加权

---

## 开发

```bash
git clone https://github.com/wyufeng/memcore.git
cd memcore

# 免安装直接跑
PYTHONPATH=. python3 -m memcore doctor

# 测试
python3 -m unittest discover -s tests -v

# 开发模式安装
python3 setup.py develop
```

### 目录结构

```
memcore/
├── memcore/
│   ├── config.py      # 配置（env + json 双通道）
│   ├── db.py          # SQLite schema、FTS5、触发器
│   ├── store.py       # 记忆仓库：去重合并、衰减、会话、向量重建
│   ├── embed.py       # 向量提供方分派（local / hash / remote）
│   ├── embed_local.py # 本地 ONNX 向量模型
│   ├── tokenizer.py   # 纯标准库 BertTokenizer
│   ├── retrieve.py    # 混合检索与上下文渲染
│   ├── distill.py     # 规则 + 可配置抽取模型
│   ├── mcp.py         # MCP stdio 服务端（8 个工具）
│   ├── hooks.py       # Claude Code hooks
│   ├── install.py     # 宿主配置生成
│   └── cli.py         # 命令行
├── scripts/install.sh     # 离线安装器
├── scripts/uninstall.sh
├── scripts/fetch_vendor.py # 开发机用：把 wheel/模型拉进 vendor/
├── vendor/                # 离线资源（wheel + 本地模型）
├── docs/PITFALLS.md   # 真机踩坑记录
├── setup.py
└── tests/test_memcore.py
```

开发过程中踩过的坑（FTS5 中文分词失效、opencode 配置键严格校验、插件目录单复数差异等）
都记在 [docs/PITFALLS.md](docs/PITFALLS.md)，接新环境前建议先读一遍。

---

## License

MIT（代码）

随仓库分发的第三方组件：

| 组件 | 许可 | 说明 |
|---|---|---|
| `bge-small-zh-v1.5`（int8 ONNX 导出） | MIT（BAAI/bge-embedding） | 本地向量模型，23.9 MB |
| numpy 1.26.4 wheel | BSD-3-Clause | 向量计算 |
| onnxruntime wheel | MIT | ONNX 推理 |
