# 多 MCP 服务入口与声明式工具编排设计

## 1. 背景与目标

一期已经将企业微信改造成 `wecom` 代码连接器，并建立租户级 `connection_instance`、连接级 MCP Token、统一 `ConnectorSpec` 和 `http_declarative` 声明式连接器。本设计是一期方案的增量演进，不替换连接器运行时。

二期把 MCP 发布和鉴权边界从单个连接上移到独立的服务入口：

- 租户只表示组织和数据隔离边界；
- 一个租户可以创建多个连接实例；
- 一个租户可以创建多个 MCP 服务入口；
- 一个服务入口可以显式发布多个连接实例中的多个工具；
- 服务 Token 绑定服务入口，不绑定连接器或连接实例；
- OpenAPI 声明式连接器支持一个 MCP 工具顺序调用多个 API operation；
- 租户使用单账号登录并自主管理本租户资源。

代码连接器仍由平台评审、安装和启用。租户不能上传 Python 包或执行自定义代码。

## 2. 领域边界

```text
Tenant
├─ TenantAccount
├─ ConnectionInstance *
│  ├─ ConnectionCredential *
│  ├─ ConnectionToolPolicy *
│  └─ Connector Tools *
└─ McpService *
   ├─ McpServiceToolBinding *
   └─ McpServiceToken *
```

### 2.1 租户与登录账号

`tenant` 是组织和数据隔离边界，不是连接器配置，也不是 MCP 凭证。

`tenant_account` 为每个租户保存一个管理面登录账号：

| 字段 | 说明 |
| --- | --- |
| `tenant_id` | 主键，并关联租户 |
| `password_hash` | Argon2id 或框架支持的强密码哈希 |
| `status` | `active/disabled/locked` |
| `failed_attempts` | 连续失败次数 |
| `locked_until` | 临时锁定截止时间 |
| `password_changed_at` | 密码变更时间 |
| `last_login_at` | 最近成功登录时间 |
| `created_at/updated_at` | 生命周期字段 |

租户使用“租户标识 + 密码”登录。租户密码只用于管理面，禁止作为 `/mcp/**` Bearer Token。

### 2.2 连接器定义与连接实例

`connector_key` 是平台内部稳定类型标识，例如 `wecom`、`http_declarative`，不是 Token，也不是租户需要输入的第三方 Key。创建连接时前端展示连接器卡片并自动提交对应标识。

代码连接器属于平台全局目录，由部署和允许列表控制。租户只创建连接实例并填写该实例的公开配置和第三方凭证。第三方凭证继续使用凭证保险库加密保存，不能通过租户管理 API 回读明文。

`connection_instance` 增加租户内唯一的 `connection_alias`。它是以字母开头、仅包含字母、数字、下划线、点和短横线的稳定标识，用于生成新工具绑定的默认别名建议，不是授权标识。显示名称可以包含中文；连接别名不能由显示名称在运行时临时推导。历史连接使用 `connector_key + "_" + connection_id 前八位` 幂等回填。

### 2.3 MCP 服务入口

新增 `mcp_service`：

| 字段 | 说明 |
| --- | --- |
| `service_id` | 不可预测标识 |
| `tenant_id` | 所属租户 |
| `display_name` | 服务名称 |
| `service_key` | 租户内唯一、稳定的可读标识 |
| `status` | `draft/active/disabled` |
| `config_version` | 工具映射和策略变更版本 |
| `created_at/updated_at` | 生命周期字段 |

对外地址为 `/mcp/service/{service_id}`。路径标识只用于路由，不构成授权。

### 2.4 服务工具绑定

新增 `mcp_service_tool_binding`：

| 字段 | 说明 |
| --- | --- |
| `binding_id` | 主键 |
| `service_id` | MCP 服务入口 |
| `connection_id` | 源连接实例 |
| `source_tool_key` | 连接器内部稳定工具键 |
| `tool_alias` | MCP 客户端看到的名称 |
| `binding_status` | `active/disabled/broken` |
| `policy_json` | 只能收紧连接策略的覆盖项 |
| `created_at/updated_at` | 生命周期字段 |

约束：

- `UNIQUE(service_id, tool_alias)`；
- `UNIQUE(service_id, connection_id, source_tool_key)`；
- 写入绑定时必须在事务内锁定并校验 `connection.tenant_id == service.tenant_id`；
- 源身份始终使用 `(connection_id, source_tool_key)`，不使用可能变化的 `mcp_name`；
- 默认别名由 `connection_alias + "__" + source mcp_name` 生成；
- `tool_alias` 在创建绑定时物化，修改连接别名不会改变已有工具名；
- 修改工具别名必须显式操作，并递增 `service.config_version`。

