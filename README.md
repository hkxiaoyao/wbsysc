# 多连接 MCP Gateway

将企业微信和受控第三方连接器的数据能力通过模型上下文协议（MCP）暴露给 WorkBuddy/CodeBuddy。每个连接使用独立的 `/mcp/{connection_id}`、Token、工具策略、凭证、缓存和审计边界。

> 解决痛点：WorkBuddy 的企业微信连接器**只支持 MCP**，且只能"新建/写入"，**不支持读取**历史业务数据。本服务作为数据中转层，主动从企微 OpenAPI 拉取落库，再以 MCP Server 形态暴露给 WorkBuddy 读取。

## ✨ 功能特性

- **MCP Gateway**（Streamable HTTP + Bearer Token，远程多客户连接）
- **连接级隔离**：一个租户可创建多个连接，每个连接独立鉴权、策略、缓存和日志
- **兼容迁移**：旧 `/mcp` 在兼容期映射到默认企微连接，新客户端使用 `/mcp/{connection_id}`
- **三类数据读取**：审批、汇报、打卡（智能表格一期搁置：见下方说明）
- **多租户物理隔离**：每客户独立 MySQL schema（`wbd_{corpid_hash}`），凭证 AES 加密
- **租户级功能开关**：每客户可选 `report`/`approval`/`checkin` 模块组合
- **打卡自动拉通讯录**：配通讯录同步 secret → 自动调 `user/list_id` 拉全员 userid
- **增量同步**：游标驱动 + 断点续传 + 幂等 UPSERT + APScheduler 定时
- **管理后台**：React + Ant Design，租户 CRUD、模块开关、手动同步、单密码登录
- **CI/CD**：GitHub Actions 自动构建镜像推 GHCR，服务器 `docker pull` 免本机编译
- **生产就绪**：Dockerfile + Nginx/HTTPS + systemd + 健康检查 + 日志轮转

## 🏗 架构

```
┌─ 企微 OpenAPI ──────────────────────────────────────────┐
│   审批 oa/getapprovalinfo+getapprovaldetail              │
│   汇报 oa/journal/get_record_list+get_record_detail     │
│   打卡 checkin/getcheckindata                            │
│   通讯录 user/list_id                                    │
└──────────────────────────────────────────────────────────┘
        │  ① 同步层（APScheduler 按租户调度，游标增量）
        ▼
┌─ MySQL（多租户分 schema 物理隔离）────────────────────────┐
│  中心库 websysc: tenant_config (secret AES加密)          │
│  各租户 wbd_{hash}: wecom_report/approval/checkin +     │
│                     sync_cursor + audit_log              │
└──────────────────────────────────────────────────────────┘
        │  ② MCP Gateway 暴露层（Streamable HTTP）
        ▼
   WorkBuddy / CodeBuddy
   (Bearer Token → 租户强绑定 → 读该租户 schema)
        ▲  ③ 管理后台（独立 session 鉴权）
        │
   浏览器 http://server/admin/ui/  (React+AntD)
```

- **生产 transport**：HTTP (Streamable HTTP)，路径 `/mcp`
- **鉴权**：MCP 用 Bearer Token（每租户独立，token→租户强绑定，不信任客户端 tenant_id）；管理后台用单密码 session
- **同步策略**：一期定时轮询（增量游标），预留 Webhook 位，不引 MQ

### 数据读取模式

每个租户可选择 `stored` 或 `direct`。`stored` 定时把业务数据同步到租户 MySQL schema，MCP 查询本地表。`direct` 在每次 MCP 调用时请求企微 API，不写入审批、汇报或打卡业务表，也不参加后台同步。

两种模式都在 MySQL 保存租户配置、加密凭证、MCP Token 和审计日志。直连请求失败时会返回企微错误，不读取历史缓存。现有租户升级后保持 `stored`。

直连模式查询大时间窗口时，会从最新时间分段开始遍历企微列表分页，并为返回的单号逐条请求详情，API 调用成本较高。生产调用建议缩小时间窗口并设置较小的 `limit`。

## 🚀 快速开始

### 方式一：Docker（生产推荐，用 CI 构建的镜像）

```bash
git clone https://github.com/hkxiaoyao/wbsysc.git && cd wbsysc
cp .env.prod.example .env && vim .env    # 填写密码和三个独立密钥；首次保持 MCP_SERVICE_ENABLED=false

docker pull ghcr.io/hkxiaoyao/wbsysc:latest
docker compose up -d
curl http://localhost:8001/health        # 同时核对 mcp_service_enabled 布尔值

# 接入第一个租户
docker compose exec wbsysc python -m app.tenant_init \
  --tenant-id tenant_id_here --corpid corpid_here --secret app_secret_here \
  --token $(openssl rand -hex 24) --contact-secret contact_secret_here \
  --modules report,approval,checkin --display "测试客户1"
```

