#!/bin/sh
# 卸载 memcore（保留数据目录，除非 --purge）
set -eu
PREFIX="${MEMCORE_PREFIX:-$HOME/.local}"
LIB_DIR="$PREFIX/lib/memcore"
BIN_DIR="$PREFIX/bin/memcore"

rm -rf "$LIB_DIR"
[ -f "$BIN_DIR" ] && rm -f "$BIN_DIR"
echo "已移除 $LIB_DIR 与 $BIN_DIR"

if [ "${1:-}" = "--purge" ]; then
    rm -rf "${MEMCORE_HOME:-$HOME/.local/share/memcore}"
    echo "已清除记忆数据"
fi
echo "如需清理 PATH，请手动删除 ~/.bashrc 中的 memcore 行。"
