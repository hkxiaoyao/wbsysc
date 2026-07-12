# 企业微信数据中转 MCP Gateway 架构计划

> 状态：待确认（CONFIRM 阶段） · 作者：浮浮酱 · 日期：2026-07-12
> 遵循 ccg-plan 风格，确认后进入 EXECUTE 阶段。

## 0. 背景与目标

### 0.1 问题陈述
WorkBuddy 的企业微信连接器**只支持 MCP 协议**，且只能创建/删除/写入数据，**不支持读取汇报日志、审批日志、智能表格**。多个客户均有"WorkBuddy 读取企微历史业务数据"的需求。

### 0.2 方案核心
搭建集中式 **MCP Gateway 中转服务**：
- 主动从企微 OpenAPI 拉取（汇报/审批/智能表格）→ 落库
- 以 **MCP Server (Streamable HTTP + Bearer Token)** 形态暴露 tools
- WorkBuddy 客户端通过 HTTPS 远程连接，按租户 Token 鉴权

### 0.3 已锁定的关键边界（主人确认）
| 维度 | 决策 |
|------|------|
| MCP Transport | 生产 HTTP(Streamable)，开发保留 stdio，SSE 仅兼容 |
| MCP 鉴权 | 一期 HTTPS + Bearer Token（每客户独立 Token），二期升级 OAuth 2.1 |
| 租户识别 | 服务端 Token→租户强绑定，**不信任客户端 tenant_id** |
| 数据隔离 | 每客户独立数据库或独立 schema（不软隔离） |
| 客户授权 | 当前未完成 → PoC 仅脱敏/模拟数据，不长期保存原始数据，不接生产库 |
| 同步策略 | 一期定时轮询 + 增量游标 + 断点续传 + 幂等写入 |
| 实时性 | T+1 为主，关键状态 15~30min 轮询，**不追求秒级实时** |
| 数据量基线 | 单客户日增量 ≤ 1 万条 |
| 部署动机 | 便于运维 > 降本 > 统一监控；**非客户强制** |
| MQ | 一期**不引入**消息队列；预留 Webhook/回调接口位（YAGNI） |

### 0.4 不做范围（一期 Out of Scope）
- 会话内容存档（合规门槛过高，若涉及改单租户独立部署）
- 秒级/分钟级实时同步
- OAuth 2.1 完整流程
- 跨客户数据汇总分析
- 客户生产数据库直连

---

## 1. 总体架构

```
 企业微信 OpenAPI (report / approval / 智能表格)
      │  ①Server-Side 拉取（按租户 Worker + 频控退避）
      ▼
 ┌──────────────────────────────────────────────┐
 │  同步层 Sync Layer（容器化，按租户调度）       │
 │   Tenant Scheduler → per-corpid Worker        │
 │   增量游标 + 断点续传 + 幂等写入               │
 │   access_token 内存缓存+自动续期               │
 │   凭证存腾讯云 KMS，按租户加密                 │
 └──────────────────────────────────────────────┘
      │  落库
      ▼
 ┌──────────────────────────────────────────────┐
 │  存储层 Storage（多租户分 schema）            │
 │   推荐：TDSQL-C MySQL                         │
 │   schema: wbd_{corpid_hash} 每客户独立        │
 │   游标表 cursor / 业务表 / 审计日志表          │
 └──────────────────────────────────────────────┘
      │  封装为 MCP tools
      ▼
 ┌──────────────────────────────────────────────┐
 │  MCP 暴露层 MCP Gateway（Streamable HTTP）     │
 │   Bearer Token → 解析租户 → 强约束查询        │
 │   tools: list_reports / get_report            │
 │         list_approvals / get_approval_detail  │
 │         list_smart_table_records              │
 └──────────────────────────────────────────────┘
      ▲  HTTPS + Bearer Token（远程）
      │
   WorkBuddy / CodeBuddy（MCP Client，多客户）
```

---

## 2. 技术选型

