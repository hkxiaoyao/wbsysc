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
| `ADMIN_PASSWORD` | 管理后台强密码 |
| `ADMIN_SESSION_TTL_MIN` | 管理会话有效分钟数 |
| `CREDENTIAL_KEY` | 凭证加密密钥，生产至少 32 个 UTF-8 字节 |
| `MCP_TOKEN_HMAC_KEY` | Token 基于哈希的消息认证码（HMAC）密钥，生产至少 32 个 UTF-8 字节，必须使用非示例值 |
| `CONNECTOR_ALLOWLIST` | 已审核连接器入口名的逗号分隔精确列表 |
| `MCP_BASE_URL` | 对外 HTTPS 基础地址，也参与 Host 许可判断 |
| `MCP_ALLOWED_HOSTS` | 反向代理 Host 的逗号分隔精确列表 |
| `WECOM_USE_MOCK` | 生产必须设为 `false` |
| `REDIS_URL` | 可选 Redis 地址，留空时使用进程内缓存 |
| `SYNC_INTERVAL_REPORT_MIN` | 企微汇报同步间隔 |
| `SYNC_INTERVAL_APPROVAL_MIN` | 企微审批同步间隔 |
| `SYNC_INTERVAL_SMARTTABLE_MIN` | 企微智能表格同步间隔 |

