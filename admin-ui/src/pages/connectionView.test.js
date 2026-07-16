import test from 'node:test'
import assert from 'node:assert/strict'
import {
  buildConnectionMcpConfig,
  canEnableWriteTool,
  closeTokenModal,
  createWizardState,
  hasExplicitPolicies,
  parseConnectionLocation,
  safeServerError,
  serializeConnectionLocation,
} from './connectionView.js'

test('buildConnectionMcpConfig uses the encoded instance-specific endpoint', () => {
  const config = JSON.parse(buildConnectionMcpConfig(
    { connection_id: 'conn/a ?#', initial_token: 'mcp_once' },
    'https://gw.example.com/',
  ))

  assert.equal(config.mcpServers['conn/a ?#'].url, 'https://gw.example.com/mcp/conn%2Fa%20%3F%23')
  assert.equal(config.mcpServers['conn/a ?#'].headers.Authorization, 'Bearer mcp_once')
})

test('token display state is cleared after the one-time modal closes', () => {
  assert.deepEqual(closeTokenModal({ open: true, rawToken: 'mcp_secret', connectionId: 'conn-a' }), {
    open: false,
    rawToken: '',
    connectionId: '',
  })
})

test('write tools require explicit enable and explicit write consent', () => {
  const writeTool = { operation_kind: 'write' }
  assert.equal(canEnableWriteTool(writeTool, { explicitEnable: false, explicitWrite: true }), false)
  assert.equal(canEnableWriteTool(writeTool, { explicitEnable: true, explicitWrite: false }), false)
  assert.equal(canEnableWriteTool(writeTool, { explicitEnable: true, explicitWrite: true }), true)
  assert.equal(canEnableWriteTool({ operation_kind: 'read' }, { explicitEnable: true }), true)
})

test('safeServerError never renders secret-bearing server details or raw upstream payloads', () => {
  assert.equal(
    safeServerError({ response: { data: { detail: 'Authorization: Bearer mcp_secret' } } }),
    '操作失败，请检查连接配置',
  )
  assert.equal(
    safeServerError({ response: { data: { detail: 'connection configuration changed' } } }),
    'connection configuration changed',
  )
  assert.equal(safeServerError(new Error('socket included api_key=secret')), '操作失败，请稍后重试')
})

test('connection URL state serializes tenant and connection filters without secrets', () => {
  const search = serializeConnectionLocation({ tenantId: 'tenant /a', connectionId: 'conn?1' })
  assert.equal(search, 'tenant_id=tenant+%2Fa&connection_id=conn%3F1')
  assert.deepEqual(parseConnectionLocation(`?${search}&token=mcp_secret`), {
    tenantId: 'tenant /a',
    connectionId: 'conn?1',
  })
  assert.equal(search.includes('token'), false)
})

test('declarative wizard starts with a bounded import-only state and blocks active imports', () => {
  assert.deepEqual(createWizardState({ status: 'draft' }), {
    step: 0,
    sourceFormat: 'json',
    sourceText: '',
    specId: '',
    revision: 1,
    imported: false,
    validated: false,
    tested: false,
    published: false,
    activated: false,
    mustDisable: false,
  })
  assert.equal(createWizardState({ status: 'active' }).mustDisable, true)
})

test('every pending tool needs an explicit policy before activation', () => {
  const tools = [{ tool_key: 'health' }, { tool_key: 'orders.create' }]
  assert.equal(hasExplicitPolicies(tools, [{ tool_key: 'health', enabled: true }]), false)
  assert.equal(hasExplicitPolicies(tools, [
    { tool_key: 'health', enabled: true },
    { tool_key: 'orders.create', enabled: false },
  ]), true)
  assert.equal(hasExplicitPolicies(tools, [
    { tool_key: 'health', enabled: true },
    { tool_key: 'orders.create', enabled: false },
    { tool_key: 'stale', enabled: false },
  ]), false)
})
