# 需求：增强声明式连接器能力

## 背景

声明式连接器（`connector_key = "http_declarative"`）已实现 OpenAPI 3 受限子集导入、
1~N 步编排、SSRF 安全边界和修订版生命周期。但在模型层、导入层、执行层之间存在
四处「已声明但未执行」的断裂：契约对外宣告了某项能力，运行时并不兑现。

## 已验证的四处断裂

### D1 分页：模型完整，导入拒绝，执行缺失

- `app/connectors/declarative/models.py:658-691` 的 `PaginationPolicy` 定义完整，
  含 `max_pages` / `max_items` / `items_pointer` / `next_pointer` / `next_query_param`
  及全套校验。
- `app/connectors/declarative/validator.py:597-604` 的 `_pagination()` 遇到
  `x-pagination` 直接 `raise SpecValidationError("pagination is not supported")`，
  永远返回 `None`。
- `app/connectors/declarative/connector.py:430-468` 的 `_execute_operation()`
  只发一次请求。
- `app/connectors/declarative/http_client.py:415-439` 的 `request()` **已经**接受并
  校验 `page_count` 参数（上限 `self._max_pages`），调用方从未传过大于 1 的值。

**后果**：任何分页 API 只能取回第一页，声明式连接器无法读全量数据。

### D2 响应缓存：实现完整，生产零引用

- `app/connections/cache.py` 368 行 `ConnectionCache` 实现完整：TTL、
  参数规范化哈希（`normalized_args_hash`）、敏感值拒绝（`_contains_sensitive_value`）、
  inflight 合并（`get_or_load`）。
- 该模块**仅被 2 个测试文件 import**（`tests/test_connection_sync.py:12`、
  `tests/test_mcp_connection_isolation.py:12`），生产代码零引用。
- `x-cache-ttl-seconds` → `DeclarativeOperation.cache_ttl_seconds` →
  `ToolSpec.cache_ttl_seconds`（`app/connectors/runtime.py:66`）之后链路中断。
- 企微连接器同样受影响：`app/connectors/wecom.py:41` 的 ToolSpec 默认
  `cache_ttl_seconds=60`，同样从未生效。

**后果**：声明的缓存 TTL 完全无效，每次 MCP 调用都打上游。

### D3 OAuth2 token 每次请求都重新获取

- `app/connectors/declarative/connector.py:443` 在 `_execute_operation()` 内部调用
  `_auth_headers()`，即**每个 step 的每次执行**都走一遍。
- `_auth_headers()` 的 `oauth2_client_credentials` 分支（`connector.py:516-549`）
  每次都 POST `token_url` 换新 token，无任何缓存。
- `DeclarativeConnector` 由 `provider.connect()` 每次请求新建
  （`app/connectors/declarative/provider.py:106-119`），实例级缓存无效。

**后果**：一个 3 步编排工具 = 3 次 token 请求 + 3 次业务请求；上游 token 端点
通常有严格频率限制，且响应时延翻倍。

### D4 stored 模式对外宣告可用，实际不落库

- `SyncSpec`（`models.py:733-751`）完整校验 `primary_key_pointer` 与
  `field_mappings`，且 `models.py:1137-1158` 强制这些指针必须已在
  sync 操作的 `output_mappings` 中声明。
- `models.py:1266` 据此对外宣告 `supports_data_modes` 含 `stored`。
- 但 `connector.py:470-482` 的 `sync()` 只是 `execute(operation_key, {})` 后原样返回，
  **完全未使用 `primary_key_pointer` 和 `field_mappings`**。
- 上层 `app/connections/sync.py:322-338` 拿到 `SyncResult` 后
  **只写审计日志（`_write_event`），不写任何业务表**。

**后果**：租户可以把声明式连接设为 `stored` 模式并通过所有校验，但数据不落库；
MCP 读取仍然实时打上游，与 `stored` 语义不符。

## 验收标准

1. **D1**：带 `x-pagination` 的 OpenAPI 文档可导入；执行时按声明的 cursor 协议
   翻页，受 `max_pages` / `max_items` / `MAX_OUTPUT_BYTES` 三重约束；翻页请求
   只改 `next_query_param` 一个 query 参数，不跟随任意 next link。
2. **D2**：`cache_ttl_seconds > 0` 的**只读**工具在 TTL 内命中缓存，不打上游；
   缓存按 `(connection_id, tool_key, args_hash)` 隔离；连接配置变更后失效。
3. **D3**：同一连接在 token 有效期内复用 OAuth token；多步工具只换一次 token；
   token 按连接隔离，凭证轮换后立即失效。
4. **D4**：`stored` 模式下同步任务将映射后的字段落入按 `connection_id` 隔离的
   中心记录表；MCP 读取走本地表；重复同步幂等（UPSERT）；断点续传。

## 安全不变量（不得破坏）

- 绝不持久化或缓存上游原始响应体，只保留 `output_mappings` / `field_mappings`
  投影后的字段。
- 绝不将凭证、token、Authorization 头写入 `ConnectionCache` 或业务表。
- 翻页与同步的每一跳都必须走 `SafeHttpClient`，保留 HTTPS / allowlist /
  DNS-IP 锁定 / 重定向 / 请求响应大小 / 超时全部现有控制。
- 租户与连接隔离：所有缓存键、token 键、数据表行都必须带 `connection_id`。
- 已发布修订版不可变；分页声明变更必须产生新修订版。

## 明确不做

- 不实现跟随任意 `next` 链接的分页（只支持预声明的 cursor 参数）。
- 不引入 Redis（`ConnectionCache` 与 token 缓存均为进程内）。
- 不为声明式连接器做动态建表（用固定的通用记录表）。
- 不改变企微连接器现有的表结构和同步逻辑。
- 不实现 Webhook、智能表格或其他新数据域。