### 生产升级（先迁移再切换）

推荐执行 `bash deploy/server_deploy.sh`：脚本先校验三个生产密钥，再用独立迁移账户和宿主 `mysql` CLI 严格执行 `004` → `005` → `006` → `007` → `008`；任一迁移失败都会在拉取/启动前终止。随后脚本强制以 `MCP_SERVICE_ENABLED=false` 重建并验证健康，仅在原请求值为 `true` 时二次重建启用。启用检查失败会恢复 `false`、重建并验证关闭态后非零退出，且保留 `008` 数据。

```bash
git pull
read -rp "DB_MIGRATION_USER: " DB_MIGRATION_USER && export DB_MIGRATION_USER
read -rsp "DB_MIGRATION_PASSWORD: " DB_MIGRATION_PASSWORD && export DB_MIGRATION_PASSWORD && echo
bash deploy/server_deploy.sh
```

需要手动升级时，顺序必须是“备份数据库 → `004` → `005` → `006` → `007` → `008` → 关闭态重建/健康检查 → 经批准启用并再次重建”。`008` 依赖 `005` 与 `006`。密码通过 `MYSQL_PWD` 环境变量传递：

```bash
DB_MIGRATION_HOST=127.0.0.1
DB_PORT=3306
DB_MIGRATION_USER=wbsysc_migrator
DB_NAME=websysc
read -rsp "DB_MIGRATION_PASSWORD: " MYSQL_PWD && export MYSQL_PWD && echo
mysql --protocol=TCP --host="$DB_MIGRATION_HOST" --port="$DB_PORT" \
  --user="$DB_MIGRATION_USER" "$DB_NAME" < sql/004_gateway_hardening.sql
mysql --protocol=TCP --host="$DB_MIGRATION_HOST" --port="$DB_PORT" \
  --user="$DB_MIGRATION_USER" "$DB_NAME" < sql/005_mcp_call_log.sql
mysql --protocol=TCP --host="$DB_MIGRATION_HOST" --port="$DB_PORT" \
  --user="$DB_MIGRATION_USER" "$DB_NAME" < sql/006_connection_platform.sql
mysql --protocol=TCP --host="$DB_MIGRATION_HOST" --port="$DB_PORT" \
  --user="$DB_MIGRATION_USER" "$DB_NAME" < sql/007_tenant_auth.sql
mysql --protocol=TCP --host="$DB_MIGRATION_HOST" --port="$DB_PORT" \
  --user="$DB_MIGRATION_USER" "$DB_NAME" < sql/008_mcp_service.sql
unset MYSQL_PWD
docker pull ghcr.io/hkxiaoyao/wbsysc:latest
# 先写 MCP_SERVICE_ENABLED=false，再 docker compose up -d --force-recreate 并核对 health
# 仅经批准后改 true，再次 --force-recreate 并核对 health
```

> `004_gateway_hardening.sql` 包含 `DELIMITER` 和存储过程语句，必须使用 MySQL `mysql` CLI 执行。`006_connection_platform.sql` 会幂等地将旧库的声明式文档列从 `TEXT` 扩容为 `MEDIUMTEXT`，以支持运行时允许的 256 KiB 文档。任一迁移失败时都不要启动新版本。

### 方式二：本地开发

```bash
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -r requirements.txt
cp .env.example .env                                 # 默认 mock 模式
python -m app.main                                   # 启动 (http://localhost:8001)

# 另开终端：MCP 客户端真实协议冒烟测试
python tests/test_smoke_client.py
```

## 📋 配置（.env）

| 变量 | 说明 | 必填 |
|------|------|------|
| `DB_HOST` | 同台部署填 `host.docker.internal`；跨机填 MySQL IP | ✓ |
| `DB_PASSWORD` | MySQL `websysc` 账户密码 | ✓ |
| `ADMIN_PASSWORD` | 管理后台登录密码 | ✓ |
| `CREDENTIAL_KEY` | 凭证加密主密钥（开发可留空；生产必配强随机） | 生产必填 |
| `MCP_TOKEN_HMAC_KEY` | MCP Token HMAC 密钥，至少 32 个 UTF-8 字节且与 `CREDENTIAL_KEY` 独立 | 生产必填 |
| `MCP_TOKEN_PLAINTEXT_KEY` | 未撤销服务 Token 密文密钥，至少 32 个 UTF-8 字节且与另两个密钥独立 | 生产必填 |
| `MCP_SERVICE_ENABLED` | 服务路由/租户服务自助开关；首次发布为 `false` | 生产必填 |
| `CONNECTOR_ALLOWLIST` | 已审核 `wbsysc.connectors` 入口名的归一化精确列表 | - |
| `WECOM_USE_MOCK` | `true`=脱敏 mock；生产必须为 `false` 并配置租户凭证 | 生产必填 |
| `SYNC_INTERVAL_*_MIN` | 同步间隔（report/approval/smarttable） | - |