`CREDENTIAL_KEY` 与 `MCP_TOKEN_HMAC_KEY` 必须独立。轮换 `CREDENTIAL_KEY` 前先重加密全部凭证，否则旧密文无法解密。轮换 `MCP_TOKEN_HMAC_KEY` 会让所有现有 MCP Token 摘要失配，请先为每个连接签发新 Token，再切换密钥。

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
CONNECTOR_ALLOWLIST=reviewed-connector-name
```

## 迁移企微连接

迁移保持 MySQL 5.7 兼容，不使用 MySQL 8 专属语法。请按顺序执行，任何一步失败都停止发布。

1. 停止配置变更，记录当前镜像版本和日志保留天数
2. 对中心库和所有 `wbd_*` 租户 schema 做一致性备份
3. 验证备份可列出 `tenant_config`、业务表和 `audit_log`
4. 将备份时间与二进制日志位置登记为恢复点
5. 使用 MySQL 5.7 客户端执行 `sql/005_mcp_call_log.sql`
6. 使用同一客户端执行 `sql/006_connection_platform.sql`
7. 启动新应用，让启动迁移创建缺失对象并执行旧企微回填
8. 核对每个旧租户只有一个确定性默认 `wecom` 连接
9. 并行调用旧 `/mcp` 和新 `/mcp/{connection_id}`，比较工具和结果
10. 通过兼容门禁后，逐批把客户端切到新地址

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

Token 只在签发响应中返回一次。不要把 Authorization、Cookie、Token、凭证、原始请求正文或原始响应正文写入工单和日志。

## 发布连接器包

只安装经过评审和签名校验的固定版本包。后台不接受上传 Python 包，应用也不加载未列入允许列表的入口。

1. 评审源码、依赖锁文件、`ConnectorSpec`、工具输入输出 schema 和写操作声明
2. 验证构建来源、包摘要和发布签名
3. 在隔离环境安装固定版本，检查 `wbsysc.connectors` 入口名
4. 将归一后的精确入口名加入 `CONNECTOR_ALLOWLIST`
5. 部署镜像并重启应用，检查发现失败日志和活动连接依赖
6. 在非生产连接验证工具列表、调用、限流、同步和日志脱敏
7. 发布到生产，并按连接逐批启用

回滚时先禁用使用该包的连接，再恢复旧镜像和旧允许列表。重启应用以卸载新入口，然后验证旧版本连接器和不相关连接仍可用。

## 审查声明式规范

发布 revision 前，评审人员必须核对每个 operation。已发布 revision 不允许原地修改。

- **目标主机**：只写精确 HTTPS 主机名，不允许通配内网、IP 字面量、用户信息或非标准混淆形式
- **域名系统**：解析域名系统（DNS）结果，拒绝环回、链路本地、私网、保留地址和元数据地址
- **重定向**：逐跳重新校验协议、主机、DNS 与 IP，超出跳数立即失败
- **开放授权（OAuth）**：只允许 OAuth 2.0 client credentials；Token URL 必须是许可的精确 HTTPS 地址；Token 交换禁止重定向；平台不支持 authorization code 或 `state` 流程；凭证只写加密存储
- **响应限制**：设定超时、最大响应字节数、分页上限和字段提取白名单
- **操作范围**：只发布已声明 operation，默认只读，写操作同时要求 ToolSpec 与连接策略允许

安全测试至少包含许可 HTTPS 主机、未许可主机、HTTP 降级、内网解析、DNS 重绑定和跳转到内网。任何不安全 URL 必须在发出业务请求前拒绝。

## 监控和审计

按租户和连接观察鉴权拒绝率、工具错误率、P95 耗时、同步延迟、限流、熔断和缓存失效。为连接禁用、Token 签发与撤销、凭证修改、策略修改、规范发布、同步和日志删除保留管理审计记录。

告警中只记录安全标识和固定错误码。定期验证日志保留任务按设置删除，并抽查 `tenant_id` 与 `connection_id` 同时参与查询边界。

## 回滚单个连接

新旧企微对账出现任何工具、鉴权或结果差异时，立即回滚受影响连接。不要删除连接、凭证、策略、日志或已同步数据。

1. 将受影响连接状态设为 `disabled`
2. 失效精确 MCP 缓存键 `(connection_id, config_version)`
3. 保留连接数据、迁移水位、审计日志和故障样本
4. 让该租户的旧企微 Token 通过 legacy adapter 继续访问旧 `/mcp`
5. 修正归属任务中的实现或数据，再重跑新旧对账
6. 签发或确认有效 Token，启用连接并重试迁移

只回滚受影响连接，不清空其他连接缓存。若数据库结构损坏或多个租户出现同类故障，请停止应用并执行完整备份恢复流程。

## 执行非生产冒烟检查

只在获得授权的非生产浏览器和 MySQL 环境中执行本节。使用非生产 ID 和唯一前缀，例如 `smoke_1234567890123`，并在开始前记录当前日志保留天数。

1. 在浏览器创建测试租户和企微连接
2. 保存只显示一次的 Token，并调用 `/mcp/{connection_id}`
3. 轮换 Token，确认旧 Token 失败且新 Token 成功
4. 禁用一个工具，确认工具列表立即移除它
5. 按测试租户和连接查看日志
6. 导入只访问许可 HTTPS 主机的安全规范并发布 revision
7. 导入指向环回或私网的 URL，确认系统拒绝
8. 恢复冒烟前的日志保留天数
9. 执行清理并确认查询结果为零

管理 API 可以撤销 Token 和禁用连接，但当前不提供删除连接的端点。完成冒烟后，先撤销 Token 并禁用连接，再在备份后执行以下 MySQL 5.7 事务。执行前替换描述性占位值，并确认它们只匹配本次唯一前缀：

```sql
START TRANSACTION;
DELETE FROM declarative_spec_operation WHERE connection_id = 'smoke_connection_id';
DELETE FROM declarative_spec_revision WHERE connection_id = 'smoke_connection_id';
DELETE FROM connection_sync_state WHERE connection_id = 'smoke_connection_id';
DELETE FROM connection_tool_policy WHERE connection_id = 'smoke_connection_id';
DELETE FROM connection_token WHERE connection_id = 'smoke_connection_id';
DELETE FROM connection_credential WHERE connection_id = 'smoke_connection_id';
DELETE FROM mcp_call_log WHERE tenant_id = 'smoke_tenant_id'
  AND connection_id = 'smoke_connection_id';
DELETE FROM connection_instance WHERE tenant_id = 'smoke_tenant_id'
  AND connection_id = 'smoke_connection_id';
COMMIT;
```

再清理管理操作审计中带唯一前缀的测试记录，并删除测试租户。若冒烟修改了 `gateway_setting` 中的日志保留值，请通过 `PUT /admin/mcp-log-settings` 恢复记录值，不要删除其他环境设置。
