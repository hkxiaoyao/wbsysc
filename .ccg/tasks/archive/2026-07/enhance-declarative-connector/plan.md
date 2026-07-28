# 实施计划：增强声明式连接器能力

## 目标态

声明式连接器的模型层声明与运行时行为完全一致：声明了分页就会翻页，声明了
缓存 TTL 就会缓存，声明了 OAuth 就会复用 token，宣告支持 `stored` 就会落库。

## 执行顺序与依赖

```
Layer 1 (D3 token 缓存)  ──┐
                           ├──> Layer 2 (D1 分页) ──> Layer 4 (D4 stored 落库)
Layer 3 (D2 响应缓存) ─────┘
```

- **D3 先做**：分页循环会放大 token 请求次数，先修复 token 缓存再加翻页。
- **D1 在 D4 之前**：`stored` 同步要拉全量数据，必须先具备翻页能力。
- **D2 独立**：可与 D1 并行，但落在 runtime 层，需先确认不与 D1 的输出结构冲突。

---

## Layer 1：OAuth token 缓存（D3）

### 1.1 新增连接维度 token 缓存

新建 `app/connectors/declarative/token_cache.py`：

- 进程内缓存，键为 `(connection_id, token_url, client_id_key, scopes)` 的哈希，
  **不以凭证明文入键**。
- 值为 `(token, expires_at_monotonic)`；解析上游 `expires_in`，
  提前 60 秒过期；上游未返回 `expires_in` 时按保守默认（300 秒）。
- `asyncio.Lock` 保护，同连接并发只换一次 token（参考 `ConnectionCache._inflight`
  的 inflight 合并做法，避免惊群）。
- 提供 `invalidate(connection_id)`，供凭证轮换与连接配置变更调用。

### 1.2 接线

1. `connector.py:_auth_headers()` 的 `oauth2_client_credentials` 分支改为
   先查缓存，未命中再走现有换取逻辑，成功后写入缓存。
2. 上游返回 401 时使该连接 token 失效并**最多重试一次**，避免 token 提前失效
   导致整个工具调用失败；重试不改变现有 fail-fast 语义（第二次仍失败即抛出）。
3. `app/connections/store.py` 中凭证轮换、连接停用/删除的提交点调用
   `invalidate(connection_id)`。复用 `mcp_gateway.py:330`
   `register_connection_cache_invalidator` 的既有失效通道，不新建机制。

### 1.3 测试

- 同一连接连续两次调用只发一次 token 请求。
- 多步编排工具（3 步）只换一次 token。
- token 过期后自动重新换取。
- 不同 `connection_id` 不共享 token。
- 凭证轮换后立即失效。
- 上游 401 触发一次失效重试，第二次失败不再重试。

---

## Layer 2：声明式分页（D1）

### 2.1 导入层

`app/connectors/declarative/validator.py`：

1. `_pagination()` 由「一律拒绝」改为真正解析 `x-pagination`，构造 `PaginationPolicy`。
2. 校验项（失败一律 `SpecValidationError`）：
   - `items_pointer` / `next_pointer` 必须被响应 schema 声明——复用现有
     `_schema_has_pointer()`，与 `_output_mappings()` 同一套判定。
   - `next_query_param` 必须匹配 `_IDENTIFIER_RE`，且**不得与任何已声明的
     query 位 `InputMapping.target` 冲突**（防止翻页覆盖用户输入）。
   - `max_pages` ≤ `MAX_PAGE_COUNT`(10)、`max_items` ≤ `MAX_PAGE_LIMIT`(1000)，
     由 `PaginationPolicy.__post_init__` 兜底。
   - 仅 `GET` 操作允许分页；非 GET 声明分页直接拒绝。
3. `_compile_operation()`（`validator.py:726-741` 附近）把解析结果挂到
   `DeclarativeOperation.pagination`（字段已存在，`models.py:879`）。

### 2.2 执行层

`app/connectors/declarative/connector.py:_execute_operation()` 增加翻页循环：

- 第 1 页走现有路径；`pagination is None` 时行为与现在完全一致（零回归）。
- 后续页：在**已构造的 URL** 上替换/追加 `next_query_param=<cursor>` 这一个参数，
  其余部分不变；cursor 取自上一页响应的 `next_pointer`。
- 每次请求向 `client.request()` 传递递增的 `page_count`（该参数与校验已存在于
  `http_client.py:434-439`），由客户端强制页数上限。
- 终止条件（任一满足即停）：`next_pointer` 取不到值 / 达到 `max_pages` /
  累积条目数达 `max_items` / 累积输出超 `MAX_OUTPUT_BYTES`。