> 租户企微凭证（corpid/secret）**不进 .env**，通过管理后台或 `tenant_init` 写入 `tenant_config`（AES 加密）。
>
> 生产启动必须同时设置 `WECOM_USE_MOCK=false`、三个两两不同的密钥、`ADMIN_PASSWORD` 和 `DB_PASSWORD`；缺失、密钥少于 32 个 UTF-8 字节或使用示例值时应用会拒绝启动。

连接 Token 只在签发时显示一次且不可揭示；未撤销服务 Token 仅当前平台管理员或所属租户可通过限流、审计且 `no-store` 的端点揭示。轮换 `CREDENTIAL_KEY` 前重加密凭证；当前 HMAC 仅支持单 key，须先盘点旧 token ID，在维护窗口切 key/重启后再用新 key 签发、分发、验证并核对旧 ID 全部失效（旧 key 下预签发不能保持可用）；轮换 plaintext 密钥前重加密全部未撤销服务 Token 密文。完整步骤见 [`docs/connection-platform-operations.md`](docs/connection-platform-operations.md)。

## 🔧 MCP 工具（6 个）

| 工具 | 说明 | 对应企微 API |
|------|------|----|
| `wecom_list_reports` | 汇报单号列表 | `oa/journal/get_record_list` |
| `wecom_get_report` | 汇报详情 | `oa/journal/get_record_detail` |
| `wecom_list_approvals` | 审批单号列表 | `oa/getapprovalinfo` |
| `wecom_get_approval_detail` | 审批详情 | `oa/getapprovaldetail` |
| `wecom_list_checkins` | 打卡记录 | `checkin/getcheckindata` |
| `wecom_list_smart_table_records` | 智能表格记录（一期搁置）| `wedoc/smartsheet/get_records` |

### WorkBuddy 连接配置
```json
{
  "mcpServers": {
    "wecom-gateway": {
      "type": "http",
      "url": "https://mcp_host_name/mcp/connection_id_here",
      "headers": { "Authorization": "Bearer ${WORKBUDDY_MCP_TOKEN}" }
    }
  }
}
```

> **代理坑点（必读）**：httpx 在 Windows 会读系统级代理，localhost/内网可能被错误代理致 502。WorkBuddy 机器若有系统代理，需 `NO_PROXY=mcp.example.com`。

## 🎛 管理后台

访问 `http://server_host_name:8001/admin/ui/`，单密码登录（`.env` 的 `ADMIN_PASSWORD`）。

| API | 说明 |
|-----|------|
| `POST /admin/login` | 密码登录 → session token（Cookie + Bearer 双支持） |
| `GET /admin/tenants` | 列出租户（secret 不回传明文） |
| `POST /admin/tenants` | 新增租户（自动建 schema） |
| `PUT /admin/tenants/{id}` | 编辑租户（密钥留空=不改） |
| `DELETE /admin/tenants/{id}` | 删除租户配置 |
| `POST /admin/tenants/{id}/sync` | 手动触发该租户同步 |

创建租户时可选设置初始密码；管理员可重置密码或启用/禁用登录，但后台从不读取、回填既有密码。重置密码、租户自行改密或禁用登录都会撤销该租户现有会话。服务功能仅在 `MCP_SERVICE_ENABLED=true` 的重启后开放；此时可信连接器注册完成后会幂等回填默认服务和工具绑定，不复制连接 Token。改回 `false` 并重建会关闭服务路由、租户服务 UI/API 和服务运行时，同时保留旧 MCP 入口、管理员清理能力及 `008` 数据。

前端开发：`cd admin-ui && pnpm install && pnpm dev`（:5178 跨域代理后端）；`pnpm build` 产出 `app/static/dist`。

## 🏢 多租户

**物理 schema 隔离**：每租户独立 `wbd_{corpid_md5}` schema，凭证 AES-Fernet 加密存中心库。

```bash
# 接入新租户（自动建schema+5张表+刷缓存）
python -m app.tenant_init \
  --tenant-id tenant_id_here --corpid corpid_here --secret app_secret_here \
  --token $(openssl rand -hex 24) \
  --modules report,approval,checkin \
  --contact-secret contact_secret_here # 可选：通讯录同步 secret
  --checkin-userids "userid_here"      # 可选：手动配置 userid
```

**模块开关** `--modules`：可选 `report`/`approval`/`checkin` 任意组合。

