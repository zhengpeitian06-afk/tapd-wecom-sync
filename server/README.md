# TAPD → 企微智能表格 增量同步 · 服务器部署版

将 TAPD 中所有带「OA 需求单号」的需求，**单向、增量**同步到企业微信智能表格，供 OA 需求提交人查看进度。**本版本不依赖 WorkBuddy / MCP / wecom-cli**，可在任意 Linux 云服务器上独立运行，由 crontab 定时驱动。

---

## 一、为什么"一次授权即可自动跑"

本工具**完全不使用**会过期的 OAuth 连接器（即 WorkBuddy 里那个经常断线重连的 TAPD / 企微连接器）。它改用**长期有效的 API 凭据**，所以配置一次后就不再需要人工授权：

| 系统 | 鉴权方式 | 有效期 | 是否需要每次授权 |
|---|---|---|---|
| TAPD | API 账号 BasicAuth（`TAPD_API_USER` / `TAPD_API_PASSWORD`） | 长期有效 | ❌ 一次填入即可 |
| 企业微信 | 自建应用 `corpid` + `corpsecret` 换 `access_token` | token 7200s，代码内自动刷新 | ❌ 一次填入即可 |

> ⚠️ **与 WorkBuddy 连接器的区别**：WorkBuddy 里的 TAPD / 企微连接器是 OAuth 授权，会过期、会断线，需要人去点"同意授权"。本服务器版走的是 API 账号 / 自建应用凭据，属于企业后台的「长期凭证」，不会过期，因此适合无人值守的定时任务。
>
> 这意味着：**你（或管理员）需要先在企微后台创建一个「自建应用」并拿到 corpid + Secret**（详见第三节）。这一步就是字面意义上的"一次授权"。

---

## 二、工作原理

```
┌────────────┐   TAPD OpenAPI (Basic Auth)    ┌──────────────────┐
│  TAPD 平台  │ ────────────────────────────> │  tapd_fetcher.py │
└────────────┘                               └────────┬─────────┘
                                                     │ /tmp/tapd_current_oa.json
                                                     ▼
                                          ┌──────────────────┐
                                          │ incremental_sync │  diff + 状态码翻译
                                          └────────┬─────────┘
                                                   │ 企微 wedoc/smartsheet API (自建应用 token)
                                                   ▼
                                          ┌──────────────────┐
                                          │ 企微智能表格       │ （复用 augJOD / 或自动新建）
                                          └──────────────────┘
```

- **TAPD 拉取**：直连 `https://api.tapd.cn/stories`，用 API 账号 BasicAuth。
- **企微写入**：用自建应用 `corpid + corpsecret` 换 `access_token`，调用 `/cgi-bin/wedoc/smartsheet/*`。
- **增量同步**：本地缓存 `wecom_sync_cache.json` 记录 `TAPD需求ID → 企微 record_id`，每次只处理「新增 / 变更 / TAPD 侧已移除」的记录，**无变化则跳过**。
- **复用已有表（augJOD）**：首次运行会先**读回** augJOD 现有 361 行，按「TAPD需求ID」把 `record_id` 对齐进缓存（reconcile），因此**不会重复写入**；之后按增量更新。要求自建应用对 augJOD 拥有**读 + 写**权限。
- **状态码翻译**：TAPD 状态码（如 `status_4`）含义跨项目不同，已为 12 个项目分别建立映射，保存在 `project_oa_config.json` 的 `status_mappings`。

---

## 三、前置准备（一次授权）

### 1. TAPD API 账号（应已具备）
TAPD 后台「公司管理 → API 账号」创建，得到 `api_user` 与 `api_password`。
（也可用开放平台 Token：`TAPD_TOKEN`，二选一。）

### 2. 企微自建应用（关键一步）
1. 企业微信管理后台「应用管理 → 应用 → 创建应用（自建）」，得到：
   - **corpid**：「我的企业」页面顶部
   - **Secret**：刚创建的应用详情页
2. 给该应用授予**文档**相关接口权限（应用详情 → 接口权限 → 勾选文档类）。
3. **把自建应用加入目标智能表格的「可调用应用」并授予读 + 写**：
   - 打开 augJOD 智能表格 → 右上角「…」/「分享」→「可调用应用」→ 添加刚才的自建应用。
   - 权限需包含**可读取内容**与**可编辑**（否则首次对账读表时会报 60020 / 无权限）。
   - 若你选择让服务器**新建**一张表（而非复用 augJOD），则建表本身由代码完成，建好后同样需把自建应用加进去（首次运行日志会提示 docid）。

