# 实施计划

## 决策

- `tenant_config` 本次逻辑上收敛为租户身份根；保留 `tenant_id`、`display_name`、`enabled` 与时间字段。
- `tenant_account` 继续承载密码哈希、登录状态、失败次数和锁定信息。
- `connection_instance` 承载连接名称、连接器类型、数据模式与公开配置。
- `connection_credential` 承载企业微信应用 Secret 和通讯录 Secret。
- `connection_token` 承载连接 MCP Token；`mcp_service_token` 承载服务 Token。
- 旧企业微信列只作为兼容源保留，不再由租户 CRUD 写入；后续清理必须是独立发布。
- 现有 `tenant_config.enabled` 保留为登录、连接 Token 和写操作的硬隔离闸门。

## Layer 1：可并行

### A. 后端租户身份契约与兼容迁移

文件：

- `app/admin.py`
- `app/tenant.py`
- `sql/009_tenant_identity_boundary.sql`
- `deploy/server_deploy.sh`
- `tests/test_admin_security.py`
- `tests/test_migrations.py`
- `tests/test_server_deploy_script.py`

步骤：

1. 拆分严格的租户创建/更新模型，拒绝旧企业微信字段。
2. 创建租户只写身份根与登录账号；更新只改名称、租户状态和显式密码。
3. 禁用租户时同步禁用登录账号；重新启用不得隐式激活旧账号。
4. 列表只返回身份、登录状态和时间元数据。
5. 新增 MySQL 5.7 可重复迁移，放宽旧连接列的非空约束但不删除数据/索引。
6. 旧租户加载器只加载完整 legacy 连接行，纯身份租户不得导致启动失败。
7. 部署脚本加入 009。

### B. 管理员租户页面收敛

文件：

- `admin-ui/src/pages/Tenants.jsx`
- `admin-ui/src/pages/tenantsView.js`
- `admin-ui/src/pages/tenantsView.test.js`

步骤：

1. 列表只展示租户名称、ID、租户状态和登录状态。
2. 创建/编辑表单只保留名称、ID、密码与状态。
3. 移除 CorpID、Secret、旧 MCP Token、同步策略、可信域名、租户级同步和诊断入口。
4. 保留连接中心、MCP 服务和日志的全局导航，不在租户信息表单内配置连接。

### C. 企业微信连接实例友好表单

文件：

- `admin-ui/src/pages/Connections.jsx`
- `admin-ui/src/pages/connectionView.js`
- `admin-ui/src/pages/connectionView.test.js`

步骤：

1. 企业微信创建表单使用命名字段，不要求用户编辑 JSON。
2. 公开配置映射 CorpID、同步模块、同步间隔、打卡用户与可信域名。
3. 凭据映射应用 Secret、通讯录 Secret，保存后立即清空且不回显。
4. MCP Token 继续使用连接 Token 页签，不混入企业微信凭据。
5. 声明式连接保留现有向导和 JSON 流程。

## Layer 2：集成

文件：

- `app/connectors/wecom.py`
- `tests/test_wecom_connector.py`
- 必要的共享前端样式或纯函数

步骤：

1. 完整声明企业微信公开配置 schema 和凭据 schema。
2. 验证连接实例是运行时企业微信配置的权威来源。
3. 保留 deterministic legacy 默认连接及原 schema 名，不按新 CorpID 重算历史 schema。
4. 对齐前后端字段和校验。

## 验收

- 管理员无需企业微信字段即可创建租户并设置密码。
- 旧企业微信字段提交到租户 API 返回 422，不会静默忽略。
- 租户登录后可管理自己的连接实例、MCP 服务和调用日志。
- 企业微信连接实例可配置凭据、数据模式、同步策略和可信域名。
- 现有租户连接、Token、服务、日志、schema 与同步数据不丢失。
- 禁用租户后登录、连接 Token 和写操作继续被拒绝。
- Python、Node 测试和前端生产构建全部通过。
