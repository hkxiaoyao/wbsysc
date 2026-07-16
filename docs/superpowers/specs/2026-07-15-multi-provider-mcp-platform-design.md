# 多第三方连接器 MCP 平台设计

## 目标与范围

将当前企微中心的 MCP 网关改造成多连接器平台。企业微信变为 `wecom` 官方连接器；首期同时交付受信任的代码连接器入口与受控 REST/OpenAPI 声明式连接器入口。

已确认：

- 租户代表客户组织，一个租户可有多个连接实例；
- 每个实例使用 `/mcp/{connection_id}` 与独立 MCP Token；
- 代码/插件连接器与声明式连接器汇聚为同一 `ConnectorSpec`；
- 实例独立配置 `direct/stored/hybrid`、工具启停、只读、超时和限流；
- 管理员在后台录入受控凭证类型，平台加密入库；
- 首期迁移企业微信，并交付声明式 OpenAPI/REST 能力；钉钉、飞书后续接入。

## 架构

```text
Tenant
  └─ ConnectionInstance
       ├─ /mcp/{connection_id} + instance token
       ├─ Code Connector: wecom / future packages
       ├─ DeclarativeConnector: REST/OpenAPI config
       └─ Shared Connector Runtime
            ├─ resolver + token validation
            ├─ credential vault
            ├─ policy engine
            ├─ execution planner
            ├─ storage/sync orchestrator
            └─ audit/observability
```

连接器仅实现第三方业务适配。实例鉴权、工具策略、执行模式、缓存、同步、审计和错误治理由共享运行时统一实现。

## 数据模型

保留现有 `tenant_config` 作为组织边界。兼容期内其中的企微字段继续存在，但新运行时不再以其为配置来源。

### `connection_instance`

| 字段 | 说明 |
| --- | --- |
| `connection_id` | 不可预测 UUID/ULID，作为 MCP 路径 |
| `tenant_id` | 所属组织 |
| `connector_key` | `wecom`、`feishu`、`http_declarative` 等 |
| `display_name` | 后台显示名称 |
| `status` | `draft/active/disabled/error` |
| `data_mode` | `direct/stored/hybrid` |
| `public_config_json` | schema 校验后的非敏感配置 |
| `config_version` | 用于 MCP 会话缓存失效 |
| `created_at/updated_at` | 生命周期字段 |

核心索引为 `(tenant_id, status)` 与 `(connector_key, status)`。

### 相关资源

- `connection_credential`：实例、凭证类型、加密值、密钥版本、轮换/过期时间；只在执行时按需解密。
- `connection_token`：实例、Token 前缀、HMAC 摘要、创建/过期/撤销时间；不保存明文，支持双 Token 短暂重叠轮换。
- `connection_tool_policy`：实例、工具、是否启用、只读、超时、限流。
- `connection_sync_state`：实例、资源、游标、同步状态与安全错误摘要。
- `declarative_spec_revision`：版本化 OpenAPI/REST 规范、operation 与字段映射。连接只能引用已发布 revision，修改生成新 revision。

## 统一 ConnectorSpec

```text
ConnectorManifest
  connector_key, version, config_schema, credential_schema,
  tools, supports_sync, supports_data_modes

ToolSpec
  tool_key, mcp_name, input_schema, output_contract,
  operation_kind(read/write), timeout, cache_policy, execute()

ConnectionContext
  tenant_id, connection_id, validated_public_config,
  credential_handle, tool_policy, data_mode, request metadata
```

`tools/list` 仅返回连接实例已启用的工具；`tools/call` 再次校验实例状态、Token、工具策略、限流和只读限制。

代码连接器通过固定插件注册入口发现。首期仅加载随镜像或部署流水线安装、版本锁定且位于允许列表中的包，不接受后台上传任意 Python 包。未来不受信任插件必须进入独立进程/容器沙箱。

## 声明式 REST/OpenAPI 连接器

声明式连接器是受控 `http_declarative` ConnectorSpec，不是脚本执行器。

- 支持 API Key、Basic、OAuth Client Credentials 等受控凭证；授权码 OAuth 后续扩展。
- 仅允许 HTTPS、许可域名和受限重定向；阻断内网/元数据地址和 DNS 重绑定，防止 SSRF。
- 每个 MCP 工具显式绑定一个 OpenAPI operation，默认只读；写操作须由规范与实例策略双重允许。
- 参数仅映射到已声明的路径、查询和请求体字段；不支持 JavaScript、Python、Shell 或任意模板函数。
- 响应只按字段白名单/JSON Pointer 提取，限制响应大小、分页、超时、重试和重定向。
- 导入过程为校验、安全连通性测试、工具预览、发布 revision、实例切换；已发布版本不能原地覆盖。

## MCP 运行时与数据模式

以连接实例会话工厂替换当前全局静态企微工具注册。会话按 `(connection_id, config_version)` 创建/缓存，配置、策略或规范 revision 变化即仅失效该实例。

```text
/mcp/{connection_id}
  → ConnectionResolver
  → instance token + status validation
  → McpSessionFactory
  → ConnectorRegistry + PolicyEngine
  → ExecutionPlanner
  → Connector Executor
  → redacted result + audit log
```

- `direct`：每次直连第三方。
- `stored`：仅查询连接器声明的本地资源。
- `hybrid`：优先本地资源，缺失或过期才回源。