工具别名沿用 MCP 标识限制：以字母开头，只包含字母、数字、下划线、点和短横线，最大 128 个字符。

`binding_status` 持久化在绑定表。创建和编辑绑定时只能写 `active/disabled`；服务发布、连接 revision 变化以及运行时投影检查发现源工具不存在时，领域服务将其更新为 `broken`。`tools/list` 隐藏损坏工具，`tools/call` 返回稳定的“源工具不可用”错误，绝不能回退到同名工具。源工具重新出现后也不自动启用，必须由租户重新确认并保存为 `active`。

## 3. 服务 Token

新增 `mcp_service_token`，不改变一期 `connection_token` 的 HMAC-only 语义：

| 字段 | 说明 |
| --- | --- |
| `token_id` | 主键 |
| `service_id` | 所属服务 |
| `token_hmac` | 鉴权摘要，全局唯一 |
| `encrypted_token` | 可查看原文的密文 |
| `token_prefix` | 列表提示 |
| `token_label` | 客户端或用途名称 |
| `expires_at` | 可选过期时间 |
| `revoked_at` | 撤销时间 |
| `last_used_at` | 最近使用时间 |
| `created_at` | 创建时间 |

必须使用三类相互独立的生产密钥：

- `CREDENTIAL_KEY`：第三方系统凭证加密；
- `MCP_TOKEN_HMAC_KEY`：MCP Token 鉴权摘要；
- `MCP_TOKEN_PLAINTEXT_KEY`：服务 Token 原文加密与重加密。

鉴权路径只读取 HMAC，不解密 Token。查看接口才解密 `encrypted_token`。列表接口只返回前缀、标签、状态、有效期和最近使用时间；平台管理员或所属租户通过独立 reveal API 查看、复制完整 Token。按产品确认，reveal 不要求再次输入密码，但必须验证当前会话、校验租户归属、限速并记录不含原文的审计事件。前端不得把完整 Token 写入 localStorage、URL、日志或错误上报。

同一服务可签发多个并存 Token。签发新 Token 默认不撤销其他 Token；每个 Token 独立撤销和过期。轮换操作由调用方明确选择要撤销的旧 Token，避免破坏使用同一服务的其他客户端。

撤销后禁止 reveal，并清除或不可恢复地覆盖 `encrypted_token`。轮换 `MCP_TOKEN_PLAINTEXT_KEY` 时重加密所有未撤销密文；轮换 HMAC 密钥仍会使所有摘要失效，需按运维流程提前签发替代 Token。

## 4. 服务运行时与授权

服务层只是工具投影和鉴权边界，不创建第二套连接器运行时，也不把多个连接的凭证合并进同一个 `ConnectionContext`。

```text
/mcp/service/{service_id}
→ resolve_service_token(raw_token, path_service_id)
→ ServiceMcpGateway
→ resolve binding by (service_id, tool_alias)
→ load exactly one ConnectionContext
→ existing ConnectorRuntime
→ code connector or declarative connector
```

`tools/list` 和 `tools/call` 都必须按以下 AND 语义失败关闭：

1. 服务为 `active`；
2. Token 匹配 URL 中的 `service_id`，未撤销且未过期；
3. 工具绑定启用且未损坏；
4. 连接与服务属于同一租户；
5. 连接为 `active`；
6. 连接的工具策略允许 `source_tool_key`；
7. 服务策略允许该调用；
8. 写工具仍需连接策略显式允许写入。

服务层策略只能收紧超时、限流、读写和启停，不能放宽连接层策略。

限流同时应用 `(service_id, tool_alias)` 和 `(connection_id, source_tool_key)`，避免多个服务通过同一连接耗尽第三方配额。熔断继续以连接和源工具为主要边界。

服务会话缓存键为 `(service_id, service.config_version)`。绑定增删、启停、改名或服务策略变化时递增服务版本。连接状态、连接策略、源工具规范或声明式 revision 变化时，精确查找引用该连接的服务并使其缓存失效；若精确失效失败，则保守失效该租户全部服务缓存。

## 5. OpenAPI 多接口工具编排

声明式连接实例内部区分：

```text
Declarative Revision
├─ API Operation *
└─ Declarative Tool *
   ├─ Tool Step 1..N
   └─ Result Mapping
```

