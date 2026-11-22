"""宿主配置生成：Claude Code / opencode 的 MCP 与 hook 接入。

原则：只写用户目录（~/.claude、~/.config/opencode），绝不动系统目录，零 sudo。
所有写操作都支持 dry-run。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config

MEMCORE_BIN = shutil.which("memcore") or "memcore"


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: Dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _merge_mcp_servers(existing: Dict[str, Any], servers: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(existing or {})
    merged.update(servers)
    return merged


# ---------------- Claude Code ----------------

def claude_mcp_entry(cfg: Config) -> Dict[str, Any]:
    """Claude Code 的 MCP 配置（项目级 .mcp.json 与全局 ~/.claude.json 通用格式）。"""
    return {
        "command": MEMCORE_BIN,
        "args": ["mcp"],
        "env": {
            "MEMCORE_HOME": str(cfg.home),
            "MEMCORE_DB_PATH": str(cfg.db_path),
        },
    }


def install_claude(cfg: Config, dry_run: bool = False) -> Dict[str, Any]:
    """写入 Claude Code 全局 MCP 配置 ~/.claude.json 的 mcpServers 段。"""
    home = Path.home()
    settings_path = home / ".claude.json"
    data = _read_json(settings_path)
    servers = _merge_mcp_servers(data.get("mcpServers") or {}, {"memcore": claude_mcp_entry(cfg)})
    data["mcpServers"] = servers
    _write_json(settings_path, data, dry_run)

    # SessionStart hook：每次开会话自动注入相关记忆
    hooks_cfg = hook_config(cfg)
    hook_path = home / ".claude" / "memcore-hooks.json"
    _write_json(hook_path, hooks_cfg, dry_run)

    return {
        "claude_json": str(settings_path),
        "hooks": str(hook_path),
        "mcp_server": "memcore",
    }


def hook_config(cfg: Config) -> Dict[str, Any]:
    """Claude Code hooks 配置。

    - SessionStart：把项目相关记忆注入上下文
    - UserPromptSubmit：把当前提问检索出的记忆一并注入
    - Stop：会话结束后把对话蒸馏进记忆库
    """
    py = sys.executable or "python3"
    pkg_root = str(Path(__file__).resolve().parent.parent)
    env = {
        "MEMCORE_HOME": str(cfg.home),
        "MEMCORE_DB_PATH": str(cfg.db_path),
        "PYTHONPATH": pkg_root,
    }
    return {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{py} -m memcore.hooks session_start",
                            "timeout": 20,
                        }
                    ]
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{py} -m memcore.hooks prompt_submit",
                            "timeout": 20,
                        }
                    ]
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{py} -m memcore.hooks stop",
                            "timeout": 60,
                        }
                    ]
                }
            ],
        },
        "env": env,
    }


def print_instructions(cfg: Config) -> None:
    print("")
    print("接入说明：")
    print("  Claude Code")
    print(f"    1. 已写入 MCP: ~/.claude.json  (server: memcore)")
    print(f"    2. hooks 配置: ~/.claude/memcore-hooks.json")
    print("       把它合并进 ~/.claude/settings.json 的 hooks 字段即可自动注入记忆")
    print("  opencode")
    print(f"    3. 已写入: ~/.config/opencode/opencode.json (mcp.memcore)")
    print("  手动验证：")
    print("    memcore doctor")
    print("    memcore remember '示例：用户偏好使用中文回答' --kind pref")
    print("    memcore recall '语言偏好'")


# ---------------- opencode ----------------

def opencode_mcp_entry(cfg: Config) -> Dict[str, Any]:
    return {
        "type": "local",
        "command": [MEMCORE_BIN, "mcp"],
        "enabled": True,
        "environment": {
            "MEMCORE_HOME": str(cfg.home),
            "MEMCORE_DB_PATH": str(cfg.db_path),
        },
    }


def _detect_opencode_version() -> str:
    """探测 opencode 版本号。"""
    exe = shutil.which("opencode")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr or "").strip()
    except Exception:
        return ""


def _opencode_plugin_key() -> str:
    """猜 opencode 使用的插件配置键名。

    1.14.x 用 `plugin`，较新版本用 `plugins`。
    opencode 对未知键做严格校验，写错一个键会让整个配置失效，
    所以这里先按版本猜，再由 _validate_opencode_config 实测纠正。
    """
    ver = _detect_opencode_version()
    m = re.search(r"(\d+)\.(\d+)", ver)
    if m:
        major, minor = int(m.group(1)), int(m.group(2))
        if major >= 2:
            return "plugins"
        if major == 1:
            return "plugins" if minor >= 15 else "plugin"
    # 版本探测失败：先按旧版猜，后续校验会纠正
    return "plugin"


def _validate_opencode_config(path: Path) -> str:
    """跑一次 opencode 校验，返回错误信息（空串表示通过）。"""
    exe = shutil.which("opencode")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "mcp", "list"], capture_output=True, text=True,
                             timeout=45, cwd=str(path.parent))
    except Exception:
        return ""
    blob = (out.stdout or "") + (out.stderr or "")
    if "Unrecognized key" in blob or "Configuration is invalid" in blob:
        return blob.strip()
    return ""


def _pick_plugin_key(path: Path, data: Dict[str, Any], dry_run: bool) -> str:
    """确定可用的插件键名：猜测 + 实测校验纠正。

    opencode 版本跨度大（1.14 到 2.x），键名从 plugin 改成了 plugins，
    猜错会导致配置整体失效，所以必须实测。
    """
    candidates = ["plugin", "plugins"]
    for key in candidates:
        trial = dict(data)
        arr = [x for x in (trial.get(key) or []) if isinstance(x, str)]
        # 两个目录都写上，兼容 1.x 单数与 1.15+ 复数，且不重复
        for entry in ("./plugin", "./plugins"):
            if entry not in arr:
                arr.append(entry)
        seen = set()
        arr = [x for x in arr if not (x in seen or seen.add(x))]
        trial[key] = arr
        if dry_run:
            return key
        _write_json(path, trial, False)
        err = _validate_opencode_config(path)
        if not err:
            return key
    return candidates[0]


def _opencode_plugin_dir_files(base: Path, dirname: str) -> List[str]:
    """写出一个 opencode 插件目录（目录即插件）。

    opencode 1.x+ 把配置里的目录当作 npm 包安装，要求目录里有
    package.json 且带 main 字段，缺一个就会报
    "Plugin directory ... is missing package.json" 或
    "must define package.json main"，
    插件里的 .js 会被加载但目录插件安装失败。
    """
    d = base / dirname
    d.mkdir(parents=True, exist_ok=True)
    pkg = {
        "name": "memcore-plugin",
        "version": "1.0.0",
        "type": "module",
        "main": "./index.js",
    }
    (d / "package.json").write_text(
        json.dumps(pkg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (d / "memcore.js").write_text(opencode_plugin_js(), encoding="utf-8")
    (d / "index.js").write_text(
        'import { MemcorePlugin } from "./memcore.js";\n'
        "export { MemcorePlugin };\n"
        "export default MemcorePlugin;\n",
        encoding="utf-8",
    )
    return [str(d / "package.json"), str(d / "memcore.js"), str(d / "index.js")]


def install_opencode(cfg: Config, dry_run: bool = False) -> Dict[str, Any]:
    """写入 opencode 的 MCP 配置与插件。

    - 1.14.x：`plugin` 键 + `~/.config/opencode/plugin/` 目录（单数）
    - 1.15+：`plugins` 键 + `~/.config/opencode/plugins/` 目录（复数）

    插件本体放在两个目录下（opencode 会自动发现 .js 文件），
    配置键名则实测确定，避免写出 opencode 不认的键。

    注意：opencode 1.3+ 会把配置里的目录当 npm 包安装，
    因此每个目录必须带 package.json（含 main），
    否则报 "missing package.json" / "must define package.json main"。
    """
    base = Path.home() / ".config" / "opencode"
    path = base / "opencode.json"
    data = _read_json(path)

    mcp = dict(data.get("mcp") or {})
    mcp["memcore"] = opencode_mcp_entry(cfg)
    data["mcp"] = mcp

    # 先把插件目录放好（校验时 opencode 可能会加载它）
    written: List[str] = []
    for dirname in ("plugin", "plugins"):
        if dry_run:
            written.append(str(base / dirname / "memcore.js"))
        else:
            written.extend(_opencode_plugin_dir_files(base, dirname))

    key = _pick_plugin_key(path, data, dry_run)
    if dry_run:
        _write_json(path, data, dry_run)
    else:
        # 选中的键：补齐两个目录并去重
        arr = [x for x in (data.get(key) or []) if isinstance(x, str)]
        for entry in ("./plugin", "./plugins"):
            if entry not in arr:
                arr.append(entry)
        seen = set()
        data[key] = [x for x in arr if not (x in seen or seen.add(x))]
        # 另一个键（历史遗留）也顺手去重，避免留下重复项
        other = "plugins" if key == "plugin" else "plugin"
        if isinstance(data.get(other), list):
            seen2 = set()
            data[other] = [x for x in data[other]
                           if isinstance(x, str) and not (x in seen2 or seen2.add(x))]
        _write_json(path, data, dry_run)

    return {
        "opencode_json": str(path),
        "plugin": written,
        "plugin_key": key,
        "mcp_server": "memcore",
    }


def opencode_plugin_js() -> str:
    """opencode 插件：把记忆注入 system prompt。

    用 opencode 1.x 的 experimental.chat.system.transform 钩子（output.system 是 string[]）。
    同时兼容 chat.params 老写法，双保险。
    """
    return """// memcore plugin for opencode
