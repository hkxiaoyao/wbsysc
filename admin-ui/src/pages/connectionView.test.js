import test from 'node:test'
import assert from 'node:assert/strict'
import {
  buildConnectionMcpConfig,
  apiClientEndpoint,
  canEnableWriteTool,
  closeTokenModal,
  connectionCollectionEndpoint,
  connectionResourceEndpoint,
  createConnectionMutationSequence,
  createRequestSequence,
  createWizardState,
  hasExplicitPolicies,
  invalidateWizardState,
  isActiveDeclarativeConfigReadOnly,
  parseConnectionLocation,
  requiredCredentialKeys,
  schemaMetadataSummary,
  selectActiveTokenHint,
  safeServerError,
  serializeConnectionLocation,
  setExplicitToolPolicy,
  wizardRevisionIdentity,
} from './connectionView.js'

test('tenant connection endpoints never use admin tenant routes', () => {
  assert.equal(connectionCollectionEndpoint('tenant', ''), '/tenant/connections')
  assert.equal(connectionCollectionEndpoint('tenant', 'tenant-b'), '/tenant/connections')
  assert.equal(connectionResourceEndpoint('tenant', 'conn/a', 'tools'), '/tenant/connections/conn%2Fa/tools')
})

test('admin connection endpoint serialization remains unchanged', () => {
  assert.equal(
    connectionCollectionEndpoint('admin', 'tenant /a'),
    '/admin/tenants/tenant%20%2Fa/connections',
  )
  assert.equal(connectionResourceEndpoint('admin', 'conn/a'), '/admin/connections/conn%2Fa')
})

test('tenant API base URL does not duplicate the tenant path prefix', () => {
  assert.equal(apiClientEndpoint({ defaults: { baseURL: '/tenant' } }, '/tenant/connections'), '/connections')
  assert.equal(apiClientEndpoint({ defaults: { baseURL: '' } }, '/admin/tenants'), '/admin/tenants')
})

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
    mappingReviewed: false,
    credentialsSaved: false,
    policiesSaved: false,
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

test('active declarative connections block the generic config editor', () => {
  assert.equal(isActiveDeclarativeConfigReadOnly({ connector_key: 'http_declarative', status: 'active' }), true)
  assert.equal(isActiveDeclarativeConfigReadOnly({ connector_key: 'http_declarative', status: 'disabled' }), false)
  assert.equal(isActiveDeclarativeConfigReadOnly({ connector_key: 'wecom', status: 'active' }), false)
})

test('source identity changes invalidate every downstream wizard result', () => {
  const state = {
    ...createWizardState({ status: 'disabled' }),
    imported: true,
    validated: true,
    published: true,
    mappingReviewed: true,
    credentialsSaved: true,
    policiesSaved: true,
    tested: true,
    activated: true,
  }
  assert.deepEqual(invalidateWizardState(state, 'source'), {
    ...state,
    imported: false,
    validated: false,
    published: false,
    mappingReviewed: false,
    credentialsSaved: false,
    policiesSaved: false,
    tested: false,
    activated: false,
  })
})

test('credential, mapping, and policy edits invalidate test and activation only', () => {
  const state = {
    ...createWizardState({ status: 'disabled' }),
    imported: true,
    validated: true,
    published: true,
    mappingReviewed: true,
    credentialsSaved: true,
    policiesSaved: true,
    tested: true,
    activated: true,
  }
  for (const scope of ['credentials', 'mapping', 'policy']) {
    const next = invalidateWizardState(state, scope)
    assert.equal(next.published, true)
    assert.equal(next.tested, false)
    assert.equal(next.activated, false)
  }
})

