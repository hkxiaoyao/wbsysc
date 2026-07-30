import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  buildCredentialPayload,
  buildOpenApiActivationRequest,
  classifyOpenApiTools,
  credentialFields,
  emptyCredentialValues,
  normalizeOpenApiAnalysis,
  openApiQuickEndpoint,
  parseOpenApiDocument,
  requiredCredentialsPresent,
} from './openApiQuickOnboarding.js'

const analysisPayload = {
  spec_id: 'auto-conn-a',
  revision: 2,
  config_version: 7,
  analysis_digest: 'sha256:digest',
  credential_schema: {
    type: 'object',
    required: ['api_key'],
    properties: {
      api_key: { type: 'string', title: 'API Key', writeOnly: true },
      account: { type: 'string' },
    },
  },
  items: [
    { tool_key: 'items.list', mcp_name: 'items_list', description: 'List items', operation_kind: 'read' },
    { tool_key: 'items.create', mcp_name: 'items_create', description: 'Create item', operation_kind: 'write' },
  ],
}

test('quick OpenAPI endpoints are scope-aware and honor an injected API base URL', () => {
  assert.equal(
    openApiQuickEndpoint({ defaults: { baseURL: '' } }, 'admin', 'conn/a', 'analyze'),
    '/admin/connections/conn%2Fa/openapi/analyze',
  )
  assert.equal(
    openApiQuickEndpoint({ defaults: { baseURL: '/tenant' } }, 'tenant', 'conn/a', 'activate'),
    '/connections/conn%2Fa/openapi/activate',
  )
  assert.throws(
    () => openApiQuickEndpoint({}, 'admin', 'conn-a', 'fetch'),
    /未知的 OpenAPI 快速接入操作/,
  )
})

test('JSON specifications are parsed to objects while YAML stays bounded text for backend safe parsing', () => {
  assert.deepEqual(parseOpenApiDocument('{"openapi":"3.0.0","paths":{}}'), {
    format: 'json',
    document: { openapi: '3.0.0', paths: {} },
  })
  assert.deepEqual(parseOpenApiDocument('openapi: 3.0.0\npaths: {}', 'service.yaml'), {
    format: 'yaml',
    document: 'openapi: 3.0.0\npaths: {}',
  })
  assert.deepEqual(parseOpenApiDocument('{"openapi": 3.0.0, paths: {}}', 'service.yaml'), {
    format: 'yaml',
    document: '{"openapi": 3.0.0, paths: {}}',
  })
  assert.throws(() => parseOpenApiDocument('https://example.com/openapi.yaml'), /暂不支持 URL/)
  assert.throws(() => parseOpenApiDocument('{"openapi":', 'service.json'), /JSON 格式无效/)
})

test('analysis normalization accepts explicit response aliases and rejects unsafe tool kinds', () => {
  const normalized = normalizeOpenApiAnalysis(analysisPayload)
  assert.equal(normalized.expected_config_version, 7)
  assert.equal(normalized.tools[0].mcp_name, 'items_list')

  assert.deepEqual(normalizeOpenApiAnalysis({
    specId: 'spec-b',
    revision: 1,
    expectedConfigVersion: 3,
    digest: 'digest-b',
    credentialSchema: { type: 'object', properties: {} },
    tools: [{ toolKey: 'health', mcpName: 'health', operationKind: 'read' }],
  }), {
    spec_id: 'spec-b',
    revision: 1,
    expected_config_version: 3,
    analysis_digest: 'digest-b',
    credential_schema: { type: 'object', properties: {} },
    tools: [{ tool_key: 'health', mcp_name: 'health', description: '', operation_kind: 'read' }],
  })

  assert.throws(() => normalizeOpenApiAnalysis({
    ...analysisPayload,
    items: [{ tool_key: 'admin', operation_kind: 'admin' }],
  }), /工具分类无效/)
})

