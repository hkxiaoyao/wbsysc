# 部署验收记录

## 交付版本

- 功能提交：`f865d31 feat: add one-click MCP config copy`
- 依赖修复：`0d04ed7 fix: pin compatible MCP SDK`
- 生产运行版本：`0d04ed7`

## 验证结果

- 前端测试：122/122 通过，生产构建通过。
- 后端测试：1400 通过、1 跳过。
- GitHub Actions：运行 `30378124195` 的 test 与 build-push 均通过。
- 数据库迁移：004–011 全部成功。
- 部署脚本：返回 `deploy-exit=0`。
- 容器：`wbsysc-gateway` 为 healthy。
- 内部健康检查：`http://127.0.0.1:8001/health` 返回 ok。
- 公网检查：`/health`、`/admin/ui/`、`/tenant/ui/` 均返回 HTTP 200。
- 生产静态包：包含“一键生成并复制 MCP 配置”。
- 容器 MCP SDK：`1.28.1`。
- MCP 兼容服务：沿用现有配置，保持禁用。

## 部署说明

- GHCR 拉取因服务器外网超时，部署脚本按设计回退到本地构建并成功切换容器。
- Windows 本机公网请求受 Schannel `SEC_E_NO_CREDENTIALS` 影响；公网状态改由生产服务器侧独立请求确认。
- 按用户权限说明，本次未执行安全测试。
- 外部审查中 Claude 通过；Gemini 因环境缺少 `GEMINI_API_KEY` 未能运行。
