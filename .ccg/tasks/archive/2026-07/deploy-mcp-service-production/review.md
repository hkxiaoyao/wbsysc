# 生产部署审查结果

## 结果

通过。生产服务器已运行不可变镜像 `wbsysc:eb7302a`，容器健康且 MCP 服务已启用。

## 变更与保护措施

- 修复 Dockerfile 的 pnpm 版本漂移问题，固定使用 pnpm 10.34.5 和冻结锁文件安装。
- 部署前完成数据库压缩备份并校验 gzip 与 SHA-256。
- 固定旧镜像为 `wbsysc:rollback-pre-eb7302a`，新镜像使用提交标签 `wbsysc:eb7302a`。
- 使用一次性独立迁移账号执行 004 至 008；完成后立即删除。
- 为现有租户 schema 补齐运行时账号的最小项目要求权限。
- 先以 `MCP_SERVICE_ENABLED=false` 启动并验证，再原子切换为 `true`。
- 修正生产 `.env` 中三个不兼容的行内注释；密钥未回显或写入仓库。

## 验证证据

- Python：1316 passed，1 skipped。
- 前端：114 passed；Vite 生产构建通过。
- Docker 镜像构建成功；容器内应用导入成功。
- 运行时账号对中心库和租户业务表访问成功。
- 数据库迁移 004、005、006、007、008 全部成功。
- 关键表、非空别名列、唯一索引和租户业务表均存在。
- `/health` 在关闭态和启用态均返回 `status=ok`。
- 管理后台返回 HTTP 200。
- 旧 MCP 与服务 MCP 的未授权请求均返回 HTTP 401。
- 公网 `/health` 返回 HTTP 200，并显示 `mcp_service_enabled=true`。
- 容器 Docker health 为 `healthy`，近五分钟无 ERROR、CRITICAL 或 Traceback。

## 外部审查

- Claude 审查通过，并建议移除非冻结 pnpm 安装回退；已采纳。
- Gemini 分析与审查因执行环境未配置 `GEMINI_API_KEY` 无法运行，已记录为工具环境限制。

## 回滚点

- 数据库备份：服务器 `/root/backups/wbsysc/` 下的本次部署前备份。
- 应用镜像：`wbsysc:rollback-pre-eb7302a`。
- 发布包：`/root/app/releases/wbsysc-eb7302a`。
- 配置备份：`/root/app/wbsysc/.env.pre-eb7302a-*`。
