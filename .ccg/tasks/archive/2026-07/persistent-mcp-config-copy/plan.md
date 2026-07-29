# 实施计划

## Layer 1：并行实现

1. Token 持久化：为 `connection_token` 增加可空密文字段；新签发、创建和轮换时加密保存；撤销和轮换旧 Token 时清除密文；提供租户边界内的 reveal；历史 Token 保持不可恢复。
2. 权限接口：为管理员和租户连接接口增加单 Token reveal 路由，要求认证、同源、no-store、限流和审计，跨租户及不可用 Token 统一返回安全错误。
3. 前端交互：有效且可 reveal 的 Token 行显示“复制 MCP 配置”，调用 reveal 后直接复制；历史 Token 给出明确不可复制提示；撤销 Token 不显示复制操作。

## Layer 2：集成验证

1. 运行定向后端与前端测试。
2. 运行完整后端测试、前端测试、构建和差异检查。
3. 双模型审查，修复 Critical/Warning 后归档提交。
