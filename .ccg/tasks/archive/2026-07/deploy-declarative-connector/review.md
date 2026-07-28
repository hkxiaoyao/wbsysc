# 部署复核

- 应用版本：`9b3e116`
- GitHub Actions：测试与镜像构建均通过（run `30364884204`）
- 数据库迁移：`004` 至 `011` 执行成功，`declarative_record` 表存在
- 迁移凭据：使用独立最小权限账户，凭据仅保存在服务器 root 私有配置文件中
- 部署方式：GHCR 拉取超时后由部署脚本回退为服务器本地构建
- 容器状态：`wbsysc-gateway` 为 `running / healthy`
- 内部健康检查：`/health` 返回 `status=ok`，生产模式且未启用 mock
- 公网验证：`https://wbsysc.hacka.cn/health`、`/admin/ui/`、`/tenant/ui/` 均返回 200
- MCP 兼容服务：按现有生产配置保持禁用
- 安全测试：遵照用户权限说明未执行安全扫描或渗透测试

## 结论

部署成功，管理员后台与租户后台可通过正确生产域名访问。
