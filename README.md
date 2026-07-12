# 企微数据中转 MCP Gateway

将企业微信（审批/汇报/打卡）数据**读取**能力，通过 MCP 协议暴露给 WorkBuddy/CodeBuddy，解决 WorkBuddy 连接器只支持"新建/写入"不支持"读取"的问题。

> **生产就绪（阶段三）**：Docker + Nginx/HTTPS + systemd 多租户部署。详见 `docs/部署指南.md`。

## 架构

```
企微 OpenAPI → 同步层(按租户游标) → MySQL(分schema) → MCP Gateway(Streamable HTTP)
                                                        ▲ Bearer Token(每租户独立)
                                                        │
                                            WorkBuddy / CodeBuddy
```

- 生产 transport：HTTP (Streamable HTTP)，挂载于 `/mcp`
- 鉴权：Bearer Token → 租户强绑定（不信任客户端 tenant_id）
- 一期定时轮询同步，预留 Webhook 接口位，不引 MQ

## 快速开始（PoC）

### 1. 安装依赖
```bash
python -m venv .venv && .venv/Scripts/activate     # Windows
pip install -r requirements.txt
```

### 2. 配置环境
```bash
cp .env.example .env
# 至少设置 MCP_TOKENS=your-token:tenant1
# PoC 默认 WECOM_USE_MOCK=true，用脱敏 mock 数据
```

### 3. 启动服务
```bash
python -m app.main
# 或 uvicorn app.main:app --reload
```
健康检查：`GET http://localhost:8000/health`

### 4. WorkBuddy 远程连接配置
```json
{
  "mcpServers": {
    "wecom-gateway": {
      "type": "http",
      "url": "https://<your-server>/mcp",
      "headers": {
        "Authorization": "Bearer ${WORKBUDDY_MCP_TOKEN}"
      }
    }
  }
}
```

> **代理坑点（生产必读）**：httpx 在 Windows 会读系统级代理设置，localhost/内网地址可能被错误代理导致 502。WorkBuddy 部署机器若存在系统代理，需将中转服务地址加入 `NO_PROXY`（如 `NO_PROXY=mcp.example.com`）。

### 5. 冒烟测试
```bash
# 终端1：启动服务（设 token，开 mock）
MCP_TOKENS="test-token:tenant1" WECOM_USE_MOCK=true python -m app.main

# 终端2：客户端真实协议调用
python tests/test_smoke_client.py
```
预期：鉴权拒绝 PASS + 工具调用 PASS（5 个工具均返回 mock 数据）。

### 6. 真实模式（已验证端到端打通）
`.env` 填入真实 `WECOM_CORPID/SECRET`、`DB_*`，设 `WECOM_USE_MOCK=false` 启动：
- 服务启动会**立即首次同步** + 之后按 `SYNC_INTERVAL_*_MIN` 间隔**周期同步**
- 同步逻辑：游标驱动增量（首次回填30天，后续从游标续传），幂等落库
- workbuddy 调 tools **从数据库读取**（读库<100ms，不走企微 API）

> 企微接入前置：IP白名单(60020)、审批权限(301055) 等见 `docs/企微接入配置清单.md`。

## 管理后台（React + Ant Design）

Web 管理界面：租户 CRUD、模块开关、手动同步。访问 `http://<server>:8001/admin/ui/`

**登录**：单密码（`.env` 的 `ADMIN_PASSWORD`），session token 鉴权（Cookie + Bearer 双支持）。

### 后端 API（独立路由，不经 MCP 鉴权）
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/login` | 密码登录 → session token |
| POST | `/admin/logout` | 登出 |
| GET | `/admin/session` | 校验登录态 |
| GET | `/admin/tenants` | 列出所有租户（secret 不回传明文） |
| POST | `/admin/tenants` | 新增租户（自动建 schema） |
| PUT | `/admin/tenants/{id}` | 编辑租户（留空 secret=不改） |
| DELETE | `/admin/tenants/{id}` | 删除租户配置 |
| POST | `/admin/tenants/{id}/sync` | 手动触发该租户同步 |

### 前端开发与构建
```bash
cd admin-ui
pnpm install      # 首次
pnpm dev          # 开发 :5178，跨域代理到后端 :8001
pnpm build        # 构建到 app/static/dist，FastAPI 自动托管
```

### 安全
- 密码单密码（.env），编辑租户时密钥留空=不改、不回传明文
- session HttpOnly Cookie + Bearer 双支持，生产走 HTTPS secure flag
- 管理接口不经 MCP Bearer Token（独立鉴权域）

## 多租户架构（阶段二已完成）

**A2 物理 schema 隔离** + DB AES 凭证加密：

```
workbuddy(token) → BearerTokenMiddleware
   → tenant_config(中心库, secret AES加密) 查 token
   → 命中租户 (corpid + schema_name)
   → 绑定 schema=wbd_{corpid_hash}
   → MCP tool / 审计 全走该 schema（物理隔离）
