---
title: 如何迁移和运维多连接 MCP 平台
contentType: How-to
---

# 如何迁移和运维多连接 MCP 平台

TL;DR：先备份 MySQL，再部署连接平台表和应用。保留旧模型上下文协议（MCP）路径 `/mcp`，直到新旧企微工具、鉴权和结果对账全部通过。本文面向部署与值班人员，目标是安全迁移、维护连接并完成可恢复的非生产验证。

## 配置生产环境

生产启动会校验数据库、管理端、凭证和 MCP Token 配置。请使用密钥管理系统保存真实值，不要把值提交到仓库。

| 变量 | 要求 |
| --- | --- |
| `APP_ENV` | 生产设为 `prod`，开发设为 `dev` |
| `APP_HOST` | 应用监听地址，不是第三方 API 许可地址 |
| `APP_PORT` | 应用监听端口 |
| `LOG_LEVEL` | 日志级别，例如 `INFO` |
| `DB_HOST` | MySQL 主机名，不在文档中写真实地址 |
| `DB_PORT` | MySQL 端口 |
| `DB_NAME` | 中心库名 |
| `DB_USER` | 最小权限账户 |
| `DB_PASSWORD` | 非示例强密码 |
| `DB_POOL_SIZE` | SQLAlchemy 连接池大小 |
| `DB_MIGRATION_HOST` | 发布主机访问 MySQL 的地址，可由发布终端环境覆盖 `.env` |
| `DB_MIGRATION_USER` | 独立迁移账户，只从发布终端环境读取，必须与运行时 `DB_USER` 不同 |
| `DB_MIGRATION_PASSWORD` | 独立迁移账户密码，只从发布终端环境读取，不写入应用 `.env` |
| `ADMIN_PASSWORD` | 管理后台强密码 |
| `ADMIN_SESSION_TTL_MIN` | 管理会话有效分钟数 |
| `CREDENTIAL_KEY` | 凭证加密密钥，生产至少 32 个 UTF-8 字节 |
| `MCP_TOKEN_HMAC_KEY` | Token 基于哈希的消息认证码（HMAC）密钥，生产至少 32 个 UTF-8 字节，必须使用非示例值 |
| `MCP_TOKEN_PLAINTEXT_KEY` | 可揭示服务 Token 的密文密钥，生产至少 32 个 UTF-8 字节，必须使用非示例值 |
| `MCP_SERVICE_ENABLED` | 服务路由和租户服务自助开关；首次发布保持 `false` |
| `CONNECTOR_ALLOWLIST` | 已审核连接器入口名的逗号分隔精确列表 |
| `MCP_BASE_URL` | 对外 HTTPS 基础地址，也参与 Host 许可判断 |
| `MCP_ALLOWED_HOSTS` | 反向代理 Host 的逗号分隔精确列表 |
| `WECOM_USE_MOCK` | 生产必须设为 `false` |
| `REDIS_URL` | 可选 Redis 地址，留空时使用进程内缓存 |
| `SYNC_INTERVAL_REPORT_MIN` | 企微汇报同步间隔 |
| `SYNC_INTERVAL_APPROVAL_MIN` | 企微审批同步间隔 |
| `SYNC_INTERVAL_SMARTTABLE_MIN` | 企微智能表格同步间隔 |

三个密钥必须两两不同并独立轮换，不能把“换环境变量”当作轮换完成：

- `CREDENTIAL_KEY`：先用旧密钥解密并用新密钥重加密全部连接/租户凭证，再切换。
- `MCP_TOKEN_HMAC_KEY`：当前运行时只接受一个 HMAC key，没有双 key 验证窗口；旧 key 下“预签发”的 Token 切换后同样失效。先盘点并记录全部旧连接/服务 token ID 及使用方，在批准的维护窗口切换 key 并重启；随后只在新 key 下逐一签发、分发和验证新 Token，最后核对清单中所有旧 token ID 已失效/撤销。窗口内客户端会短暂不可用，不能声称预签发可保持连续可用。
- `MCP_TOKEN_PLAINTEXT_KEY`：切换前必须把每条**未撤销**的 `mcp_service_token.encrypted_token` 用旧密钥解密、用新密钥重加密并完成数量核对；已撤销行的密文已清空，不能恢复也不需要迁移。无法完成重加密时应重新签发服务 Token，不得直接切换。