> 没有企业微信后台权限？把本文档第三节转发给有「应用管理」权限的管理员，请其创建自建应用并把 corpid + Secret 给你填入 `config.env` 即可。

### 你只需亲手做的两件事（一次性，之后永不再授权）

| # | 在哪里做 | 具体动作 | 目的 |
|---|---------|---------|------|
| ① | **企业微信管理后台**（网页端，需「应用管理」权限） | 创建「自建应用」→ 拿到 `corpid` + `Secret`；再到 augJOD 表格「分享 → 可调用应用」把它加进去并授予**读 + 写** | 拿到服务器免重复授权的长期凭据，并让它能读写目标表 |
| ② | **服务器上的 `config.env` 文件**（不是本地电脑） | 把 ① 拿到的 `corpid`+`Secret` 填进 `config.env` 的 `WECOM_CORPID` / `WECOM_CORPSECRET`（TAPD 凭据 `TAPD_API_USER`/`PASSWORD` 你应已填好） | 让脚本运行时能读到凭据 |

> 为什么只做一次：脚本用这些凭据在代码里**自动刷新 token**，没有会过期的 OAuth 弹窗。填好之后 cron 每天自动跑，永不重复授权。
> 如果你没有企微后台权限，把本文档第三节 + 上表发给有管理员权限的同事代做 ①，你只做 ②。

---

## 四、部署步骤

```bash
# 1. 把整个项目目录上传到服务器（例如 /opt/tapd-wecom-sync）
scp -r tapd-wecom-sync user@server:/opt/tapd-wecom-sync

# 2. 登录服务器，执行部署脚本
cd /opt/tapd-wecom-sync/server
bash deploy.sh
```

`deploy.sh` 会自动：
- 检查 python3（仅用标准库，无需 pip 安装任何包）；
- 从 `config.env.example` 生成 `config.env`（若已存在则跳过）；
- 赋予脚本可执行权限；
- 写入 crontab（默认每天 **22:10** 执行 `sync_runner.sh`）；
- 重置本地缓存（首次运行会按配置新建表或对账已有表）。

### 3. 填入凭据
编辑 `server/config.env`，至少填入：
```bash
TAPD_API_USER="你的api_user"
TAPD_API_PASSWORD="你的api_password"
WECOM_CORPID="你的corpid"
WECOM_CORPSECRET="你的secret"
```
> 复用现有 augJOD：`config.env` 已预填 `WECOM_DOCID` / `WECOM_SHEET_ID`（指向 augJOD）。
> 若想改用新表：把这两行清空，首次运行会自动新建一张表并输出 docid。

### 4. 手动试运行（验证凭据 + 建表 / 对账）
```bash
bash /opt/tapd-wecom-sync/server/sync_runner.sh
```
- 复用 augJOD：日志会出现 `[reconcile] 从智能表格读回 361 条记录 …`，然后 `[done] 新增 0 / 更新 0 / 移除 0`（数据已是最新）。
- 新建表：日志会出现 `[init] 已创建智能表格 docid=…`，并写入全部需求。
- 日志见 `logs/sync_*.log`。

---

## 五、文件结构（部署包内）

```
tapd-wecom-sync/
├── scripts/
│   ├── project_oa_config.json   # 44 个项目 + 12 个项目状态映射 + OA/申请人字段覆盖
│   ├── tapd_fetcher.py          # TAPD OpenAPI 拉取（无 MCP）
│   ├── wecom_smartsheet.py      # 企微智能表格 HTTP 客户端（无 wecom-cli，含 get_records）
│   ├── incremental_sync.py      # 增量同步核心（diff + reconcile + 写企微 + 更新缓存）
│   ├── field_discovery.json     # 各项目「需求申请人员」字段映射（已发现，省一次 API 调用）
│   └── status_change_cache.json # 各需求状态变更时间缓存（已拉取，省一次 API 调用）
└── server/
    ├── deploy.sh                # 部署脚本（安装 + 配置 + crontab）
    ├── sync_runner.sh           # 每日编排（拉取 → 同步）
    ├── crontab                  # 容器内 cron 任务定义（Docker 用）
    ├── config.env.example       # 凭据模板（不含机密）
    └── README.md                # 本文件
Dockerfile                      # 容器镜像定义（python:3.13-slim + cron）
docker-compose.yml              # 容器编排（挂载 config.env + 持久化卷）
```

