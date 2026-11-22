#!/bin/sh
# memcore 一键安装脚本（Linux / macOS，免 sudo，全程离线）
#
#   git clone https://github.com/wyufeng/memcore.git
#   cd memcore && sh scripts/install.sh
#
# 特性：
# - 只写用户目录（~/.local、~/.config），不需要 root
# - 零联网：numpy / onnxruntime 从 vendor/wheels 离线安装，本地模型从 vendor/models 复制
# - 本地向量模型不可用时自动退回内置哈希向量，功能不中断
# - 自动探测 Python3（>=3.8）与 SQLite FTS5
# - 可选一键写入 Claude Code / opencode 接入配置

set -eu

PREFIX="${MEMCORE_PREFIX:-$HOME/.local}"
REPO_DIR="${MEMCORE_REPO_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
BIN_DIR="$PREFIX/bin"
LIB_DIR="$PREFIX/lib/memcore"
MODEL_DIR="$HOME/.local/share/memcore/models"

log()  { printf '\033[36m[memcore]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[memcore]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[memcore]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 1. 环境探测 ----------

PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,8) else 1)' 2>/dev/null; then
            PY="$(command -v "$cand")"
            break
        fi
    fi
done
[ -n "$PY" ] || die "未找到 Python 3.8+，请先安装 python3"

log "Python: $PY ($("$PY" -V 2>&1))"

"$PY" - <<'PYEOF' || die "当前 Python 的 sqlite3 缺少 FTS5，检索会退化，请更换 Python 构建"
import sqlite3, sys
c = sqlite3.connect(":memory:")
try:
    c.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
except Exception as exc:
    print("FTS5 不可用:", exc, file=sys.stderr)
    sys.exit(1)
PYEOF
log "SQLite FTS5: 可用"

PY_TAG="$("$PY" -c 'import sys; t=sys.implementation; print("cp%d%d" % sys.version_info[:2])')"

# ---------- 2. 离线安装二进制依赖（numpy / onnxruntime） ----------
#
# 目标机不允许联网，依赖全部来自仓库内 vendor/wheels。
# 优先用 pip --no-index；pip 不可用或失败时，回退到手动解压 wheel 到用户 site-packages。

SITE_DIR="$("$PY" -c 'import site,sys; d=site.getusersitepackages(); print(d)' 2>/dev/null || true)"
[ -n "$SITE_DIR" ] || SITE_DIR="$HOME/.local/lib/python3/site-packages"

install_wheels() {
    WHEEL_DIR="$REPO_DIR/vendor/wheels"
    [ -d "$WHEEL_DIR" ] || { warn "vendor/wheels 不存在，跳过二进制依赖安装"; return 0; }

    # 只挑与当前 Python 版本匹配的二进制 wheel（cp39/cp310/...）+ 纯 Python wheel
    SEL=$(ls "$WHEEL_DIR" 2>/dev/null | grep -E -- "-$PY_TAG-|none-any\.whl$" || true)
    if [ -z "$SEL" ]; then
        warn "vendor/wheels 中没有匹配 $PY_TAG 的 wheel，跳过二进制依赖安装"
        return 0
    fi

    # 优先走 pip：一次装齐 onnxruntime 及其传递依赖
    if "$PY" -m pip --version >/dev/null 2>&1; then
        if "$PY" -m pip install --no-index --find-links "$WHEEL_DIR" \
               --target "$SITE_DIR" --upgrade numpy onnxruntime >/dev/null 2>&1; then
            log "已离线安装 numpy + onnxruntime（pip --no-index）"
        else
            warn "pip 离线安装失败，改为手动解压全部 wheel"
            extract_wheels_manually
        fi
    else
        warn "未找到 pip，改为手动解压全部 wheel"
        extract_wheels_manually
    fi

    # 验证 numpy / onnxruntime 能否导入
    if PYTHONPATH="$SITE_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PY" -c 'import numpy, onnxruntime' 2>/dev/null; then
        log "numpy + onnxruntime: 可用（本地向量模型就绪）"
    else
        warn "numpy / onnxruntime 不可用，向量化将退回内置哈希向量"
    fi
}