test('analysis normalization accepts the backend preview tools envelope', () => {
  const normalized = normalizeOpenApiAnalysis({
    ...analysisPayload,
    items: undefined,
    preview: { tools: analysisPayload.items },
  })
  assert.deepEqual(normalized.tools.map((tool) => tool.tool_key), ['items.list', 'items.create'])
})

test('tool classification keeps reads enabled by default and writes in an explicit group', () => {
  const normalized = normalizeOpenApiAnalysis(analysisPayload)
  const classified = classifyOpenApiTools(normalized.tools)
  assert.deepEqual(classified.readTools.map((tool) => tool.tool_key), ['items.list'])
  assert.deepEqual(classified.writeTools.map((tool) => tool.tool_key), ['items.create'])
  assert.throws(() => classifyOpenApiTools([{ tool_key: 'unknown' }]), /明确标记/)
})

test('plain credential schema produces controlled fields without reading existing values', () => {
  const schema = {
    type: 'object',
    required: ['api_key', 'retry_count'],
    properties: {
      api_key: { type: 'string', title: 'API Key' },
      account: { type: 'string' },
      retry_count: { type: 'integer' },
      sandbox: { type: 'boolean' },
    },
  }
  const fields = credentialFields(schema)
  assert.equal(fields.find((field) => field.name === 'api_key').secret, true)
  assert.deepEqual(emptyCredentialValues(schema), {
    api_key: '', account: '', retry_count: null, sandbox: false,
  })
  assert.equal(requiredCredentialsPresent(schema, { api_key: 'secret', retry_count: 2 }), true)
  assert.equal(requiredCredentialsPresent(schema, { api_key: '', retry_count: 2 }), false)
  assert.deepEqual(buildCredentialPayload(schema, {
    api_key: 'new-secret', account: '', retry_count: 2, sandbox: false, ignored: 'nope',
  }), { api_key: 'new-secret', retry_count: 2, sandbox: false })
})

test('activation request carries the analysis identity and only explicitly confirmed write tools', () => {
  const analysis = normalizeOpenApiAnalysis(analysisPayload)
  assert.deepEqual(buildOpenApiActivationRequest(analysis, { api_key: 'new-secret' }, ['items.create']), {
    spec_id: 'auto-conn-a',
    revision: 2,
    expected_config_version: 7,
    analysis_digest: 'sha256:digest',
    credentials: { api_key: 'new-secret' },
    enabled_write_tools: ['items.create'],
  })
  assert.throws(
    () => buildOpenApiActivationRequest(analysis, {}, ['items.list']),
    /只能显式启用分析结果中的写工具/,
  )
})

test('quick onboarding renders secret fields as passwords and clears credentials after requests', () => {
  const source = readFileSync(new URL('./OpenApiQuickOnboarding.jsx', import.meta.url), 'utf8')
  assert.match(source, /Input\.Password/)
  assert.match(source, /finally\s*\{[\s\S]*clearCredentialState/)
  assert.doesNotMatch(source, /response\.config|requestError\.config|JSON\.stringify\(credentials/)
})

test('admin and tenant declarative connections share the quick entry and scoped advanced mode', () => {
  const connections = readFileSync(new URL('./Connections.jsx', import.meta.url), 'utf8')
  const advanced = readFileSync(new URL('./DeclarativeSpecWizard.jsx', import.meta.url), 'utf8')
  assert.match(connections, /detail\.connector_key === 'http_declarative'/)
  assert.match(connections, /setActiveTab\(row\.connector_key === 'http_declarative' \? 'wizard' : 'config'\)/)
  assert.match(connections, /<OpenApiQuickOnboarding[\s\S]*apiClient=\{apiClient\}[\s\S]*scope=\{scope\}/)
  assert.doesNotMatch(connections, /!tenantScope && detail\.connector_key === 'http_declarative'/)
  assert.match(advanced, /apiClientEndpoint\([\s\S]*connectionResourceEndpoint\(scope, connection\.connection_id\)/)
  assert.doesNotMatch(advanced, /`\/admin\/connections\/\$\{/)
})
