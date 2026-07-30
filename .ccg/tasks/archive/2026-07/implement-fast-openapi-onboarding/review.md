# OpenAPI 极速接入第一期审查报告

## 结论

通过。无未修复 Critical 或 Warning。

## 已修复发现

### Critical：激活前测试可能命中历史读缓存

- 原因：普通 runtime execute 对声明了缓存 TTL 的 read 工具可返回缓存结果，不能证明新修订和新凭据真实可用。
- 修复：`ConnectorRuntime.execute` 增加默认关闭的 keyword-only `bypass_cache`；快速激活测试显式开启。策略、写权限、限流、超时和审计仍完整执行，仅绕过数据缓存。
- 回归：增加缓存已命中后显式 bypass 仍真实调用 connector 的测试。

### Critical：管理端快速写接口缺少显式同源校验

- 修复：管理端 analyze/activate 在连接 lookup 前调用 `require_same_origin`。
- 回归：覆盖无 Origin 与跨站 Origin，确认 403 且不触发连接查询。

### Warning：编排阶段间可能吸收并发配置变更

- 修复：保存凭据和保存策略后，分别要求 config version 严格增加 1；任何额外递增均返回 409，测试与激活不再继续。

### Warning：原始编译凭据 schema 未标记 required

- 修复：analyze/activate 使用 provider 批准后的 candidate spec，向前端返回 required/additionalProperties，并在后端复用同一批准 schema。

### Info：声明式连接未默认进入快速入口

- 修复：打开 `http_declarative` 连接时默认选择 OpenAPI 接入页；高级向导保留在折叠区。

### Info：前端 YAML 预检过严

- 修复：JSON 文件仍严格本地解析，其他文本交由后端受限 safe YAML loader 验证，兼容合法流式 YAML 和引号键。

## 测试与质量

- Python 全量：1454 passed, 1 skipped。
- 前端 Node 全量：134 passed。
- Vite production build：通过；仅保留既有大 chunk 提示。
- Ruff 阻断规则：通过。
- Python compileall：通过。
- `git diff --check`：通过；仅 Windows LF/CRLF 提示。

## 审查环境说明

- Gemini 调用失败：环境缺少 `GEMINI_API_KEY`。
- Claude 外部包装器长时间运行后异常退出，未产出报告。
- 已使用独立 `ccg-review` 代理完成替代交叉审查，并修复上述两个 Critical；主代理另行完成并发、凭据 schema、YAML 与默认入口复核。

## 非阻断后续项

- 前端当前以纯函数和源码契约测试为主，可在后续引入 React 交互测试覆盖上传、中止请求和失败后清理。
- 快速编排目前由分层测试覆盖；后续可增加真实数据库 + provider + mock upstream 的单条端到端用例。
