# Review

## Standards

- 本地审查：无 Critical。
- 修复：旧服务运行时存在时，完全移除管理员清理能力会导致旧 Token 无法撤销、服务无法停用。现保留隐藏的 `legacy_admin_router`，只允许只读盘点、停用、揭示和撤销 Token；禁止创建、改绑定、签发和重新激活。
- 修复：健康响应、Compose、部署脚本和运维文档统一以 `mcp_service_legacy_enabled` 作为兼容层状态契约；旧 `mcp_service_enabled` 响应字段仅保留兼容。
- 接受：服务前端模块仍保留部分历史测试与连接器卡片共享代码，但不再被管理后台或租户控制台挂载。
- 接受：FastAPI 当前版本对 `include_router` 使用延迟包装，路由注册测试需要读取 `original_router.routes`；运行时端点测试同时覆盖实际可达性。

## Spec

- 连接实例成为唯一用户可管理 MCP 边界：通过。
- tenant/admin 页面移除独立服务入口，旧链接回退连接实例：通过。
- tenant 服务管理 API 不再挂载：通过。
- 默认服务回填停止：通过。
- 旧服务运行时仅受兼容开关控制：通过。
- 可信域名、校验文件、连接 Token、工具策略和连接日志保持：通过。
- 跨连接服务和旧 Token 不静默迁移/删除：通过；新增只读盘点 SQL。

## External review

- Gemini：未执行成功，本机未配置 `GEMINI_API_KEY`。
- Claude：无 Critical；提出健康字段与部署脚本契约风险，已修复并增加测试。其余为死代码和历史 admin 日志筛选等 Minor，未影响当前目标。

## Verification

- Backend: `1334 passed, 1 skipped`。
- Frontend: `118 passed`。
- Production build: Vite build passed。
- Python compile: `python -m compileall -q app` passed。
- Compose: `docker compose config --quiet` passed。
- Diff: `git diff --check` passed。
- Shell syntax: 本机 WSL 缺少 `/bin/bash`，无法执行 `bash -n`；部署脚本由现有 Python 契约测试覆盖。
- 未执行安全扫描或联网安全测试。
