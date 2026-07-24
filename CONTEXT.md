# Multi-tenant MCP Platform

该上下文定义平台中的身份隔离主体、外部系统连接与对外 MCP 能力边界。

## Language

**租户（Tenant）**:
平台中的身份与数据隔离主体，具有租户名称、租户 ID 和登录凭据。
_Avoid_: 企业微信配置、连接配置

**连接实例（Connection Instance）**:
某个租户对一个外部系统的具体连接，也是该外部系统唯一可管理的 MCP 能力边界。
_Avoid_: 租户配置、MCP 服务

**企业微信连接器（WeCom Connector）**:
描述企业微信连接实例可使用的配置、凭据、同步能力和工具集合的连接器类型。
_Avoid_: 企业微信租户

**MCP 端点（MCP Endpoint）**:
连接实例固有的 MCP 访问入口，与连接实例共享生命周期、访问令牌和工具策略。
_Avoid_: MCP 服务、独立服务

**调用日志（Call Log）**:
记录租户、连接实例及工具调用结果的审计记录。
