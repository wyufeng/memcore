"""memcore - 内网免 sudo 长记忆内核。

零第三方依赖：只用 Python 标准库（sqlite3 / json / hashlib / urllib ...）。
面向 Claude Code / opencode 等 CLI Agent，通过 MCP stdio 暴露记忆读写能力。
"""

__version__ = "0.1.0"
__all__ = ["config", "db", "store", "embed", "retrieve", "distill", "ingest", "mcp", "cli"]
