#!/bin/bash
# 在阿里云上以低风险方式测量 opencode 启动的内存开销。
# 做法：后台启动 opencode，同时每 2 秒采样 total RSS，最后杀掉，避免把 1.6GB 的机器压死。
set -u
LOG=/tmp/oc_mem_watch.log
: > "$LOG"

export PATH="$HOME/.npm-global/bin:$PATH"
cd "$HOME" || exit 1

echo "=== baseline $(date +%T) ===" >> "$LOG"
free -m >> "$LOG"

# 启动一个最小 opencode 会话（超时 60s）
timeout 60 opencode run "reply with the single word: ok" >> /tmp/oc_run.log 2>&1 &
OC_PID=$!

MAX_TOTAL=0
MAX_OC=0
MAX_TOP=""
i=0
while kill -0 "$OC_PID" 2>/dev/null; do
    # 所有相关进程 RSS 之和（KB）
    T=$(ps -eo rss,comm --no-headers | awk '$2 ~ /opencode|bun|memcore|python|mcp/ {s+=$1} END {print s+0}')
    # 单进程最大
    TOP=$(ps -eo pid,rss,pcpu,comm --sort=-rss --no-headers | head -3 | tr '\n' '|')
    [ "$T" -gt "$MAX_TOTAL" ] && MAX_TOTAL=$T
    echo "$(date +%T) rel_rss_kb=$T | $TOP" >> "$LOG"
    i=$((i+1))
    sleep 2
    [ $i -gt 40 ] && break
done

echo "=== peak rel_rss_mb=$((MAX_TOTAL/1024)) ===" >> "$LOG"
free -m >> "$LOG"
cat "$LOG"