> `wecom_sync_cache.json`（record_id 缓存）与 `config.env`（真实凭据）**不会**进入部署包，首次运行自动生成 / 需手动填入。

---

## 六、调度与运维

- **修改同步时间**：`CRON_HOUR` / `CRON_MIN` 环境变量，例如 `CRON_HOUR=23 CRON_MIN=0 bash deploy.sh`。
- **立即手动同步**：`bash server/sync_runner.sh`。
- **只做 diff 预览不写库**：`WECOM_MOCK=1 python3 scripts/incremental_sync.py <input> --dry-run`（本地校验用，不连企微）。
- **日志**：`logs/sync_*.log`，保留 30 天；cron 输出在 `logs/cron.log`。
- **缓存重置**：删除 `scripts/wecom_sync_cache.json` 后下次运行会重新对账（复用模式）或重新建表（新建模式）。

---

## 七、常见问题

- **errcode 60020 / 没有调用权限**：自建应用未加入目标表的「可调用应用」，或未授予读 + 写。见第三节 2.3。
- **reconcile 报错「请确认自建应用已加入该表且拥有读权限」**：把自建应用加入 augJOD「可调用应用」并勾选「可读取内容」。
- **TAPD 返回 401 / 无数据**：检查 `TAPD_API_USER` / `TAPD_API_PASSWORD` 是否正确、该 API 账号是否有项目访问权限。
- **日期列显示异常**：日期以毫秒时间戳写入；时区偏差可在 `wecom_smartsheet.py` 的 `to_ms_timestamp` 调整。
- **优先级显示为数字而非中文**：TAPD OpenAPI 部分账号 `priority` 返回等级代码；多数账号会返回 `priority_label`，已优先使用。

---

## 八、Docker 容器部署（推荐）

不想在服务器上折腾 python3 / cron 环境，用容器最省事：**构建一次镜像，容器常驻，里面 cron 每天 22:10 自动同步**。凭据通过挂载文件注入，绝不写进镜像。

### 1. 准备凭据文件（在宿主机做，只做一次）
```bash
# 从模板生成真实 config.env（容器会挂载它）
cp server/config.env.example server/config.env
vim server/config.env        # 填 TAPD_API_USER/PASSWORD、WECOM_CORPID/CORPSECRET
```
> 注意：这里的 `server/config.env` 是**宿主机**上的文件，不是镜像里的。容器内那份是占位模板，会被宿主机挂载覆盖。

### 2. 构建并启动（后台常驻）
```bash
docker compose up -d --build
```
镜像内 `cron -f` 前台运行，依据 `server/crontab`（每天 22:10，Asia/Shanghai）触发 `sync_runner.sh`。

### 3. 手动跑一次验证
```bash
docker compose run --rm sync bash /app/server/sync_runner.sh
```

### 4. 看日志
```bash
docker compose logs -f                          # 容器 / cron 输出
docker compose exec sync tail -f /app/data/logs/sync_*.log   # 同步明细
```

### 关键机制说明
- **凭据不进镜像**：`server/config.env` 以只读方式挂载到 `/app/server/config.env`；`sync_runner.sh` 用 `source` 加载（文件内含 `set -a`/`set +a`，变量会导出为环境变量），Python 脚本用 `os.environ.get()` 读取。换服务器只需重新挂载同一个文件。
- **数据持久化**：`/app/data` 用命名卷 `sync_data` 持久化，存放 `wecom_sync_cache.json`（record_id 映射）、中间 JSON、日志。容器重建不会丢失映射、不会重复写入。
- **复用 augJOD**：`config.env` 已预填 `WECOM_DOCID` / `WECOM_SHEET_ID`。首次运行会读回现有 361 行做增量对账，**0 重复**。
- **改同步时间**：编辑 `server/crontab`（`分 时 日 月 周` 五个字段），改完重新 `docker compose up -d --build`。
- **传统服务器（无 Docker）部署**：见第四节，`bash deploy.sh` 即可，机制相同。