- **auth headers 在循环外求值一次**，全部页复用（依赖 Layer 1）。
- 单页超时沿用 `timeout_ms`；整体受 `MAX_TOOL_TIMEOUT_MS`(60s) 约束，
  超时按现有 `_StepExecutionFailure("timeout")` 处理。
- 输出合并：`items_pointer` 指向的数组跨页拼接，其余 `output_mappings`
  取**第一页**的值（避免末页的分页元数据覆盖业务字段）。

### 2.3 测试

新建 `tests/test_declarative_pagination.py`：

- 带 `x-pagination` 的文档可导入并生成 `PaginationPolicy`。
- `next_query_param` 与已声明 query 输入冲突时拒绝导入。
- `items_pointer` / `next_pointer` 未在响应 schema 声明时拒绝导入。
- 非 GET 操作声明分页时拒绝导入。
- 执行：3 页数据完整拼接；`max_pages` 截断；`max_items` 截断；
  输出超 `MAX_OUTPUT_BYTES` 截断。
- 翻页 URL 只有 cursor 参数变化，host 不变（SSRF 边界回归）。
- 未声明分页的操作仍只发一次请求。

---

## Layer 3：响应缓存接线（D2）

### 3.1 接线位置：runtime 层（而非 declarative 层）

理由：`ToolSpec.cache_ttl_seconds` 是 `app/connectors/contracts.py` 的通用契约字段，
企微连接器（`wecom.py:41` 默认 60 秒）同样声明了它。放在
`app/connectors/runtime.py` 可让两类连接器一次性受益，符合 DRY。

### 3.2 实施

1. `ConnectionExecutionRuntime` 持有一个 `ConnectionCache` 实例，生命周期绑定
   app lifespan。
2. 在 `runtime.py:465-475 _execute_with_data_mode()` 外包一层缓存：
   - **仅当** `tool.operation_kind == "read"` 且 `tool.cache_ttl_seconds` 为正整数
     且 `context.data_mode != "stored"` 时启用（`stored` 模式读本地表，无需再缓存）。
   - 用 `ConnectionCache.get_or_load()` 合并并发相同请求。
   - 仅缓存 `status == "ok"` 的结果；`partial` / `error` 一律不缓存。
3. 缓存键由 `ConnectionCache._key()` 生成，已含 `connection_id` + `tool_key` +
   参数规范化哈希，租户隔离由此保证。
4. 失效：复用 `mcp_gateway.py:330` 的连接失效通道，在连接配置版本变更、
   凭证轮换、工具策略变更时清除该连接条目。
5. 保留 `ConnectionCache.put()` 现有的敏感值拒绝行为（`cache.py:262`）——
   含敏感字段的结果静默不缓存，不报错、不降级功能。

### 3.3 测试

新建 `tests/test_connection_cache_runtime.py`：

- 只读工具在 TTL 内第二次调用不触达连接器。
- TTL 过期后重新执行。
- 写工具（`operation_kind == "write"`）永不缓存。
- `error` / `partial` 结果不缓存。
- 不同 `connection_id` 不串数据；不同参数不串数据。
- 连接配置变更后缓存失效。
- 含敏感字段的结果不进缓存且调用正常返回。
- 企微连接器同样命中缓存（验证通用性）。

---

## Layer 4：stored 模式落库（D4）

### 4.1 存储结构

在中心连接数据库新增**固定结构**的通用记录表（不做动态建表），所有读写都由
服务端强制携带 `connection_id`：

```sql
CREATE TABLE IF NOT EXISTS `declarative_record` (
  connection_id VARCHAR(64) NOT NULL,
  resource_key VARCHAR(128) NOT NULL,
  record_key VARCHAR(255) NOT NULL,
  payload_json TEXT NOT NULL,
  synced_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY(connection_id, resource_key, record_key),
  KEY idx_cr_time(connection_id, resource_key, synced_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
```

- `record_key` 来自 `SyncSpec.primary_key_pointer`。
- `payload` **只存 `field_mappings` 投影后的字段**，绝不存原始响应体。
- 落库前对 `payload` 复用 `cache.py` 的敏感值检测，命中则跳过该行并计入
  `partial` 状态，不静默写入。

### 4.2 迁移

1. 新增 `sql/011_declarative_record.sql`（幂等 `CREATE TABLE IF NOT EXISTS`）。
2. `app/connections/store.py` 的中心连接表 DDL 加入同一结构，保证新环境初始化
   与存量环境迁移一致。
3. `README.md` 与 `deploy/server_deploy.sh` 的迁移序列追加 `011`。
4. `tests/test_migrations.py` 补 `011` 的顺序断言。

### 4.3 同步写入路径

