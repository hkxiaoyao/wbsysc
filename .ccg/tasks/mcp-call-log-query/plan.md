# MCP 调用日志管理执行计划

完整实施计划：`docs/superpowers/plans/2026-07-14-mcp-call-log-management.md`

## 执行层

- Layer 1：中心日志存储/迁移；前端查询视图模型。
- Layer 2：审计埋点；管理 API；日志工作台页面。
- Layer 3：后端运行时与定时清理；前端导航与租户快捷入口。
- Layer 4：完整测试、迁移验证、浏览器冒烟和双模型审查。

## 文件归属

并行任务必须遵守完整计划中的文件归属，同一时刻不得由两个代理修改同一文件。

## 验收

- `pytest -q`
- `node --test src/pages/tenantsView.test.js src/pages/mcpLogsView.test.js`
- `pnpm run build`
- `antd lint src/App.jsx src/pages/Tenants.jsx src/pages/McpLogs.jsx --format json`
- 桌面和 390 像素窄屏浏览器验证
