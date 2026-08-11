# ============================================================
# TAPD -> 企微智能表格 同步 容器镜像
# 仅用 Python 标准库，无需 pip 安装任何包。
#
# 本镜像用于 Rainbond「从源码构建」：Rainbond 会拉取含本 Dockerfile 的
# 代码仓库并自行执行 `docker build`，无需你本地有 Docker / 镜像仓库。
#
# 运行行为：
#  - 容器内 cron 每天 22:10（Asia/Shanghai）自动执行 sync_runner.sh
#  - 凭据通过「平台环境变量」注入（TAPD_API_USER / TAPD_API_PASSWORD /
#    WECOM_CORPID / WECOM_CORPSECRET），不写进镜像、不进代码仓库
#  - 缓存 / 中间文件 / 日志 落在 /app/data（Rainbond 据 VOLUME 自动持久化）
#  - 复用现有表 augJOD（docid/sheet_id 已预置，可在平台环境变量覆盖）
# ============================================================
FROM python:3.13-slim

# 安装 cron 并设时区（python:3.13-slim 基于 Debian）
RUN apt-get update \
 && apt-get install -y --no-install-recommends cron \
 && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 复用 augJOD 智能表格（用户已选"复用现有表"）。
# 如需改用新表，在 Rainbond「环境变量」中覆盖这两个值即可。
ENV WECOM_DOCID="dccARy9b7NbhAPFS_noXF4INKLW__6AzlSZMmuwxov4_wUl1G6esRLxXu7XFXleFHqyYYfpgnJ25jG67EWv3p5DQ"
ENV WECOM_SHEET_ID="augJOD"

WORKDIR /app

# 复制同步代码与运行脚本（不含任何凭据）
COPY scripts/ /app/scripts/
COPY server/sync_runner.sh /app/server/sync_runner.sh

# 赋可执行权限
RUN chmod +x /app/scripts/*.py /app/server/sync_runner.sh

# 持久化目录（缓存 / 中间文件 / 日志）——Rainbond 会据此自动创建持久化存储
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# 安装定时任务（每天 22:10，时区见 ENV TZ）
COPY server/crontab /etc/cron.d/tapd-sync
RUN chmod 0644 /etc/cron.d/tapd-sync \
 && crontab /etc/cron.d/tapd-sync

# 前台运行 cron，保持容器存活（非 Web 服务）
CMD ["/usr/sbin/cron", "-f"]