`API Operation` 保存受控 method、path、参数、响应字段、读写类型和安全策略。`Declarative Tool` 保存独立的 `tool_key`、MCP 名称、描述、输入 Schema、输出 Schema和有序步骤。一个 operation 可被多个工具复用，一个工具可调用多个 operation。

每个步骤包含：

- 租户定义且在工具内唯一的 `step_id`；
- revision 内存在的 `operation_key`；
- 从工具输入或前序步骤已声明输出到 operation 参数的显式 `input_map`；
- 允许后续步骤使用的显式 `output_mappings`；
- 不超过全局上限的步骤超时。

唯一允许的值引用为：

```text
$input.<declared_input_field>
$steps.<previous_step_id>.<declared_output_field>
```

不支持任意 JSONPath、字符串插值、模板函数、JavaScript、Python、Shell、条件、循环或并行。步骤只能引用前序步骤，发布时构建并验证有向无环的顺序依赖。最终 `result_map` 必须显式声明输出字段来源，禁止隐式返回最后一步完整响应。

第一版执行语义：

- 严格顺序执行；
- 任一步失败立即停止；
- 每步继续使用一期 SSRF、DNS、重定向、响应大小和字段白名单限制；
- 工具总超时受全局上限约束，不能仅依赖步骤超时之和；
- 默认只允许全读步骤，或最多包含一个写步骤；
- 多写编排不在本期范围，因为第三方 API 之间不存在事务，容易产生部分成功。

编排定义属于声明式 revision，而不是 MCP 服务入口。已发布 revision 不可原地修改；变更生成新 revision，并在发布时重新校验步骤引用、输入输出 Schema、读写策略和目标主机。

## 6. 登录与管理面隔离

管理面分成：

```text
/admin/login    平台管理员
/tenant/login   租户单账号
```

平台管理员与租户会话使用不同 Cookie、不同 API 前缀和明确的 `principal_type`。Cookie 必须设置 `HttpOnly`、生产环境 `Secure` 和合适的 `SameSite`。所有写接口具备跨站请求防护。

租户 API 使用 `/tenant/**`，租户身份只来自服务端会话。对 path 或 body 中出现的 `tenant_id`，服务端必须忽略或验证与会话租户完全一致。租户不得调用无租户限定的全局连接、服务或 Token 路由。

租户后台包含：

- 概览；
- 连接实例；
- MCP 服务；
- 调用日志；
- 账号设置。

平台管理员可以创建、禁用租户并设置或重置租户密码。租户可以修改自己的密码。登录失败按租户标识和来源地址双维度限速，达到阈值后临时锁定。

## 7. 管理流程

### 7.1 创建连接

```text
选择连接器卡片
→ 填写公开配置和第三方凭证
→ 连通性测试
→ 保存并启用连接
```

企业微信卡片在内部使用 `wecom`。OpenAPI 卡片使用 `http_declarative`，提供规范导入、operation 管理、组合工具配置、测试和 revision 发布。

### 7.2 创建 MCP 服务

```text
填写服务名称和稳定标识
→ 选择连接实例
→ 选择源工具
→ 生成并预览工具别名
→ 解决别名冲突
→ 发布服务
→ 签发并复制 Token
```

连接别名冲突时使用稳定的连接 ID 前八位作为建议后缀，并在保存前让用户确认；系统不能在发布后静默改变工具名。

## 8. 日志与错误处理

`mcp_call_log` 增加可空 `service_id` 和对外 `tool_alias`。服务调用同时记录 `tenant_id`、`service_id`、`connection_id`、`connector_key`、`source_tool_key` 和 `tool_alias`。连接旧入口的 `service_id` 保持为空。

OpenAPI 编排为一次 MCP 调用写父日志，并为每个步骤写安全摘要：`step_id`、`operation_key`、状态、耗时和固定错误码。日志禁止记录 Token、第三方凭证、请求头、完整 URL、原始请求正文或原始响应正文。

鉴权失败时，只有服务和 Token 已由服务端共同解析后才记录 `service_id`；不能把攻击者提供的路径值直接写为可信资源标识。

源工具缺失、连接停用、策略拒绝、步骤失败和超时都返回稳定错误码与安全摘要，不泄露内部堆栈、schema、凭证或第三方响应。

## 9. API 资源草案

平台管理员和租户分别使用 `/admin/**` 与 `/tenant/**` 路由，但调用同一领域服务。主要资源为：