// 把 memcore 的长期记忆注入 system prompt。会话开始时拉取项目相关记忆。
import { spawnSync } from "node:child_process";

const MEMCORE = process.env.MEMCORE_BIN || "memcore";
const LIMIT = process.env.MEMCORE_INJECT_LIMIT || "10";

function recallContext(cwd) {
  try {
    const res = spawnSync(MEMCORE, ["context", "--scope", cwd, "--limit", LIMIT], {
      encoding: "utf-8",
      timeout: 15000,
    });
    if (res.status === 0 && res.stdout && res.stdout.trim()) {
      return res.stdout.trim();
    }
  } catch (e) {
    // 记忆服务不可用不应阻塞会话
  }
  return "";
}

export const MemcorePlugin = async ({ directory, worktree }) => {
  // opencode 的 worktree 在非 git 目录下是 "/"（根目录），
  // 直接用它会让 scope 落到 global，召不回项目记忆。
  // 依次取 worktree / directory / cwd，跳过根目录与空值。
  const candidates = [worktree, directory, process.cwd()];
  let cwd = "";
  for (const c of candidates) {
    if (c && c !== "/" && c !== "\\") { cwd = c; break; }
  }
  if (!cwd) cwd = directory || process.cwd() || ".";
  let cached = "";

  return {
    "experimental.chat.system.transform": async (input, output) => {
      if (!cached) cached = recallContext(cwd);
      if (cached && Array.isArray(output.system)) {
        output.system.push(cached);
      }
    },
    "chat.params": async (input, output) => {
      // 兼容旧版：部分宿主不走 experimental 钩子，这里通过 options 兜底
      if (!cached) cached = recallContext(cwd);
      if (cached && output.options && typeof output.options === "object") {
        output.options.memcore = cached;
      }
    },
  };
};

export default MemcorePlugin;
"""
