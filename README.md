# memcore

**内网离线、免 sudo 的长记忆内核**，为 Claude Code / opencode 这类 CLI Agent 提供跨会话记忆。

- 零第三方依赖：只用 Python 标准库（sqlite3 + FTS5 + urllib），不需要 pip install、不需要 npm install
- 不需要 root：全部写入用户目录 `~/.local`、`~/.config`、`~/.local/share`
- 离线可用：默认用本地哈希向量 + 规则抽取，不依赖任何外部服务；可选接内网 LLM / embedding 网关增强
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
        │  distill (记忆抽取)   │  规则抽取 ⊕ 可选内网 LLM
        ├──────────────────────┤
        │  store  (记忆仓库)    │  精确/近似去重合并、衰减归档、置顶
        └──────────────────────┘
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

## 安装（Linux，免 sudo）

### 方式一：一键脚本

```bash
curl -fsSL https://raw.githubusercontent.com/wyufeng/memcore/main/scripts/install.sh | sh
```

### 方式二：克隆后安装

```bash
git clone https://github.com/wyufeng/memcore.git
cd memcore
sh scripts/install.sh
```

### 方式三：直接拷贝（无 git 环境）

```bash
cp -r memcore ~/.local/lib/memcore
mkdir -p ~/.local/bin
cat > ~/.local/bin/memcore <<'EOF'
#!/bin/sh
MEMCORE_HOME="${MEMCORE_HOME:-$HOME/.local/share/memcore}"
MEMCORE_DB_PATH="${MEMCORE_DB_PATH:-$MEMCORE_HOME/memories.db}"
export MEMCORE_HOME MEMCORE_DB_PATH
PYTHONPATH="$HOME/.local/lib/memcore${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -m memcore "$@"
EOF
chmod +x ~/.local/bin/memcore
```

安装器会：

1. 探测 `python3`（≥3.8）与 SQLite FTS5，不满足直接报错退出
2. 拷贝包到 `~/.local/lib/memcore`，生成 `~/.local/bin/memcore` 启动脚本
3. 把 `~/.local/bin` 写进 `~/.bashrc`（或 `.zshrc`/`.profile`）
4. 检测到 `claude` / `opencode` 时自动写入接入配置
5. 跑一次 `memcore doctor` 自检

卸载：`sh scripts/uninstall.sh`（加 `--purge` 连数据一起删）

### 要求

- Linux x86_64 / macOS，Python 3.8+
- SQLite 需带 FTS5（CPython 官方发行版默认都有）
- 不需要 Node —— Node 只在你想跑 opencode 时才需要

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
memcore doctor                           # 环境自检
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
| `MEMCORE_EMBED_BASE_URL` | 空 | 内网 embedding 网关 |
| `MEMCORE_EMBED_MODEL` | 空 | embedding 模型名 |
| `MEMCORE_RECALL_LIMIT` | `8` | 默认召回条数 |
| `MEMCORE_W_LEXICAL` | `1.0` | 词法分权重 |
| `MEMCORE_W_VECTOR` | `0.8` | 语义分权重 |
| `MEMCORE_W_IMPORTANCE` | `0.5` | 重要度权重 |
| `MEMCORE_W_RECENCY` | `0.35` | 时效权重 |
| `MEMCORE_DECAY_HALFLIFE` | `45` | 记忆强度半衰期（天） |

> 环境变量一律要求 `MEMCORE_` 前缀。裸 `DB_PATH` 这类通用名会被忽略，
> 避免和系统自带的环境变量撞车（踩过坑：`DB_PATH=.` 会让数据库路径变成当前目录）。

### 接内网 LLM 网关（可选）

```bash
export MEMCORE_LLM_BASE_URL="http://内网网关/v1"
export MEMCORE_LLM_API_KEY="sk-xxx"
export MEMCORE_LLM_MODEL="qwen2.5-14b-instruct"
export MEMCORE_USE_LLM=1
```

网关不可用时自动退回本地规则抽取，不会中断使用。

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
- **向量**：默认本地哈希向量（signed hashing trick），字符 n-gram + 词元混合，无需下载模型；
  配了 embedding 网关就换成网关向量，网关挂了自动退回本地。
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
│   ├── store.py       # 记忆仓库：去重合并、衰减、会话
│   ├── embed.py       # 哈希向量 / 远端 embedding
│   ├── retrieve.py    # 混合检索与上下文渲染
│   ├── distill.py     # 规则 + LLM 记忆抽取
│   ├── mcp.py         # MCP stdio 服务端（8 个工具）
│   ├── hooks.py       # Claude Code hooks
│   ├── install.py     # 宿主配置生成
│   └── cli.py         # 命令行
├── scripts/install.sh
├── scripts/uninstall.sh
├── docs/PITFALLS.md   # 真机踩坑记录
├── setup.py
└── tests/test_memcore.py
```

开发过程中踩过的坑（FTS5 中文分词失效、opencode 配置键严格校验、插件目录单复数差异等）
都记在 [docs/PITFALLS.md](docs/PITFALLS.md)，接新环境前建议先读一遍。

---

## License

MIT