1. `connector.py:sync()` 改为：
   - 调用 sync 操作（经 Layer 2 翻页拉全量）。
   - 用 `SyncSpec.field_mappings` 投影字段、`primary_key_pointer` 取主键。
   - 返回结构化的 `SyncResult`，`data` 内含待落库的记录集合（已投影，无原始体）。
2. `app/connections/sync.py:322-338` 在现有 `_write_event()` 审计之外，
   新增落库步骤：按 `uk_crr` 做幂等 UPSERT。
3. 游标：复用中心 `connection_sync_state` 表，以连接和资源摘要组成状态键；每页
   投影成功落库后才推进游标，完成后删除游标，支持断点续传。
4. 并发保护：复用 `db.py:145 connection_sync_lock` 的 advisory lock，
   同一连接同一资源不并发同步。
5. 单条记录失败不中断整批，落库结果为 `partial`；整批失败按现有
   `sync_failed` 处理，不泄漏上游内容。

### 4.4 读取路径

`connector.py:execute()` 增加分支：`context.data_mode == "stored"` 时，
声明式工具从 `declarative_record` 读取而非请求上游。

- 仅当该工具的操作是 `sync_spec.operation_key` 对应的读操作时走本地表；
  其余工具在 `stored` 模式下仍走直连（与企微连接器
  `stored` 语义保持一致：只有被同步的资源才有本地副本）。
- 查询强制带 `connection_id` + `resource_key` 前缀条件，租户输入无法覆盖服务端
  注入的连接范围。

### 4.5 测试

新建 `tests/test_declarative_stored_mode.py`：

- 同步把投影后的字段写入 `declarative_record`。
- 重复同步幂等（同 `record_key` 不产生重复行）。
- `payload` 不含未在 `field_mappings` 声明的字段。
- 含敏感值的记录被跳过且整批标记 `partial`。
- `stored` 模式下 MCP 读取走本地表，不发出上游请求。
- 未配置 `sync_spec` 的连接仍拒绝进入 `stored` 模式（现有行为不回归）。
- 不同连接的数据互不可见（隔离回归）。
- 游标断点续传。

---

## Layer 5：验证与交付

1. **先写失败测试再改实现**（每层内部遵循 TDD）。
2. 每层完成后跑该层聚焦测试，全部 4 层完成后跑完整 pytest。
   - 注意：当前仓库测试会读开发机 `.env`，本机 `APP_ENV=prod` 会导致 12 个模块
     收集失败。执行时用隔离环境变量运行：
     `APP_ENV=dev WECOM_USE_MOCK=true CREDENTIAL_KEY= ADMIN_PASSWORD= DB_PASSWORD= pytest -q`
   - 迁移测试必须 mock 数据库初始化边界，不允许读取开发机远程 MySQL 配置。
3. 前端无改动，但如果 `DeclarativeSpecWizard.jsx` 需要展示分页声明，
   补 `admin-ui/src/pages/*.test.js` 并用 `node --test "src/pages/*.test.js"` 验证
   （当前 118 个用例全过）。
4. 更新文档：`README.md` 声明式连接器能力说明、
   `docs/connection-platform-operations.md` 的 `stored` 模式运维说明。
5. 自检：Ruff + 完整 pytest + 归档任务记录。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 分页循环放大上游调用与时延 | 三重上限（页数/条目/字节）+ 工具级 60s 总超时；`page_count` 由 `SafeHttpClient` 强制 |
| 翻页被用于绕过 SSRF 边界 | 只允许改一个预声明的 query 参数，不跟随任意 next link；每跳仍走 `TargetGuard` |
| 缓存跨租户串数据 | 缓存键强制含 `connection_id`；补隔离回归测试 |
| 缓存导致数据陈旧 | 仅缓存只读工具；连接配置变更即失效；TTL 由声明控制且上限 86400 秒 |
| token 缓存泄漏到日志/审计 | token 只进独立缓存，不进 `ConnectionCache`、不进审计字段；键不含明文凭证 |
| 新增表影响存量 schema | DDL 幂等且同时进 `_BIZ_DDLS`；迁移脚本独立编号 `011`，失败即终止部署 |
| `stored` 落库写入原始响应体 | 只写 `field_mappings` 投影；落库前敏感值检测；补断言 payload 字段集合 |

## 明确不做

- 不实现跟随任意 `next` 链接的分页。
- 不引入 Redis；缓存与 token 缓存均为进程内，多实例各自持有。
- 不为声明式连接器做动态建表或按 spec 生成专用表。
- 不改变企微连接器现有表结构与同步逻辑（仅让它顺带享受 Layer 3 的缓存）。
- 不连接或修改真实 MySQL；迁移测试仅验证隔离的 DDL/顺序行为。
- 不实现 Webhook、智能表格或其他新数据域。
