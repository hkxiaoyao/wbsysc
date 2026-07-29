# 部署验收记录

- 功能提交：`e09d61b feat: allow persistent MCP config copy`
- GitHub Actions：`30417186077`，test 与 build-push 均成功
- 数据库迁移：004–012 全部成功
- 部署方式：GHCR 拉取 300 秒超时后自动回退本地构建，构建成功
- 部署脚本：`deploy-exit=0`
- 生产容器：`wbsysc-gateway` healthy
- 生产健康检查：正常，MCP 新旧服务兼容层维持禁用
- 公网检查：`/health`、`/admin/ui/`、`/tenant/ui/` 均返回 200
- 前端产物：持久复制 MCP 配置提示已存在
- 数据库结构：`connection_token.encrypted_token` 字段存在（count=1）
- MCP SDK：1.28.1
- 安全验证：本次未执行外部渗透/安全测试；功能相关自动化测试已包含在 CI 测试中