| 层 | 选型 | 理由 |
|----|------|------|
| 语言/框架 | **Python 3.11 + FastAPI** | MCP SDK Python 成熟、生态好；FastAPI 异步适合 I/O 密集拉取 |
| MCP SDK | `mcp` 官方 Python SDK | 支持 Streamable HTTP transport |
| 数据库 | **TDSQL-C MySQL（腾讯云）** | 与部署同云、备份运维省心；一期客户少也可单实例多 schema |
| 缓存 | 腾讯云 Redis（access_token 缓存） | token 有效期 2h，避免重复请求 gettoken |
| 凭证存储 | **腾讯云 KMS** 加密 corpid/secret | 红线：禁止硬编码密钥 |
| 容器化 | Docker + **TKE Serverless** 或 CVM | 集中运维、弹性扩容 |
| 调度 | APScheduler（一期）→ 二期可换某租户队列 | KISS，一期不上 XXL-Job |
| 监控 | 腾讯云监控 + 结构化日志（按租户标记）| 统一日志/告警 |
| 对象存储 | 腾讯云 COS（智能表格富文本附件按租户分桶）| 文件按客户独立 Bucket |

> 备选：Node/TypeScript + MCP TS SDK 亦可，若团队后端主力是 TS 则切换。**此项待主人定语言栈。**

---

## 3. 多租户隔离设计（严格标准）

按主人要求，不依赖 `tenant_id` 软过滤，从设计期就硬隔离：

1. **Token 与租户强绑定**：签发 Token 时绑定 `corpid`，服务端解析 Token → 确定 schema，连接层面就绑定目标库 schema，**应用代码不写 `WHERE tenant_id=?`**。
2. **数据库隔离**：每客户独立 schema（`wbd_{corpid_hash}`）；政务/高敏感客户升级为**独立数据库实例**，必要时独立容器/服务器。
3. **凭证隔离**：每客户 corpid/secret 存 KMS 独立主键，互不可读。
4. **同步任务隔离**：每租户独立 Worker + 独立限流参数 + 独立游标，互不阻塞。
5. **文件隔离**：智能表格附件存 COS 按租户独立 Bucket 或独立前缀目录。
6. **审计日志**：每次 MCP 调用记录 `租户/用户/工具/操作对象/参数摘要/结果状态/耗时`，**禁止跨租户查询**（中间件层校验）。
7. **配置隔离**：租户配置（同步频率、白名单模板）独立存储，热更新不互相影响。

---

## 4. MCP 工具（tools）设计

> 工具名统一前缀 `wecom_`，参数严格 schema 化，返回脱敏结构。
> 接口路径已对照企微开发者中心核实（2026-07-12）。

### 4.0 核实后的企微 API 清单（权威依据）

| 数据 | 企微接口 | 方法 | 关键参数 | 频控/限制 |
|------|---------|------|---------|----------|
| 批量取审批单号 | `POST /cgi-bin/oa/getapprovalinfo` | POST | `starttime,endtime,cursor/next_cursor,size≤100,filters` | 600次/分；跨度≤31天 |
| 审批详情 | `POST /cgi-bin/oa/getapprovaldetail` | POST | `sp_no` | 600次/分 |
| 批量取汇报单号 | `POST /cgi-bin/oa/journal/get_record_list` | POST | `starttime,endtime,cursor,limit,filters(creator/department/template_id)` | 跨度≤1月 |
| 汇报详情 | `POST /cgi-bin/oa/journal/get_record_detail` | POST | 仅 `journaluuid`（来自上一步） | — |
| 智能表格查询记录 | `POST /cgi-bin/wedoc/smartsheet/get_records` | POST | `docid,sheet_id,offset,limit≤1000,key_type,record_ids,sort,filter_spec` | - |
| access_token | `GET /cgi-bin/gettoken?corpid=&corpsecret=` | GET | - | 2h 有效 |

> 关键发现：企微自家的智能表格 MCP（path/101468）**只支持新建/写入，不支持读取记录** —— 这正是 workbuddy 连接器缺的能力，本中转服务用 `get_records` 补齐读取短板 ✓

### 4.1 汇报类
| tool | 参数 | 返回 | 对应企微 API |
|------|------|------|-------------|
| `wecom_list_reports` | `template_id?`, `start_time`, `end_time`, `cursor?`, `limit?` | report uuid 列表 + next_cursor | `oa/journal/get_record_list` |
| `wecom_get_report` | `journaluuid` | report 详情 | `oa/journal/get_record_detail` |

