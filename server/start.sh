#!/usr/bin/env bash
# ============================================================
# TAPD -> 企微智能表格 同步容器 启动脚本
#
# 为什么用启动脚本（而不是直接 CMD ["cron","-f"]）：
#   - 在 docker build 阶段执行 `crontab /etc/cron.d/xx` 经常因缺 PTY
#     出现"假成功"（exit 0，但 root 用户的 crontab 没真正装上）。
#   - 把装载放到运行时：容器内 PID 1 是我们、环境完整，100% 成功。
#   - 启动时立刻打印几行 bootstrap 日志，让 Rainbond 日志 Tab
#     **第一秒内就有内容**（不需要等心跳，验证秒回）。
# ============================================================
set -uo pipefail

echo "[bootstrap $(date '+%F %T')] container started, pid=$$"
# 新代码标识 + 关键 env 摘要：构建后看日志 Tab 第一行就能确认是不是 f2cdda8+、是不是指向 augJOD
echo "[bootstrap] tapd-wecom-sync NEW-VERSION: ensure_columns-aug-deployed (commit f2cdda8+)"
echo "[bootstrap] WECOM_DOCID env = ${WECOM_DOCID:-<空>} (len=${#WECOM_DOCID})"
echo "[bootstrap] WECOM_SHEET_ID  = ${WECOM_SHEET_ID:-<空>}"
echo "[bootstrap] TAPD_API_USER   = ${TAPD_API_USER:-<空>}"

# 持久化目录（即使 VOLUME 已挂载，显式 mkdir 保证安全）
mkdir -p /app/data /app/data/logs /app/data/wecom_sync_cache.json 2>/dev/null || true
mkdir -p /app/data 2>/dev/null || true

# 1) 把 /etc/cron.d/tapd-sync 装载到 root 用户的 crontab
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

# 2) 确保 /etc/cron.d/tapd-sync 自身权限正确（cron daemon 扫描要求 owner=root, mode=0644）
chmod 0644 /etc/cron.d/tapd-sync 2>/dev/null || true

# 3) 探活：写一行 boot 心跳到容器 stdout（即 /proc/1/fd/1，目标确认）
if [ -e /proc/1/fd/1 ]; then
  echo "[bootstrap] stdout target OK (/proc/1/fd/1 exists); cron jobs will write here" >> /proc/1/fd/1 2>&1
fi

echo "[bootstrap $(date '+%F %T')] starting cron -f in foreground..."

# 4) 前台运行 cron（替换 PID 1）
exec /usr/sbin/cron -f
