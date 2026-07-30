# 诊断与审查

## 生产证据

- 2026-07-30 21:32:14，生产访问日志记录 `POST /admin/tenants/tenant1/connections` 返回 401。
- 同路径最小复现返回 `{"detail":"未登录或会话过期"}`，与线上响应长度一致。
- OpenAPI 创建逻辑未执行外部探测；401 在管理认证边界产生。

## 根因

部署重启后，进程内管理会话失效。前端响应拦截器只清理 localStorage Token，没有同步 React 的 `authed` 状态，导致页面继续显示已登录界面，后续创建请求才暴露 axios 401。

## 修复与验证

- 管理 API 收到 401 时发布统一会话失效事件。
- 管理后台订阅事件并立即切回登录页。
- 回归测试覆盖 axios 401、Token 清理、事件发布/订阅及取消订阅完整链路。
- 前端：125 项通过，生产构建通过。
- 后端：1424 项通过，1 项跳过。
- Claude 审查无 Critical；提出的链路覆盖 Warning 已修复。
- Gemini 因本机未配置 `GEMINI_API_KEY` 无法执行审查。

## 发布验收

- 修复提交：`82752d4 fix(admin): redirect expired sessions to login`
- GitHub Actions：`30550561627`，test 与 build-push 均成功。
- GHCR 拉取超过 300 秒后自动回退到服务器本地缓存构建，16.1 秒成功完成。
- 部署脚本：`deploy-exit=0`。
- 生产容器：`wbsysc-gateway` healthy。
- 生产版本：`82752d4`，前端产物包含 `admin-session-expired`。
- 公网 `/health`、`/admin/ui/`、`/tenant/ui/` 均返回 200。
- 未认证 OpenAPI 创建仍按预期返回 401，认证边界未放宽。

## 未执行

- 未执行外部渗透或安全测试。