> 注：汇报需先 `get_record_list` 取 journaluuid 列表，再逐条 `get_record_detail` 取详情。模板 ID 在管理后台-汇报应用-内容设置页获取（无独立 list templates 接口，移除原 `list_report_templates`）。

### 4.2 审批类
| tool | 参数 | 返回 | 对应企微 API |
|------|------|------|-------------|
| `wecom_list_approvals` | `filters?`, `start_time`, `end_time`, `cursor?`, `size?` | sp_no 列表 + next_cursor | `oa/getapprovalinfo` |
| `wecom_get_approval_detail` | `sp_no` | 审批详情 | `oa/getapprovaldetail` |

> 审批 `getapprovalinfo` 用 `new_cursor`/`new_next_cursor` 游标（注意字段名是 new_cursor，非 cursor）。

### 4.3 智能表格类
| tool | 参数 | 返回 | 对应企微 API |
|------|------|------|-------------|
| `wecom_list_smart_table_records` | `docid`, `sheet_id`, `offset?`, `limit?≤1000`, `key_type?` | records + has_more + next | `wedoc/smartsheet/get_records` |

### 4.4 鉴权与限制
- 必传 `Authorization: Bearer <token>`，服务端解析→绑定租户 schema
- 每工具按租户限流（令牌桶，全局不超企微 600次/分）
- 分页上限：审批/汇报 size≤100，智能表格 limit≤1000
- 时间跨度：审批≤31天，汇报≤1月（超长跨度内部分段）

---

## 5. 同步层设计

### 5.1 拉取策略
```
定时任务（APScheduler，按租户独立 cron）
  → 取租户配置（模板白名单、频率）
  → 取游标（last_update_time 或 last_id）
  → 调企微 API（批量，size=100，频控退避）
  → 幂等写入（主键: corpid+业务流水号，UPSERT）
  → 推进游标（独立事务）
  → 失败重试（指数退避，最多 3 次）+ 死信记录
```

### 5.2 增量依据（优先级）
1. `update_time`（更新时间，最稳）
2. 自增主键 / 业务流水号
3. 数据版本号（若有）

### 5.3 游标表（每租户 schema 内）
```sql
CREATE TABLE sync_cursor (
  data_source   VARCHAR(32),   -- report / approval / smart_table
  template_id   VARCHAR(64),
  last_value    VARCHAR(128),  -- 游标值（时间戳或 id）
  last_sync_at  DATETIME,
  PRIMARY KEY (data_source, template_id)
);
```

### 5.4 频控与退避
- 企微全局限频，按租户 worker 串行 + 跨租户全局令牌桶
- 429/45009 → 指数退避 + 告警

---

## 6. 部署架构（腾讯云集中）

```
一台（一组）集中服务器 / TKE Serverless 集群
  ├─ MCP Gateway 服务（Streamable HTTP，公网 HTTPS + WAF）
  ├─ Sync Worker（同进程/独立进程，按租户调度）
  ├─ TDSQL-C MySQL（多 schema）
  ├─ Redis（token 缓存）
  └─ KMS（凭证）
外网入口：腾讯云 SSL 证书 + WAF + 限流
监控：腾讯云监控 + CLS 日志（按租户 tag）
```

> "一台"指逻辑集中，物理上用容器组而非单进程裸跑，避免单点。

---

## 7. 分阶段任务（WBS）

### 阶段一：PoC（单租户，脱敏数据，2~3 周）
- [ ] **1.1** 搭建项目骨架（Python 3.11 + FastAPI + 官方 `mcp` SDK，Streamable HTTP）
- [ ] **1.2** 实现企微 access_token 获取与缓存（2h 有效，Redis 或内存）
- [ ] **1.3** 实现审批同步：`oa/getapprovalinfo`（游标 new_cursor，跨度≤31天分段）→ `oa/getapprovaldetail` 落库
- [ ] **1.4** 实现汇报同步：`oa/journal/get_record_list`（跨度≤1月分段）→ `oa/journal/get_record_detail` 落库
- [ ] **1.5** 实现智能表格同步：`wedoc/smartsheet/get_records`（offset 分页，limit≤1000）落库
- [ ] **1.6** 单 schema 落库 + 增量游标表 + 幂等 UPSERT
- [ ] **1.7** 实现 5 个 MCP tools（wecom_list_reports/get_report/list_approvals/get_approval_detail/list_smart_table_records）
- [ ] **1.8** Bearer Token 鉴权中间件（一期 token→租户映射）
- [ ] **1.9** WorkBuddy 远程连接 + 调每个 tool 验证（脱敏数据）
- [ ] **1.10** 结构化日志（租户/工具/对象/耗时）