```text
GET/POST    /tenant/connections
GET/PATCH   /tenant/connections/{connection_id}
GET/POST    /tenant/services
GET/PATCH   /tenant/services/{service_id}
GET/PUT     /tenant/services/{service_id}/tools
GET/POST    /tenant/services/{service_id}/tokens
POST        /tenant/services/{service_id}/tokens/{token_id}/reveal
DELETE      /tenant/services/{service_id}/tokens/{token_id}
GET         /tenant/mcp-logs
POST        /tenant/password/change
```

管理员端提供对应的租户限定路由。列表 DTO 永远不包含 Token 原文；原文只出现在签发、轮换和 reveal 响应中。

## 10. 兼容迁移与回滚

兼容矩阵：

| 入口 | Token | 行为 |
| --- | --- | --- |
| `/mcp` | legacy tenant Token | 继续映射默认企业微信连接 |
| `/mcp/{connection_id}` | `connection_token` HMAC | 保持一期行为，不能 reveal |
| `/mcp/service/{service_id}` | `mcp_service_token` | 新服务入口，可审计 reveal |

迁移仅增加新表、新列和新路由，不改变 `connection_token` 语义，不删除旧入口。对每个现有活动连接创建一个兼容默认服务并绑定当前启用工具，但该自动创建步骤必须幂等、带完成水位且可通过迁移开关禁用。现有 connection Token 不复制到 service Token 表，旧客户端继续走旧入口；租户需签发新的 service Token 才能使用新入口。

现有 HMAC-only Token 无法恢复原文，页面标记为“历史连接 Token，不可查看”。只有新服务 Token 支持后续查看复制。

新服务路由受功能开关保护。回滚时关闭服务路由和租户自助入口，旧 `/mcp`、`/mcp/{connection_id}`、连接 Token 和连接器运行时不受影响。新表保留，不在应用回滚中删除。服务发布与变更不能改变旧入口的 `tools/list` 或 `tools/call` 结果。

## 11. 删除与生命周期

- 删除或停用服务不影响连接实例；
- 撤销服务 Token 不影响其他 Token；
- 删除连接前查询所有服务工具绑定，有引用时默认阻止并列出受影响服务；
- 删除声明式 revision 前检查连接和工具引用；已发布且被引用的 revision 不允许删除；
- 停用连接后，所有引用服务立即隐藏相关工具并拒绝调用；
- 删除租户必须先禁用全部服务、撤销 Token，并按现有安全清理流程处理连接与历史 schema。

## 12. 验收标准

- 同一租户可创建多个连接和多个 MCP 服务；
- 一个服务可聚合不同连接器的工具；
- 跨租户工具绑定在事务内被拒绝；
- 服务 Token 访问错误 `service_id` 返回未授权；
- 租户 A 无法列表或 reveal 租户 B 的 Token；
- 平台管理员和所属租户可查看、复制未撤销的新服务 Token；
- 旧连接 Token 可继续调用，但不能 reveal；
- 修改连接别名不改变已绑定 `tool_alias`；
- 工具改名使服务版本递增并失效对应缓存；
- 连接禁用后，所有引用服务隐藏该连接工具并拒绝调用；
- 服务策略不能绕过连接的工具禁用和写权限；
- 声明式 step 2 引用未声明输出时发布失败；
- step 1 失败时不会执行 step 2；
- 声明式工具能顺序调用多个 operation 并按 `result_map` 合并结果；
- 旧 `/mcp` 和 `/mcp/{connection_id}` 在兼容期保持行为不变；
- 数据库迁移可重复执行，新功能开关关闭后旧运行时正常；
- 登录限速、会话隔离、Token reveal 审计和日志脱敏均有自动化测试。

## 13. 非目标

本期不支持：

- 租户多用户、成员邀请或角色权限；
- 租户上传或执行自定义代码连接器；
- OpenAPI 编排中的条件、循环、并行、脚本、模板函数或任意表达式；
- 多写步骤的跨 API 事务或自动补偿；
- 在 MCP 服务层跨多个连接编排一个工具；
- 移除旧 `/mcp`、`/mcp/{connection_id}` 或 `connection_token`；
- 让服务层策略放宽连接层安全策略。

## 14. 设计复核结论

2026-07-17 按用户要求仅使用 CC（Claude Code）完成只读架构复核，未调用 Gemini。复核确认方向可行；本设计已纳入 Token 密钥分离、策略 AND 叠层、身份域分离、同租户绑定、别名物化、封闭编排语言、兼容矩阵和服务缓存版本八项关键修正。