企业微信首期完整支持三种模式。声明式连接器首期支持 `direct` 与受限响应缓存型 `hybrid`；只有提供 `SyncSpec`、资源主键和字段映射时才可启用持久化 `stored`。同步调度统一处理实例锁、游标、限流预算和重试；连接器只实现资源同步逻辑。

## 审计、安全与管理面

`mcp_call_log` 新增 `connection_id`、`connector_key`、`tool_key`。历史企微日志回填默认企微连接；无法识别归属的鉴权失败日志保留空连接实例。连接、Token、凭证、工具策略、规范发布和同步操作都进入管理安全审计。

安全默认：连接 ID 仅路由不授权；Token、凭证、Cookie、Authorization 和原始请求/响应正文不入日志；所有实例查询服务端校验租户归属；出站 HTTP 经过 SSRF、DNS、重定向、超时与大小控制；错误不暴露第三方 Secret、内部地址、schema 或堆栈。

新增管理资源：

```text
GET/POST   /admin/tenants/{tenant_id}/connections
GET/PATCH  /admin/connections/{connection_id}
POST       /admin/connections/{connection_id}/test
GET/POST   /admin/connections/{connection_id}/credentials
GET/POST   /admin/connections/{connection_id}/tokens
GET/PUT    /admin/connections/{connection_id}/tools
GET/POST   /admin/connections/{connection_id}/sync
POST       /admin/declarative-specs/import
GET/POST   /admin/declarative-specs/{spec_id}/revisions
```

租户详情新增连接实例工作台，包含新建、连通性测试、凭证与 Token 轮换、工具策略、数据模式、同步状态和实例日志。声明式连接器提供导入、映射、测试、预览、发布向导。

## 企业微信迁移与分期

1. 创建新表与读取兼容层，不改变现有 `/mcp` 行为。
2. 为每个现有租户创建默认 `wecom` 连接实例，迁移 CorpID、加密 Secret、数据模式，并从现有 Token 写入 HMAC Token 记录。
3. 将现有读写逻辑封装为 `WeComConnector`；现有租户 schema 继续由 WeCom StorageAdapter 使用，首期不搬迁业务数据。
4. 新增 `/mcp/{connection_id}`；旧 `/mcp` 根据 Token 映射到默认企微连接，确保旧客户端不中断。
5. 为日志回填连接维度；新日志全部写连接维度。
6. 建设连接管理后台和声明式 HTTP 连接器。
7. 增加钉钉、飞书等代码连接器；具备明确 `SyncSpec` 后才启用完整存储模式。
8. 经兼容窗口、结果对账与回滚演练后，清除旧企微运行字段与旧路由。

迁移可重复执行、部分失败可恢复，且不删除旧企业微信数据。旧 Token 明文只用于一次性 HMAC 回填，在切换稳定后从旧字段清除。

## 迁移门禁与运维约束

部署必须先备份中心库和全部租户 schema，并登记可恢复的二进制日志位置。随后按顺序执行中心日志迁移、连接平台迁移、应用启动迁移和旧企微回填；所有 SQL 保持 MySQL 5.7 兼容。

旧企微回填使用提交后水位并保持幂等。兼容期保留旧 `/mcp` 和 legacy adapter，只有新旧工具、鉴权、抽样结果与连接日志全部对账通过，才能切换客户端或评审移除旧路径。

若对账失败，停止该租户的新入口切换并让客户端恢复旧 `/mcp`，但保持确定性默认企微连接为 `active`，因为双入口共享连接、Token 和运行时。平台只失效精确 `(connection_id, config_version)` MCP 缓存并保留数据和日志；若旧入口也失败，则回滚整个应用版本。修正后重新对账，不影响其他连接。

连接器包只能通过签名、源码、依赖和 manifest 评审后安装固定版本。部署将归一化入口名写入 `CONNECTOR_ALLOWLIST`，重启后才加载；回滚先禁用依赖连接，再恢复旧镜像与允许列表。

声明式规范发布前必须评审精确 HTTPS 主机、DNS/IP 解析、逐跳重定向、OAuth 2.0 client credentials Token URL、响应大小和字段提取限制。当前声明式分页不受支持，导入相关声明必须失败关闭。监控与审计按 `tenant_id` 和 `connection_id` 双维度查询，且不记录凭证或原始正文。

## 验收标准

- 两个连接实例不能互相调用、读取凭证、缓存或实例日志；不同租户同样隔离。
- 现有企微客户端在兼容期不中断，新旧调用结果可对账。
- Token 轮换/撤销、连接禁用和工具禁用立即生效并失效会话缓存。
- 声明式连接器不能调用未声明 operation、访问未许可/内网域名或执行脚本。
- 写操作须经 ToolSpec 与实例策略双重允许。
- 三种数据模式、同步游标、重试、限流、熔断、日志脱敏与迁移可恢复性均有测试。

## 非目标

首期不支持管理员上传任意代码、不在主进程执行不受信任插件、不自动永久存储任意 OpenAPI 响应、不移除企微旧端点/业务表，也不支持声明式脚本或任意表达式。

## 设计审查记录

按项目流程已发起 Gemini 与 Claude 架构审查。Gemini 环境缺少 `GEMINI_API_KEY`，Claude 本地配置文件缺失，均未产出报告。实施前应在可用环境重新执行双模型审查。