# 兜底：没有 pip 时手动解压所有匹配的 wheel（含传递依赖）
extract_wheels_manually() {
    TMP=$(mktemp -d)
    if "$PY" - "$WHEEL_DIR" "$TMP" "$SEL" <<'PYEOF'
import os, sys, zipfile
wheels_dir, dest = sys.argv[1], sys.argv[2]
names = sys.argv[3].split()
for name in names:
    if not name:
        continue
    path = os.path.join(wheels_dir, name)
    if not os.path.isfile(path):
        continue
    with zipfile.ZipFile(path) as z:
        z.extractall(dest)
    # wheel 里 <pkg>-<ver>.data/ 下的内容并入目标
    for d in os.listdir(dest):
        if not d.endswith(".data"):
            continue
        data = os.path.join(dest, d)
        for sub in os.listdir(data):
            src = os.path.join(data, sub)
            if os.path.isdir(src):
                for item in os.listdir(src):
                    s = os.path.join(src, item)
                    t = os.path.join(dest, item)
                    if os.path.isdir(s):
                        os.makedirs(t, exist_ok=True)
                        for f in os.listdir(s):
                            with open(os.path.join(s, f), "rb") as fi, \
                                 open(os.path.join(t, f), "wb") as fo:
                                fo.write(fi.read())
                    else:
                        with open(s, "rb") as fi, open(t, "wb") as fo:
                            fo.write(fi.read())
print("ok")
PYEOF
    then
        mkdir -p "$SITE_DIR"
        cp -R "$TMP"/* "$SITE_DIR"/ 2>/dev/null || true
        rm -rf "$TMP"
        log "已离线安装 numpy + onnxruntime（手动解压）"
    else
        rm -rf "$TMP"
        warn "手动解压失败"
    fi
}

install_wheels

# ---------- 3. 安装主程序 ----------

mkdir -p "$LIB_DIR" "$BIN_DIR"
rm -rf "$LIB_DIR/memcore"
cp -R "$REPO_DIR/memcore" "$LIB_DIR/memcore"
find "$LIB_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$LIB_DIR" -name '*.pyc' -delete 2>/dev/null || true

# ---------- 4. 复制本地向量模型 ----------

if [ -d "$REPO_DIR/vendor/models" ]; then
    mkdir -p "$MODEL_DIR"
    # rsync 不可用时退化为 cp
    if command -v rsync >/dev/null 2>&1; then
        rsync -a "$REPO_DIR/vendor/models/" "$MODEL_DIR/"
    else
        cp -R "$REPO_DIR/vendor/models/." "$MODEL_DIR/"
    fi
    log "本地向量模型: $MODEL_DIR"
else
    warn "vendor/models 不存在，本地向量化不可用（将退回哈希向量）"
fi

cat > "$BIN_DIR/memcore" <<EOF
#!/bin/sh
# memcore launcher (generated by install.sh)
MEMCORE_HOME="\${MEMCORE_HOME:-\$HOME/.local/share/memcore}"
MEMCORE_DB_PATH="\${MEMCORE_DB_PATH:-\$MEMCORE_HOME/memories.db}"
MEMCORE_EMBED_MODEL_PATH="\${MEMCORE_EMBED_MODEL_PATH:-\$HOME/.local/share/memcore/models/bge-small-zh-v1.5}"
export MEMCORE_HOME MEMCORE_DB_PATH MEMCORE_EMBED_MODEL_PATH
PYTHONPATH="$SITE_DIR:$LIB_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec "$PY" -m memcore "\$@"
EOF
chmod +x "$BIN_DIR/memcore"

log "已安装: $LIB_DIR/memcore"
log "已生成: $BIN_DIR/memcore"

# ---------- 5. PATH 提示 ----------

SHELL_RC=""
for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
    if [ -f "$rc" ]; then SHELL_RC="$rc"; break; fi
done
if [ -z "$SHELL_RC" ]; then SHELL_RC="$HOME/.bashrc"; fi

if ! grep -q "$BIN_DIR" "$SHELL_RC" 2>/dev/null; then
    printf '\n# memcore\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$SHELL_RC"
    log "已写入 PATH 到 $SHELL_RC（重开终端或 source 生效）"
else
    log "PATH 已包含 $BIN_DIR"
fi

# ---------- 6. 宿主接入 ----------

case "${MEMCORE_SKIP_HOST:-0}" in
    1) log "跳过宿主配置（MEMCORE_SKIP_HOST=1）" ;;
    *)
        if command -v claude >/dev/null 2>&1; then
            "$BIN_DIR/memcore" install --target claude >/dev/null 2>&1 \
                && log "已写入 Claude Code 配置" \
                || warn "Claude Code 配置写入失败（可手动执行 memcore install --target claude）"
        else
            log "未检测到 claude，跳过 Claude Code 配置"
        fi
        if command -v opencode >/dev/null 2>&1; then
            "$BIN_DIR/memcore" install --target opencode >/dev/null 2>&1 \
                && log "已写入 opencode 配置" \
                || warn "opencode 配置写入失败"
        else
            log "未检测到 opencode，跳过 opencode 配置"
        fi
        ;;
esac

# ---------- 7. 自检 ----------

"$BIN_DIR/memcore" doctor

cat <<EOF

安装完成 🎉

快速验证：
  $BIN_DIR/memcore remember "示例：用户偏好使用中文回答" --kind pref
  $BIN_DIR/memcore recall "语言偏好"

配置抽取模型（内网 OpenAI 兼容 / Ollama / llama.cpp 均可）：
  $BIN_DIR/memcore config set extract_base_url=http://10.0.0.10:11434/v1 \\
      extract_model=qwen2.5:7b use_llm=true
  $BIN_DIR/memcore config set extract_base_url=http://10.0.0.10:8000 \\
      extract_model=qwen2.5-7b-instruct use_llm=true     # llama.cpp server
  # 密钥不落明文，可用环境变量 MEMCORE_EXTRACT_API_KEY

接入说明见 README「宿主接入」一节。
EOF
