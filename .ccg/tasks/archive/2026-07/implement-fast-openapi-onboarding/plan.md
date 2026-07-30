# OpenAPI 极速接入第一期实施计划

## 接口契约

- `POST {connectionBase}/openapi/analyze`
  - 输入：`document`（JSON/YAML 解析后的对象或原始文本）。
  - 服务端自动生成不可冲突的 `spec_id` 与修订号，完成导入、验证和发布。
  - 返回：分析标识、连接配置版本、摘要、凭据 schema、工具预览和只读/写分类；不返回凭据。
- `POST {connectionBase}/openapi/activate`
  - 输入：分析标识/摘要/预期版本、凭据对象、显式确认启用的写工具列表。
  - 服务端校验分析结果仍是当前待发布修订，生成完整工具策略（读默认开、写默认关），保存凭据，执行真实无参只读测试，最后激活。
  - 任一步失败都不得激活；错误信息保持通用且不泄露凭据或上游响应。
- 管理员基址：`/admin/connections/{connection_id}`。
- 租户基址：`/tenant/connections/{connection_id}`，通过现有租户适配器转交同一用例并保持认证、同源和租户边界。

## Layer 1（并行）

### 后端编排与测试

文件归属：

- `app/admin_connections.py`
- `tests/test_admin_connections.py`

步骤：

1. 新增严格请求模型、稳定摘要函数和安全的分析响应。
2. 复用现有导入/发布/凭据/策略/测试/激活能力，实现快速分析与激活用例。
3. 以当前连接版本、待发布 spec/revision 与分析摘要防止竞态和重放。
4. 写工具必须逐项显式确认；未知工具或把读工具放入写确认列表时拒绝。
5. 增加管理端路由与用例级测试，覆盖默认策略、真实测试门槛、失败不激活、敏感信息不回显。

### 前端两步流程与测试

文件归属：

- `admin-ui/src/pages/OpenApiQuickOnboarding.jsx`
- `admin-ui/src/pages/openApiQuickOnboarding.js`
- `admin-ui/src/pages/openApiQuickOnboarding.test.js`
- `admin-ui/src/pages/Connections.jsx`
- `admin-ui/src/pages/DeclarativeSpecWizard.jsx`

步骤：

1. 实现上传/粘贴 JSON/YAML 的第一步，调用快速分析接口并显示工具摘要。
2. 第二步按后端 schema 收集凭据；读工具默认展示启用，写工具默认关闭并要求显式确认。
3. 激活成功后刷新连接；请求结束后清空前端凭据状态，不读取既有凭据。
4. 快速接入对管理端和租户端均可见；现有高级向导默认折叠，并改造成使用注入的 scope/apiClient。
5. 用纯函数测试覆盖端点生成、规范解析、策略/请求构建与凭据清理相关逻辑。

## Layer 2

### 租户适配器与测试

文件归属：

- `app/tenant_connections.py`
- `tests/test_tenant_connections.py`

步骤：

1. 增加租户快速分析与激活路由，复用管理端同一用例。
2. 保持现有 `_mutation_principal`、租户所有权与同源校验。
3. 覆盖未认证、跨租户、非同源和正常流程适配测试。

## 集成与验收

1. 运行后端定向测试与前端 Node 测试。
2. 运行完整后端测试、前端测试和构建/类型检查。
3. 检查 git diff 范围、无硬编码密钥、无凭据回显。
4. Gemini 与 Claude 双模型审查；无法调用的一方记录环境原因，另一方发现的问题必须处理。
5. 更新 review、归档 CCG 任务并提交。

## 审查阶段追加修复

- `app/connectors/runtime.py`、`tests/test_connection_cache_runtime.py`：为连接测试增加显式缓存绕过能力；仍执行策略、限流、超时和审计，但快速激活前的验证必须真实访问连接器，不能命中历史读缓存。
- 管理端快速分析/激活路由显式执行同源校验；租户端继续使用 `_mutation_principal`。
- 凭据 schema 使用 provider 批准后的 required/additionalProperties 约束，避免前端将必填凭据误判为可选。
- 每个编排写阶段要求配置版本严格增加 1，阶段间任何并发修改都终止激活。