test('write policy toggles always require fresh consent', () => {
  const writeTool = { tool_key: 'orders.create', operation_kind: 'write' }
  const enabled = setExplicitToolPolicy(writeTool, undefined, true)
  assert.deepEqual(enabled, { tool_key: 'orders.create', enabled: true, allow_write: false })
  const consented = { ...enabled, allow_write: true }
  assert.deepEqual(setExplicitToolPolicy(writeTool, consented, false), {
    tool_key: 'orders.create', enabled: false, allow_write: false,
  })
  assert.deepEqual(setExplicitToolPolicy(writeTool, consented, true), {
    tool_key: 'orders.create', enabled: true, allow_write: false,
  })
})

test('explicit policies reject an enabled write tool without fresh consent', () => {
  const tools = [{ tool_key: 'orders.create', operation_kind: 'write' }]
  assert.equal(hasExplicitPolicies(tools, [{ tool_key: 'orders.create', enabled: true, allow_write: false }]), false)
  assert.equal(hasExplicitPolicies(tools, [{ tool_key: 'orders.create', enabled: true, allow_write: true }]), true)
  assert.equal(hasExplicitPolicies(tools, [{ tool_key: 'orders.create', enabled: false, allow_write: false }]), true)
  assert.equal(hasExplicitPolicies(tools, [
    { tool_key: 'orders.create', enabled: false, allow_write: false },
    { tool_key: 'orders.create', enabled: true, allow_write: true },
  ]), false)
})

test('token summary selects only a non-revoked token', () => {
  assert.equal(selectActiveTokenHint([
    { prefix: 'old', revoked_at: '2026-07-01T00:00:00Z' },
    { prefix: 'expired', expires_at: '2020-01-01T00:00:00Z' },
    { prefix: 'current', revoked_at: null },
  ]), 'current')
  assert.equal(selectActiveTokenHint([{ prefix: 'old', revoked_at: '2026-07-01T00:00:00Z' }]), '')
})

test('wizard revision identity is captured as a normalized immutable value', () => {
  const state = { specId: ' spec-a ', revision: 2 }
  const identity = wizardRevisionIdentity(state)
  state.specId = 'spec-b'
  state.revision = 3
  assert.deepEqual(identity, { specId: 'spec-a', revision: 2 })
})

test('request sequence rejects stale and invalidated responses', () => {
  const sequence = createRequestSequence()
  const first = sequence.begin()
  const second = sequence.begin()
  assert.equal(sequence.isCurrent(first), false)
  assert.equal(sequence.isCurrent(second), true)
  sequence.invalidate()
  assert.equal(sequence.isCurrent(second), false)
})

test('connection mutation sequence rejects another connection and a replaced detail request', () => {
  const sequence = createConnectionMutationSequence()
  const ticket = sequence.begin('conn-a', 4)

  assert.equal(sequence.isCurrent(ticket, 'conn-a', 4), true)
  assert.equal(sequence.isCurrent(ticket, 'conn-b', 4), false)
  assert.equal(sequence.isCurrent(ticket, 'conn-a', 5), false)
})

test('connection mutation sequence rejects superseded and explicitly invalidated mutations', () => {
  const sequence = createConnectionMutationSequence()
  const first = sequence.begin('conn-a', 4)
  const second = sequence.begin('conn-a', 4)

  assert.equal(sequence.isCurrent(first, 'conn-a', 4), false)
  assert.equal(sequence.isCurrent(second, 'conn-a', 4), true)
  sequence.invalidate()
  assert.equal(sequence.isCurrent(second, 'conn-a', 4), false)
})

test('schema metadata summary exposes approved properties without values', () => {
  assert.deepEqual(schemaMetadataSummary({
    type: 'object',
    required: ['query'],
    properties: { query: { type: 'string' } },
  }), {
    required: ['query'],
    properties: { query: { type: 'string' } },
  })
  assert.equal(schemaMetadataSummary(null), null)
})

test('required credential keys returns names only', () => {
  assert.deepEqual(requiredCredentialKeys({ required: ['api_key', 'tenant'] }), ['api_key', 'tenant'])
  assert.deepEqual(requiredCredentialKeys({ required: ['api_key', 7, ''] }), ['api_key'])
  assert.deepEqual(requiredCredentialKeys(null), [])
})