```

### 接入新租户
```bash
python -m app.tenant_init \
  --tenant-id customerA \
  --corpid wwXXXXXXXX \
  --secret XXXXX \
  --token $(openssl rand -hex 24) \
  --display "客户A" \
  --interval 30 \
  --modules report,approval,checkin \
  --checkin-userids "userA,userB"
```
执行后自动：①写 tenant_config(secret加密) ②建 `wbd_{md5hash}` schema ③建5张业务表 ④刷新缓存。

**模块开关** `--modules`：逗号分隔，可选 `report`/`approval`/`checkin`，按需组合（如只想接审批：`--modules approval`）。
**打卡 useridlist**（二选一，推荐自动）：
- 自动：`--contact-secret XXXX`（通讯录同步secret，自动调 `user/list_id` 拉全企业userid，10分钟缓存）
- 手动：`--checkin-userids "userA,userB"`（人少/精确控制；自动拉失败时也回退此配置）

**前置权限**：DB_USER 需 `CREATE SCHEMA` 权限（建租户schema用）。

### 隔离保证
- token→租户→schema 服务端强绑定，**不信任客户端 tenant_id**
- 每租户 SQL 表名带 schema 前缀（避免连接池 USE 并发竞态）
- 凭证 AES-Fernet 加密存 DB（主密钥 `.env` `CREDENTIAL_KEY`，生产改 KMS）
- 调度器遍历每租户独立同步任务，独立游标
- 审计日志按租户 schema 物理分离

## 同步任务（APScheduler）
- 启动时立即跑一次首次同步 + 周期按间隔跑（遍历所有启用租户）
- 增量游标存于各租户 `wbd_xxx.sync_cursor`，断点续传
- 同步在独立线程池执行，不阻塞 MCP 事件循环
- 边界：单租户/单条详情失败不中断整体（记错继续）；`MAX_DETAIL_PER_RUN=500` 防爆

## MCP 工具（5 个）

| 工具 | 说明 | 对应企微 API |
|------|------|----|
| `wecom_list_reports` | 汇报单号列表 | `oa/journal/get_record_list` |
| `wecom_get_report` | 汇报详情 | `oa/journal/get_record_detail` |
| `wecom_list_approvals` | 审批单号列表 | `oa/getapprovalinfo` |
| `wecom_get_approval_detail` | 审批详情 | `oa/getapprovaldetail` |
| `wecom_list_smart_table_records` | 智能表格记录 | `wedoc/smartsheet/get_records` |

## 安全红线
- 凭证（corpid/secret/DB 密码）一律走 `.env`，禁止硬编码
- `.env` 在 `.gitignore` 中，不提交
- 客户授权完成前：仅 mock/脱敏数据，不长期保存客户原始数据

## 企微接入前置条件（真实模式联调记录 2026-07-12）

切 `WECOM_USE_MOCK=false` 后真实调用，需要先在企微管理后台完成：

| 接口类 | 需配置 | 错误码对照 |
|--------|--------|-----------|
| access_token | 不需额外配置 | 凭证正确即返回 |
| 审批 / 汇报 | **企业可信IP白名单** → 加入调用机公网IP | 返回 60020 = IP未加白 |
| 智能表格 | 应用需开**文档/智能表格权限** + 真实 docid | 返回 48002 = 权限未开 |

**可信IP配置路径**：企微管理后台 → 应用管理 → 对应自建应用 → "企业可信IP" → 添加（开发机IP / 中转服务器IP）。

> 已验证：corpid + secret 有效，access_token 获取成功，HTTP/鉴权/MCP链路全通。`60020`/`48002` 均属后台配置准入，非代码问题。

## 文档
- 完整架构计划：[`docs/PLAN-wecom-mcp-gateway.md`](docs/PLAN-wecom-mcp-gateway.md)
- 企微真实 API 清单见计划 4.0 节