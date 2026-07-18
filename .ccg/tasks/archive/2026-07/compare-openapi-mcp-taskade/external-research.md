# 外部一手资料研究：openapi-mcp-generator 与 taskade/mcp

核对日期：**2026-07-18（Asia/Shanghai）**。仅使用两个项目自己的 GitHub 仓库 README、源码、包元数据和 GitHub 仓库元数据。为避免主分支后续变化，源码链接固定到核对时的 commit。

## 明确结论

两者并非同类替代品：**`openapi-mcp-generator` 是“输入 OpenAPI、输出完整可运行 MCP 代理项目”的通用脚手架生成器；`taskade/mcp` 首先是 Taskade 官方的现成 MCP 服务，同时附带一个“输入已解析 OpenAPI 文档、输出工具注册 TypeScript”的较底层代码生成库。** 若目标是一次生成独立项目，前者更直接；若目标是连接 Taskade 或把生成器嵌入既有 TypeScript MCP 服务，后者更贴合。

## 1. harsha-iiiv/openapi-mcp-generator

核对基线：[`8f6714f`](https://github.com/harsha-iiiv/openapi-mcp-generator/tree/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3)，仓库创建于 2025-03-09；核对时 `package.json` 版本 **4.0.1**、MIT、Node.js `>=20`，约 621 stars / 87 forks。来源：[仓库](https://github.com/harsha-iiiv/openapi-mcp-generator)、[package.json](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/package.json)、[GitHub API 元数据](https://api.github.com/repos/harsha-iiiv/openapi-mcp-generator)。

### 目标、输入与输出

- 目标是把 OpenAPI 3.0+ 规范转换为一个 MCP 服务，使 MCP 客户端通过生成的工具代理调用原 REST API；生成代码包含运行时参数校验、上游认证和多种传输。[README](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/README.md)
- CLI 输入为本地路径或 URL 上的 YAML/JSON OpenAPI 文档；必须指定输出目录，可覆盖服务名、版本、base URL、传输、端口和 `x-mcp` 默认筛选行为。[CLI 源码](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/src/index.ts)
- 输出是完整 TypeScript/Node 项目：`src/index.ts`、`package.json`、`tsconfig.json`、lint/format/test 配置、`.env.example`；Web/Streamable HTTP 模式还生成传输入口和浏览器测试页。它不是只返回一组动态工具。[生成流程](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/src/index.ts)
- 另有程序化 API `getToolsFromOpenApi()`：输入路径、URL 或 `OpenAPIV3.Document`，输出 `McpToolDefinition[]`，包含名称、JSON Schema、HTTP 方法、路径、参数位置、安全要求和 base URL；可按 operationId 或回调过滤。[PROGRAMMATIC_API.md](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/PROGRAMMATIC_API.md)、[api.ts](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/src/api.ts)

### 运行方式与架构

- 安装/生成：`npm install -g openapi-mcp-generator`，再执行 `openapi-mcp-generator --input <spec> --output <dir>`；生成后 `npm install && npm run build`，按传输使用 `npm start`、`npm run start:web` 或 `npm run start:http`。[README](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/README.md)
- 架构分为：安全解析 OpenAPI → 从 path/operation 抽取 MCP 工具与输入 schema → 模板生成 MCP server/上游 HTTP 执行代码 → 生成 transport 与项目配置。源码目录也按 `parser/`、`generator/`、`utils/`、`types/` 分层。[src](https://github.com/harsha-iiiv/openapi-mcp-generator/tree/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/src)
- 传输支持 stdio、Hono SSE Web 和 Streamable HTTP；认证配置从生成服务的环境变量读取，覆盖 API key、Bearer、Basic、OAuth2。[README](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/README.md)、[server-code.ts](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/src/generator/server-code.ts)

### 成熟度与限制

- 有 Vitest 单元/集成测试、CI 检查和 npm 发布 workflow，包已到 4.0.1；但 GitHub Releases API 在核对时无 release 条目，版本信息主要来自 npm/package metadata 与 changelog。[tests](https://github.com/harsha-iiiv/openapi-mcp-generator/tree/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/tests)、[check.yml](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/.github/workflows/check.yml)、[CHANGELOG](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/CHANGELOG.md)
- 明确约束：Node.js 20+；面向 OpenAPI 3.0+；规范缺失或包含多个 server 时可能必须显式给 `--base-url`；远程外部 `$ref` 默认拒绝以降低 SSRF 风险，必须显式 `--allow-external-refs` 才允许；生成结果仍需安装依赖并构建。[README](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/README.md)、[parser-security.ts](https://github.com/harsha-iiiv/openapi-mcp-generator/blob/8f6714ffcdeefbcddea25d8143dc610e4cbbc5f3/src/utils/parser-security.ts)

## 2. taskade/mcp

核对基线：[`24f491b`](https://github.com/taskade/mcp/tree/24f491bd08202489d481b8a45b9bf999e02bd559)，仓库创建于 2025-05-26；核对时约 153 stars / 46 forks，MIT。仓库根是私有发布用 monorepo 元包（0.0.3），实际公开包为 `@taskade/mcp-server` **0.1.1** 和 `@taskade/mcp-openapi-codegen` **0.0.5**。来源：[仓库](https://github.com/taskade/mcp)、[根 package.json](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/package.json)、[server package.json](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/packages/server/package.json)、[codegen package.json](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/packages/openapi-codegen/package.json)。

### 目标、输入与输出

- 首要产品是 **Workspace MCP**：把 Claude、Cursor、Windsurf、VS Code 等 MCP 客户端连接到 Taskade workspace，暴露 README 所列 62 个 workspace/project/task/agent/webhook 等工具。[README](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/README.md)
- 现成服务输入是 `TASKADE_API_KEY`（个人访问令牌）和 MCP tool call；输出是对 Taskade API v1/v2 的调用结果，部分响应附加可供模型展示的 Taskade 链接提示。[server.ts](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/packages/server/src/server.ts)
- 附带的通用 codegen 输入是调用方先用 `@readme/openapi-parser` 解引用得到的 OpenAPI 文档（类型也接受 OAS 2、3、3.1）；输出是一个 TypeScript 字符串，并可写入指定文件。生成内容是向既有 `McpServer` 注册工具的 `setupTools()` 函数及请求 runtime，**不是完整独立项目脚手架**。[codegen README](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/packages/openapi-codegen/README.md)、[codegen.ts](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/packages/openapi-codegen/src/codegen.ts)

### 运行方式与架构

- 本地 stdio：MCP 客户端以 `npx -y @taskade/mcp-server` 启动，并注入 `TASKADE_API_KEY`。远程/自定义客户端可用 `npx @taskade/mcp-server --http`，默认监听 3000，并通过 `/sse` 连接。[README Quick Start](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/README.md#quick-start)
- codegen 作为开发依赖嵌入脚本：`dereference(spec)` 后调用 `codegen({ path, document })`。Taskade 自己的 server build 会拉取 v1 YAML/v2 JSON 规范，分别生成两套工具，再构建 CLI。[server package.json](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/packages/server/package.json)
- 架构是 Yarn/Lerna monorepo：`packages/openapi-codegen` 负责 OpenAPI path/schema 解析、JSON Schema→Zod、工具注册代码和 HTTP runtime；`packages/server` 组合 v1/v2 生成工具、Taskade bearer auth、stdio/HTTP 入口。[目录](https://github.com/taskade/mcp/tree/24f491bd08202489d481b8a45b9bf999e02bd559/packages)、[codegen.ts](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/packages/openapi-codegen/src/codegen.ts)、[server.ts](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/packages/server/src/server.ts)

### 成熟度与限制

- 官方组织维护，有 CI、Changesets、SECURITY.md；codegen 包包含 parser/runtime/`allOf` 测试。不过公开包仍是 0.x，README roadmap 仍列出自动化工具与 agent toolkit 等未完成项，v2 agent chat/webhook 层标为 beta。[CI](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/.github/workflows/ci.yml)、[测试](https://github.com/taskade/mcp/tree/24f491bd08202489d481b8a45b9bf999e02bd559/packages/openapi-codegen/src)、[Roadmap](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/README.md#roadmap)
- Workspace MCP 与 hosted Genesis App MCP、in-product MCP Connectors 是三个不同表面，不能混为一谈；此仓库服务依赖 Taskade 账户/token 与在线 API，部分端点受套餐限制。[README 的选择表](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/README.md#which-taskade-mcp-do-i-want)
- 通用 codegen 是库而非 CLI 脚手架，调用者负责解析/解引用规范、提供既有 TypeScript MCP 工程和运行配置；仓库 README 宣称 OpenAPI 3.0+，源码类型范围更宽但不等于所有 OAS 2/3.1 特性均有完整兼容保证。[codegen README](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/packages/openapi-codegen/README.md)、[codegen.ts](https://github.com/taskade/mcp/blob/24f491bd08202489d481b8a45b9bf999e02bd559/packages/openapi-codegen/src/codegen.ts)

## 对照摘要

| 维度 | openapi-mcp-generator | taskade/mcp |
|---|---|---|
| 核心定位 | 通用 OpenAPI→完整 MCP 项目生成器 | Taskade 官方现成 MCP 服务 + 可复用 codegen 库 |
| 生成器输入 | 文件/URL/对象；CLI 自行解析 | 调用方提供已解析/解引用 document |
| 生成器输出 | 完整可构建项目，多 transport | 工具注册 TypeScript/字符串，嵌入既有服务 |
| 直接运行 | 先生成、安装、构建，再启动 | `npx @taskade/mcp-server` 即连 Taskade |
| 适用面 | 任意已描述的 REST API | 现成部分专注 Taskade；codegen 部分可用于任意 API |
| 当前信号 | 4.0.1、测试/CI，功能面较完整 | 官方维护、测试/CI，但两个实际包仍为早期 0.x |
