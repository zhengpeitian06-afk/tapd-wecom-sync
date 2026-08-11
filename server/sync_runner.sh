#!/usr/bin/env bash
# ============================================================
# 每日同步编排脚本：拉取 TAPD -> 增量同步到企微智能表格
# 由 deploy.sh 写入 crontab（默认每晚 22:10），也可手动执行：bash sync_runner.sh
# ============================================================
# 不用 set -e：TAPD/企微 任一凭据错误都不应阻断另一边的诊断信息。
# 每一步用 || true 降级，所有错误都打到 stdout 和日志文件，方便排查。
set -uo pipefail

# 定位本脚本目录并加载配置
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 凭据两种来源（二选一即可）：
#   1) 本地 docker-compose：把 config.env 挂载到 /app/server/config.env（与本脚本同目录）
#   2) Rainbond 等平台：直接以环境变量注入，无需 config.env
CONFIG_ENV="${SYNC_CONFIG_ENV:-$SCRIPT_DIR/config.env}"
if [[ -f "$CONFIG_ENV" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$CONFIG_ENV"
  set +a
fi

# 校验关键变量（无论来自 config.env 还是平台环境变量）
: "${TAPD_API_USER:?未配置 TAPD_API_USER（写入 config.env 或在平台环境变量中设置）}"
: "${TAPD_API_PASSWORD:?未配置 TAPD_API_PASSWORD}"
: "${WECOM_CORPID:?未配置 WECOM_CORPID}"
: "${WECOM_CORPSECRET:?未配置 WECOM_CORPSECRET}"

# 脚本目录：容器内标准位置为 /app/scripts；本地运行回退到本脚本同级的 scripts/
if [[ -d /app/scripts ]]; then
  SYNC_HOME="${SYNC_HOME:-/app/scripts}"
else
  SYNC_HOME="${SYNC_HOME:-$SCRIPT_DIR/scripts}"
fi

# 缓存（record_id 映射）/ 中间文件 / 日志 默认落到 /app/data（持久化卷），
# 这样组件或容器重建后映射不丢、避免重复写入。
LOG_DIR="${LOG_DIR:-/app/data/logs}"
WECOM_CACHE="${WECOM_CACHE:-/app/data/wecom_sync_cache.json}"
TAPD_RAW_JSON="${TAPD_RAW_JSON:-/app/data/tapd_current_oa.json}"
mkdir -p "$LOG_DIR" "$(dirname "$WECOM_CACHE")"

# python3 绝对路径（cron 的 PATH 不含 /usr/local/bin，必须用绝对路径）
PY="${PYTHON_BIN:-/usr/local/bin/python3}"
command -v "$PY" >/dev/null 2>&1 || PY="$(command -v python3 || echo python3)"
TS="$(date '+%Y-%m-%d_%H%M%S')"
LOG_FILE="$LOG_DIR/sync_$TS.log"
# 保留最近 30 天日志
find "$LOG_DIR" -name 'sync_*.log' -mtime +30 -delete 2>/dev/null || true

# 容器内写日志到 /app/data/cron.log（start.sh 的 tail -F 会把它 mirror 到 PID 1 stdout，
# Rainbond 日志 Tab 自动可见）。本地/Mac 没这个目录时只写 LOG_FILE，不影响本地运行。
LOG_TO_CRON="/app/data/cron.log"
if [[ -e /app/data ]]; then
  exec > >(tee -a "$LOG_FILE" "$LOG_TO_CRON") 2>&1
else
  exec > >(tee -a "$LOG_FILE") 2>&1
fi
echo "===== 同步开始 $(date) ====="

echo "[1/2] 拉取 TAPD 需求…"
if "$PY" "$SYNC_HOME/tapd_fetcher.py" \
    --config "$SYNC_HOME/project_oa_config.json" \
    --output "$TAPD_RAW_JSON"; then
  echo "[1/2] TAPD 拉取成功：$TAPD_RAW_JSON"
else
  rc=$?
  echo "[1/2] ⚠️  TAPD 拉取失败（exit=$rc）。请检查 TAPD_API_USER / TAPD_API_PASSWORD。"
  echo "[1/2] ⚠️  继续尝试企微同步（写入空 stories 让 ensure_columns/stamp_sync_time 跑起来）"
  # 兜底：写一个空 stories JSON，让 incremental_sync 能跑下去（用于诊断企微是否正常）
  if [[ ! -s "$TAPD_RAW_JSON" ]]; then
    echo '{"stories": [], "fetch_errors": 1, "fetch_error_detail": "TAPD 拉取失败，请检查 TAPD_API_USER/PASSWORD"}' > "$TAPD_RAW_JSON"
  fi
fi

echo "[2/2] 增量同步到企微智能表格…"
if "$PY" "$SYNC_HOME/incremental_sync.py" \
    "$TAPD_RAW_JSON" \
    --config "$SYNC_HOME/project_oa_config.json" \
    --cache "$WECOM_CACHE"; then
  echo "[2/2] 企微同步成功"
else
  rc=$?
  echo "[2/2] ⚠️  企微同步失败（exit=$rc）。请检查 WECOM_CORPID / WECOM_CORPSECRET，"
  echo "[2/2] ⚠️  并确认自建应用已加入 augJOD 「可调用应用」勾选 读+写。"
fi

echo "===== 同步结束 $(date) ====="