`CONNECTOR_ALLOWLIST` 使用 `wbsysc.connectors` 入口名，不使用 Python 包导入路径。平台去除首尾空白，将名称转为小写，并把连续的 `-`、`_`、`.` 归一为单个 `-`，然后执行精确匹配。例如，已审核入口 `reviewed_connector_name` 应写为 `reviewed-connector-name`。

以下模板只展示占位值：

```dotenv
APP_ENV=prod
DB_HOST=mysql_host_name
DB_PORT=3306
DB_NAME=central_database_name
DB_USER=least_privilege_user
DB_PASSWORD=database_password_here
CREDENTIAL_KEY=replace_with_credential_key
MCP_TOKEN_HMAC_KEY=replace_with_hmac_key
MCP_TOKEN_PLAINTEXT_KEY=replace_with_plaintext_key
MCP_SERVICE_ENABLED=false
CONNECTOR_ALLOWLIST=reviewed-connector-name
```

## 迁移企微连接

迁移保持 MySQL 5.7 兼容，不使用 MySQL 8 专属语法。请按顺序执行，任何一步失败都停止发布。

1. 停止配置变更，记录当前镜像版本和日志保留天数
2. 对中心库和所有 `wbd_*` 租户 schema 做一致性备份
3. 验证备份可列出 `tenant_config`、业务表和 `audit_log`
4. 将备份时间与二进制日志位置登记为恢复点
5. 使用独立迁移账户和 MySQL 5.7 客户端严格执行 `004` → `005` → `006` → `007` → `008`
6. 保持 `MCP_SERVICE_ENABLED=false`，重建应用并等待 `/health` 明确返回 `mcp_service_enabled:false`
7. 核对每个旧租户只有一个确定性默认 `wecom` 连接，旧 `/mcp` 与 `/mcp/{connection_id}` 正常
8. 如本次批准启用服务，改为 `true` 后**重建**应用，并等待健康响应明确返回 `mcp_service_enabled:true`
9. 核对默认服务回填、服务路由和租户控制台，再逐批切换客户端

顺序不可跳跃：`008_mcp_service.sql` 依赖 `005` 的 `mcp_call_log` 和 `006` 的连接表，`007` 提供租户登录。结构迁移只前进，不因功能回滚删除。推荐使用 `deploy/server_deploy.sh`，它会在拉取/启动前执行精确顺序，并先以关闭状态启动、健康检查，再按原请求值启用。

旧企微回填使用完成水位 `legacy_wecom_backfill_v1`。迁移只在事务成功提交后写水位，重启会重试未完成租户，重复执行不会创建第二个默认连接，也不会删除旧企微数据。

兼容期内保留旧 `/mcp`、旧租户字段和旧业务表。门禁要求同一 Token 在新旧地址通过相同鉴权，`tools/list` 完全一致，抽样 `tools/call` 结果一致，且连接日志无跨实例记录。所有已迁移租户连续完成一个约定观察周期后，变更评审才能批准移除旧路径。

## 备份和恢复

恢复操作会覆盖迁移后的中心配置。执行前先保留故障现场和迁移后数据副本。

1. 停止应用和调度器，阻止新的写入与同步
2. 恢复迁移前的中心库和全部租户 schema
3. 将二进制日志恢复到登记的迁移恢复点
4. 部署记录中的旧镜像版本
5. 清空进程内 MCP 会话与连接缓存，Redis 部署只删除受影响连接的键
6. 启动旧应用，并验证 `/health` 与旧 `/mcp`

应用重启会清空进程内 MCP 会话、Token 缓存和连接数据缓存。管理员浏览器会话可能失效，值班人员需要重新登录。数据库恢复不会撤销第三方系统内已经发生的写操作，请单独核对写工具审计记录。

## 维护连接、Token 和工具

管理后台操作必须带管理员会话，并指定服务端已校验的租户与连接。完成每个变更后，验证目标连接的 `config_version` 增加，并确认只失效该连接的 MCP 缓存。