**打卡 userid**（二选一）：自动优先（`--contact-secret` 调 list_id 拉全员，10分钟缓存），失败回退手动配置。

**隔离保证**：token→租户→schema 服务端强绑定，SQL 带 schema 前缀防连接池竞态，审计日志按租户物理分离。

## 🔄 同步任务

- APScheduler 启动立即首同步 + 周期遍历所有启用租户
- 增量游标存各租户 `sync_cursor`，断点续传
- 线程池执行不阻塞 MCP 事件循环
- 单租户/单条失败不中断整体；`MAX_DETAIL_PER_RUN=500` 防爆
- 打卡逐人拉取 + 容错：越权人员(301021)静默跳过不影响他人

## 📦 CI/CD

push 到 `main` 自动触发 GitHub Actions（`.github/workflows/build-push.yml`）：
1. 构建多阶段镜像（node 构前端 → python 运行）
2. 推到 `ghcr.io/hkxiaoyao/wbsysc:latest`（按 commit sha + latest 双标签）
3. 服务器 `docker pull` 即可，无需本机 Node/Python 编译

GHCR 私有 package 访问：公开镜像（GitHub package 页 → Package settings → Public，匿名拉取）或服务器 `docker login ghcr.io`。

## 🔐 企微接入前置（真实模式必看）

切 `WECOM_USE_MOCK=false` 前需在企微管理后台配置（详见 `docs/企微接入配置清单.md`）：

| 接口类 | 需配置 | 错误码对照 |
|--------|--------|-----------|
| 全部 | **企业可信IP白名单**（加服务器公网IP）| `60020` = IP未加白 |
| 审批/汇报 | 审批/汇报应用 → API → 可调用接口的应用 加自建应用 | `301055` = 未授权 |
| 打卡 | 同上 + 应用可见范围含目标员工 | `301021` = 人员不在可见范围 |
| 通讯录 | 通讯录同步 secret（独立于自建应用 secret）| `40001` = secret错误 |
| 智能表格 | 应用开文档/智能表格权限 + 真实 docid | `48002` = 权限未开 |

⚠️ **智能表格读取一期搁置**：企微 docid 仅通过 API 创建文档可得，成员手工存量表无法获取 docid，故读取受限。

## 🛡 安全红线

- 凭证（corpid/secret/DB密码/ADMIN_PASSWORD）**禁止硬编码**，全走 `.env` 或 AES 加密入 DB
- `.env` 在 `.gitignore`，永不提交（仓库内只有 `.env.example`/`.env.prod.example` 模板）
- 客户授权完成前：仅 mock/脱敏数据，不长期保存客户原始数据，不接生产库
- 上线必做：DB 强密码 / `CREDENTIAL_KEY` 强随机 / 企微 secret 重签 / `ADMIN_PASSWORD` 改强

## 📁 项目结构

```
app/
  config.py / auth.py / db.py / main.py        # 配置/鉴权/数据层/入口
  admin.py                                       # 管理后台 API
  crypto.py / tenant.py / tenant_init.py        # 凭证加解密/租户查询/接入脚本
  mcp_server.py                                  # 6 个 MCP 工具
  wecom/
    client.py                                    # 企微API客户端(token缓存隔离双secret)
    sync.py / approval_sync.py / checkin_sync.py # 三类同步(分段+游标+去重)
    contact.py                                   # 通讯录userid自动拉
    dispatch.py                                  # 多租户调度遍历
    mock.py                                      # 脱敏mock数据
admin-ui/                                        # React+Vite+Ant Design 管理前端
sql/                                             # 建表脚本
deploy/                                          # nginx.conf / server_deploy.sh / wbsysc.service
.github/workflows/                               # CI 构建镜像
docs/                                            # 架构计划/企微接入清单/部署指南
tests/test_smoke_client.py                       # MCP 客户端冒烟测试
```

## 📚 文档

- 多连接平台运维：[`docs/connection-platform-operations.md`](docs/connection-platform-operations.md)
- 完整架构计划：[`docs/PLAN-wecom-mcp-gateway.md`](docs/PLAN-wecom-mcp-gateway.md)
- 企微接入配置清单：[`docs/企微接入配置清单.md`](docs/企微接入配置清单.md)
- 部署指南：[`docs/部署指南.md`](docs/部署指南.md)

## 🔧 技术栈

- **后端**：Python 3.11 + FastAPI + 官方 MCP Python SDK + SQLAlchemy + APScheduler + httpx
- **前端**：React 18 + Vite + Ant Design 5
- **存储**：MySQL 5.7+（多租户分 schema）+ 可选 Redis（token缓存）
- **部署**：Docker + docker-compose + Nginx/HTTPS + systemd
- **CI**：GitHub Actions → GHCR
