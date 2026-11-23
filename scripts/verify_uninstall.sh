#!/bin/bash
# 卸载后校验：确认 memcore 无残留，且机器原有功能未受影响。
echo "=== 1. memcore 残留搜索（排除备份目录）==="
find "$HOME" -maxdepth 4 -iname "*memcore*" 2>/dev/null | grep -v "memcore-uninstall-backup" | head -10
echo "(以上为空即无残留)"
echo
echo "=== 2. which memcore ==="
command -v memcore 2>&1
echo "(空=已卸载)"
echo
echo "=== 3. opencode 配置 ==="
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".config/opencode/opencode.json"
c = json.loads(p.read_text(encoding="utf-8"))
print("keys     :", list(c.keys()))
print("mcp      :", c.get("mcp"))
print("plugin   :", c.get("plugin"))
print("model    :", c.get("model"))
print("providers:", list(c.get("provider", {}).keys()))
PY
echo
echo "=== 4. opencode 配置目录 ==="
ls -la ~/.config/opencode/
echo
echo "=== 5. 原有 Python 包是否完好 ==="
python3 -c "
import importlib
for m in ['numpy','sklearn','pandas','mlflow','cvxpy','sympy','packaging']:
    try:
        mod = importlib.import_module(m)
        print(f'  {m:10} OK  {getattr(mod, \"__version__\", \"?\")}')
    except Exception as e:
        print(f'  {m:10} BROKEN  {type(e).__name__}: {e}')
"
echo
echo "=== 6. mem0 那套是否未受影响 ==="
systemctl is-active embed_server.service mem0-api.service memos.service 2>&1 | head -5
echo
echo "=== 7. 内存现状 ==="
free -m