- **创建连接**：调用 `POST /admin/tenants/{tenant_id}/connections`，保存响应中只显示一次的 Token
- **禁用连接**：调用 `POST /admin/connections/{connection_id}/disable`，确认新请求返回未授权或不可用
- **轮换 Token**：调用 `POST /admin/connections/{connection_id}/tokens/rotate`，分发新 Token 后验证旧 Token 立即失效
- **撤销 Token**：调用 `DELETE /admin/connections/{connection_id}/tokens/{token_id}`，确认该 Token 无法访问旧、新地址
- **修改工具策略**：调用 `PUT /admin/connections/{connection_id}/tools`，确认禁用工具不再出现在 `tools/list`
- **触发同步**：调用 `POST /admin/connections/{connection_id}/sync`，检查连接级游标和安全错误摘要
- **查看日志**：调用 `GET /admin/mcp-logs?tenant_id={tenant_id}&connection_id={connection_id}`，同时使用两个过滤维度
- **修改保留期**：调用 `PUT /admin/mcp-log-settings`，变更前记录旧值，变更后检查清理任务计数

连接 Token 只在签发响应显示一次，之后不可揭示。未撤销的**服务 Token**可由当前平台管理员或该服务所属租户的当前登录会话，通过限流、审计且响应带 `Cache-Control: no-store` 的 reveal 端点再次查看；其他租户、过期会话和已撤销 Token 均不得揭示。揭示不是绕过审计的“复制数据库密文”。不要把 Authorization、Cookie、Token、凭证、原始请求正文或原始响应正文写入工单和日志。

## 租户密码与会话

- 创建租户时必须设置初始密码，并在同一事务中创建登录账户；后台不读取既有密码。
- 管理员可通过 `PUT /admin/tenants/{tenant_id}/login-password` 设置或重置密码；成功后账户为可登录状态并撤销该租户全部已有会话，用户须重新登录。
- `PUT /admin/tenants/{tenant_id}/login-status` 只接受明确的启用/禁用状态；禁用会撤销全部租户会话。普通租户资料编辑不得意外重新启用已禁用登录。
- 租户自行修改密码同样撤销全部会话并清除当前 Cookie。工单、日志和浏览器状态中都不得保存密码或密码派生提示。

## 默认服务回填与功能回滚

默认服务回填仅在 `MCP_SERVICE_ENABLED=true` 的应用重启中、可信连接器注册完成后运行。它使用水位、可重复执行，为已有连接创建确定性默认服务及当前启用工具的绑定，**不会复制连接 Token 行**。

服务功能异常时，把 `.env` 中 `MCP_SERVICE_ENABLED=false`，执行容器重建（仅 restart 不保证重新读取环境），并验证 `/health` 返回 `mcp_service_enabled:false`。此回滚保留 `008` 表和数据，关闭 `/mcp/service/{service_id}`、租户服务管理 UI/API 和服务运行时；旧 `/mcp`、`/mcp/{connection_id}` 及连接 Token 继续工作；经过认证的平台管理员服务管理 API 继续可用，以便精确撤销 Token、禁用服务和清理。若启用阶段健康检查失败，发布脚本会自动恢复 `false`、重建并验证关闭态，然后以非零状态退出。

## 发布连接器包

只安装经过评审和签名校验的固定版本包。后台不接受上传 Python 包，应用也不加载未列入允许列表的入口。

1. 评审源码、依赖锁文件、`ConnectorSpec`、工具输入输出 schema 和写操作声明
2. 验证构建来源、包摘要和发布签名
3. 在隔离环境安装固定版本，检查 `wbsysc.connectors` 入口名
4. 将归一后的精确入口名加入 `CONNECTOR_ALLOWLIST`
5. 部署镜像并重启应用，检查发现失败日志和活动连接依赖
6. 在非生产连接验证工具列表、调用、限流、同步和日志脱敏
7. 发布到生产，并按连接逐批启用

回滚时先禁用使用该包的连接，再恢复旧镜像和旧允许列表。删除允许列表项前必须显式核对并禁用依赖连接；当前启动检查不会报告已经从允许列表移除的连接器。重启应用以卸载新入口，然后验证旧版本连接器和不相关连接仍可用。

## 审查声明式规范

发布 revision 前，评审人员必须核对每个 operation。已发布 revision 不允许原地修改。

