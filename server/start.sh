#!/usr/bin/env bash
# ============================================================
# TAPD -> 企微智能表格 同步容器 启动脚本
#
# 关键变化（解决 Rainbond 日志 Tab 空白的根因）：
#   - 旧版本最后一行 `exec /usr/sbin/cron -f` 让 cron 替换 PID 1，
#     导致 cron 子进程（sync_runner.sh）的 stdout 默认流向 cron 内部
#     日志通道，不是 PID 1 stdout——Rainbond 采集 PID 1 stdout 时**永远空白**。
#   - 新版本：cron 放到后台跑；PID 1 = `tail -F /app/data/cron.log`，
#     这样同步脚本写到 cron.log 的所有内容都被实时 mirror 到 stdout，
#     Rainbond 日志 Tab 100% 能看见。
# ============================================================
set -uo pipefail

echo "[bootstrap $(date '+%F %T')] container started, pid=$$"
echo "[bootstrap] tapd-wecom-sync NEW-VERSION: stdout-tail-deployed (commit after 87d4424)"
echo "[bootstrap] WECOM_DOCID env = ${WECOM_DOCID:-<空>} (len=${#WECOM_DOCID})"
echo "[bootstrap] WECOM_SHEET_ID  = ${WECOM_SHEET_ID:-<空>}"
echo "[bootstrap] TAPD_API_USER   = ${TAPD_API_USER:-<空>}"
echo "[bootstrap] WECOM_CORPID    = ${WECOM_CORPID:-<空>}"

# 持久化目录 + 确保 cron.log 文件存在（tail 立刻可读）
mkdir -p /app/data /app/data/logs
touch /app/data/cron.log
echo "[bootstrap] cron.log size BEFORE first sync = $(stat -c%s /app/data/cron.log 2>/dev/null || wc -c < /app/data/cron.log) bytes"

# 1) 装载 crontab 到 root 用户
if [[ -f /etc/cron.d/tapd-sync ]]; then
  if crontab /etc/cron.d/tapd-sync; then
    echo "[bootstrap $(date '+%F %T')] crontab installed from /etc/cron.d/tapd-sync"
    echo "[bootstrap] current cron jobs:"
    crontab -l | sed 's/^/    /'
  else
    echo "[bootstrap] WARN: crontab install returned non-zero; continuing anyway"
  fi
else
  echo "[bootstrap] ERROR: /etc/cron.d/tapd-sync not found"
fi

chmod 0644 /etc/cron.d/tapd-sync 2>/dev/null || true

# 2) 后台启动 cron daemon（不再替换 PID 1）
#    把 cron 自身的 stdout/stderr 也重定向到 cron.log，方便集中查看
/usr/sbin/cron >> /app/data/cron.log 2>&1 &
CRON_PID=$!
disown
echo "[bootstrap] cron daemon started in background, pid=$CRON_PID"

# 3) 探活：确认 cron 进程起来
sleep 1
if kill -0 "$CRON_PID" 2>/dev/null; then
  echo "[bootstrap] cron process alive after 1s (pid=$CRON_PID, rss=$(ps -o rss= -p "$CRON_PID" 2>/dev/null || echo n/a))"
else
  echo "[bootstrap] ⚠️  cron process died immediately"
  echo "[bootstrap] ⚠️  check: /etc/cron.d/tapd-sync perms or service cron status"
fi

# 4) PID 1 自己前台 tail cron.log → 同步 + heartbeat + bootstrap 全在 stdout
#    -n +1 不丢已有内容（首次启动如果 PV 上已有 cron.log 也能看到）
#    -F 跟随滚动 + 处理文件被重建（logrotate 之类）
echo "[bootstrap] PID=1 taking over: tailing /app/data/cron.log -> stdout"
echo "[bootstrap] expect this to appear in Rainbond 日志 Tab within 1-2s:"
echo "=========================================="
exec tail -n +1 -F /app/data/cron.log