### 阶段二：多租户隔离（2~3 周）
- [ ] **2.1** Token 签发/校验服务 + Token→租户强绑定
- [ ] **2.2** 动态 schema 路由（连接级绑定 schema）
- [ ] **2.3** KMS 凭证加密存储 + 读取
- [ ] **2.4** 租户配置表 + 独立 Worker 调度
- [ ] **2.5** 跨租户查询中间件级校验（禁止跨租户）
- [ ] **2.6** 审计日志完善

### 阶段三：生产化（2 周，授权完成后）
- [ ] **3.1** 腾讯云部署（TKE Serverless + TDSQL-C + Redis + KMS + COS）
- [ ] **3.2** WAF + HTTPS + 限流
- [ ] **3.3** 监控 + 告警 + 死信处理
- [ ] **3.4** 智能表格 tools 实现 + COS 附件分桶
- [ ] **3.5** 频控退避 + 限流参数化
- [ ] **3.6** 客户授权流程清单 + 数据删除/项目终止处理预案

### 阶段四：演进（按需）
- [ ] **4.1** OAuth 2.1（员工个人身份 + Token 自动续期）
- [ ] **4.2** Webhook/回调订阅（实时性需求起来后）—— 预留接口位
- [ ] **4.3** MQ 引入（待日增量超 1 万且有实时需求）—— 预留接口位
- [ ] **4.4** 独立部署模式（政务/高敏感客户）

---

## 8. 合规与安全红线（不可越）

1. **授权前**：PoC 仅用模拟/脱敏数据，不长期保存原始数据，不接客户生产库。
2. **授权要求**：合同/数据处理协议明确 → 数据范围/用途/保存期限/部署位置/加密备份/运维访问权限/删除与终止处理/责任边界。
3. **禁止硬编码密钥**：corpid/secret 一律 KMS。
4. **禁止跨租户**：中间件级校验，发现越权立即告警。
5. **会话内容存档**：一期不涉及；若涉及改独立部署。
6. **数据出境**：服务器部署境内（腾讯云），不出境。

---

## 9. 待主人确认/决策项（CONFIRM）

1. ~~后端语言栈~~ → **已定：Python 3.11 + FastAPI + 官方 `mcp` SDK**
2. ~~企微 API 核实~~ → **已完成**，见 4.0 节核实清单（主文档 path/90664 为审批入口）
3. **数据库**：TDSQL-C MySQL 一期可单实例多 schema 起步，可接受？
4. **部署形态**：TKE Serverless（弹性）还是 CVM+Docker（固定）？
5. **PoC 数据来源**：主人能否提供一份脱敏/模拟的汇报+审批+智能表格样本数据？或浮浮酱用 mock 数据起步？
6. **MCP SDK 版本锁定**：动工时浮浮酱按 `mcp` 官方 Python SDK 当前稳定版锁版本。
7. **腾讯云资源开通**：PoC 阶段是否先本机开发（不买云资源），阶段三生产化再开腾讯云？

---

## 10. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 企微限频导致同步积压 | 跨租户令牌桶 + 退避 + 告警 |
| 跨租户数据串 | 连接级 schema 绑定，中间件校验，禁软隔离 |
| 会话存档误纳入 | 边界明确，涉及即独立部署 |
| 客户未授权强推上线 | 阶段三生产化必须挂在授权完成后 |
| MCP 协议版本演进 | 用官方 SDK，跟踪 Streamable HTTP 规范 |
| Token 泄露 | HTTPS + 短期有效 + 可吊销 + 审计 |