- **目标主机**：只写精确 HTTPS 主机名，不允许通配内网、IP 字面量、用户信息或非标准混淆形式
- **域名系统**：解析域名系统（DNS）结果，拒绝环回、链路本地、私网、保留地址和元数据地址
- **重定向**：逐跳重新校验协议、主机、DNS 与 IP，超出跳数立即失败
- **开放授权（OAuth）**：只允许 OAuth 2.0 client credentials；Token URL 必须是许可的精确 HTTPS 地址；Token 交换禁止重定向；平台不支持 authorization code 或 `state` 流程；凭证只写加密存储
- **响应限制**：设定超时、最大响应字节数和字段提取白名单；当前版本不支持声明式分页，导入含分页声明的规范必须失败关闭
- **操作范围**：只发布已声明 operation，默认只读，写操作同时要求 ToolSpec 与连接策略允许

安全测试至少包含许可 HTTPS 主机、未许可主机、HTTP 降级、内网解析、DNS 重绑定和跳转到内网。任何不安全 URL 必须在发出业务请求前拒绝。

## 监控和审计

按租户和连接观察鉴权拒绝率、工具错误率、P95 耗时、同步延迟、限流、熔断和缓存失效。为连接禁用、Token 签发与撤销、凭证修改、策略修改、规范发布、同步和日志删除保留管理审计记录。

告警中只记录安全标识和固定错误码。定期验证日志保留任务按设置删除，并抽查 `tenant_id` 与 `connection_id` 同时参与查询边界。

## 回滚入口切换

旧 `/mcp` 与新 `/mcp/{connection_id}` 使用同一连接、Token 和运行时。新旧企微对账出现工具、鉴权或结果差异时，不要禁用默认企微连接，否则两个入口都会停止工作。不要删除连接、凭证、策略、日志或已同步数据。

1. 停止把该租户客户端切换到新地址，已切换客户端改回旧 `/mcp`
2. 保持确定性默认企微连接为 `active`，保留当前有效 Token
3. 失效该连接的精确 MCP 缓存键 `(connection_id, config_version)`
4. 保留连接数据、迁移水位、审计日志和故障样本
5. 若同一运行时在旧入口也失败，停止发布并按“备份和恢复”回滚整个应用版本
6. 修正实现或数据，重新比较 `tools/list`、抽样 `tools/call` 和连接日志后再切换

入口切换回滚只影响目标客户端和目标连接缓存。非默认第三方连接可以直接禁用；默认企微连接不能依赖旧入口绕过禁用状态。若数据库结构损坏或多个租户出现同类故障，请停止应用并执行完整备份恢复流程。

## 经授权的可逆冒烟

生产冒烟默认禁止。开始前必须具备书面变更授权、维护窗口、明确生产目标、一次性 schema 权限、具名操作人和复核人、备份/恢复点以及清理批准。生产运行须显式设置 `MCP_SMOKE_MODE=production`、`MCP_SMOKE_PRODUCTION_OPT_IN=I_ACCEPT_PRODUCTION_SMOKE` 与 `MCP_SMOKE_WRITTEN_AUTHORIZATION=I_HAVE_WRITTEN_AUTHORIZATION`；这三个值只是防误触门禁，可访问或可解析的地址绝不视为授权。生产基础 URL 必须为 HTTPS，`MCP_SMOKE_PRODUCTION_HOST` 必须与 URL host 精确一致，并拒绝 loopback、非全局 IP、单标签、`.test`/`.invalid`/`.example`/`.localhost`、`example.com`/`.net`/`.org` 及其全部子域，以及已知测试/模板 host；本地模式只允许 loopback。URL 端口或 IPv6 语法错误会在联网前用固定错误码拒绝。

基础 URL、两个 connection ID/完整 endpoint/Token、real service ID/完整 endpoint/Token、wrong service ID/完整 endpoint、`MCP_SMOKE_BAD_CONNECTION_TOKEN`、`MCP_SMOKE_BAD_SERVICE_TOKEN`、三组精确预期 alias，以及三个仅访问已批准一次性数据的 call alias/JSON 参数必须逐项显式提供。资源 ID 的**输入原文**必须是平台实际持久化的规范小写 UUID（API 创建为 v4，确定性默认服务可为 v5），不能先转小写再接受；并对低多样性、周期和明显顺序 UUID 模板做保守预检。`your-*-here`、`dummy`、`todo` 等文档模板不能运行。

连接 Token 必须匹配 `mcp_` 加 43 个 URL-safe Base64 字符，服务 Token 必须匹配 `mcp_svc_` 加 43 个字符；后缀还必须规范解码为恰好 32 bytes。两个 bad Token 也必须分别采用正确类别形状，但与全部有效 Token 不同。联网前会拒绝单一/低多样性、明显顺序 payload，以及任何至少出现两次的 proper-period 前缀重复（包括 10-byte motif 重复后截断和 16-byte motif 重复）。这只是“明显占位/低多样性预检”，**不能证明密码学熵、真实签发或授权**；生产值仍必须来自平台的 `token_urlsafe(32)` 签发流程和本次受控记录。所有 Token 保留原字节，任何首尾空白直接拒绝，不能 `strip` 后继续。

检查按固定顺序产生 3 个接受检查和 11 个拒绝检查：两个 bad Token 分别覆盖其真实目标和第二目标，服务 Token 用于 connection 1/2，connection 1/2 Token 用于服务，两个连接间 Token 双向交叉，以及服务 Token 用于 wrong service。只有明确的 MCP/HTTP 401/403 算拒绝成功，DNS/TLS/timeout/transport/protocol/程序错误和取消都算失败。工具 alias 必须与预期集合完全相等，不允许额外暴露。CLI 最外层把取消、异常组及其他基础异常转成固定非零结果，不输出 traceback 或异常文本；SystemExit 仅保留 `1..125`，其他值固定为 `1`，KeyboardInterrupt 固定为 `130`。输出不记录 Cookie、Authorization、原始 Token、工具返回体或任意异常消息。

1. 在本地生成一个高熵 run ID。操作表记录精确的 tenant ID、完整 schema 名、两个 connection ID/alias、全部 service ID/key（包括精确 tenant ID 查出的自动回填服务）、binding ID、service/connection token ID 与安全 prefix、spec/revision/operation ID、原日志保留值和每张相关表的冒烟前计数；原始 Token/密码/Cookie 不入表。
2. 创建一次性租户和初始密码，验证登录、退出、管理员重置、登录状态和会话撤销；验证过程不保存密码/Cookie。
3. 创建恰好两个一次性连接，其中一个为受控 OpenAPI 连接；发布只访问已授权一次性上游的两步组合工具。
4. 创建一个绑定两个连接工具的服务，验证别名唯一、`tools/list`/受控调用、服务 Token 对第二个服务的错误绑定拒绝、签发/揭示/复制、撤销后揭示与认证拒绝、租户范围日志，以及旧连接入口兼容。
5. 保存所需审计证据后，先经 API 撤销每个服务和连接 Token，再禁用服务及两个连接；通过 API 恢复原日志保留设置并读取确认。

### 精确清理门禁

不得使用 `LIKE`、前缀匹配、通配 schema 名或未绑定 tenant ID 的删除。先把记录的 ID 装入会话临时表（示意名 `smoke_service_ids`、`smoke_connection_ids`、`smoke_token_ids`），逐项比较精确所有权及期望计数；任何断言不符立即 `ROLLBACK` 并保留现场。确认子表计数后，在**一个事务**中按子到父删除：

1. `mcp_service_token`
2. `mcp_service_tool_binding`
3. 精确 tenant/service/connection 维度的 `mcp_call_log`
4. `mcp_service`
5. `declarative_spec_operation`、`declarative_spec_revision`
6. `connection_sync_state`、`connection_tool_policy`、`connection_token`、`connection_credential`
7. 两条精确 `connection_instance`
8. `tenant_session`、`tenant_account`
9. 精确 tenant 的 `domain_verify_file`
10. 最后删除精确 `tenant_config`

每条语句使用记录的完整 ID 集合和 tenant ID，并检查 `ROW_COUNT()` 等于批准值；只有全部所有权/计数断言都满足才 `COMMIT`。随后对上述每张表用同一精确 ID 集合验证零行、零存活 Token，确认设置等于原值且非测试租户总计数没有意外变化。

schema 删除必须是独立的第二阶段：从已记录的 `tenant_config` 证据证明 schema 名与创建时的**完整 run ID** 完全对应，并查询确认没有其他 `tenant_config` 行引用该精确 schema，才可对转义后的完整标识执行一次精确 `DROP DATABASE`。无法证明时保留 schema 并登记，不得猜测。最后确认 schema 不存在（或明确登记保留）、旧 `/mcp` 和 `/mcp/{connection_id}` 仍健康。清理 SQL 应由 DBA 根据记录 ID 生成并由复核人逐条核对，不在通用文档提供可被误用的宽泛删除模板。
