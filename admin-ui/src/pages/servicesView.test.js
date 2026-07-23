import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  CONNECTOR_CARDS,
  aliasConflicts,
  apiClientEndpoint,
  bindingAliasPreview,
  bindingPayload,
  closeServiceTokenState,
  connectorCard,
  createTokenRevealSequence,
  copyTokenWithLifecycle,
  defaultToolAlias,
  failedServiceTokenState,
  parseServiceLocation,
  safeServiceError,
  serializeServiceLocation,
  serviceCollectionEndpoint,
  serviceResourceEndpoint,
  serviceTokenIssuePayload,
  tokenCanCopy,
  tokenCanReveal,
  tokenCanRevoke,
  tokenLifecycleStatus,
  canonicalTokenTimestamp,
} from './servicesView.js'

test('tenant service endpoints never use an admin tenant route', () => {
  assert.equal(serviceCollectionEndpoint('tenant', 'ignored-tenant'), '/tenant/services')
  assert.equal(
    serviceResourceEndpoint('tenant', 'ignored-tenant', 'svc/a', 'tokens/token ?/reveal'),
    '/tenant/services/svc%2Fa/tokens/token%20%3F/reveal',
  )
})

test('admin service endpoints require and encode an explicit tenant scope', () => {
  assert.equal(
    serviceCollectionEndpoint('admin', 'tenant /a'),
    '/admin/tenants/tenant%20%2Fa/services',
  )
  assert.equal(
    serviceResourceEndpoint('admin', 'tenant /a', 'svc/a', 'tools'),
    '/admin/tenants/tenant%20%2Fa/services/svc%2Fa/tools',
  )
  assert.throws(() => serviceCollectionEndpoint('admin', ''), /tenant scope/i)
})

test('tenant API base URL does not duplicate the tenant path prefix', () => {
  assert.equal(apiClientEndpoint({ defaults: { baseURL: '/tenant' } }, '/tenant/services'), '/services')
  assert.equal(
    apiClientEndpoint({ defaults: { baseURL: '/tenant/' } }, '/tenant/services/svc-a/tokens'),
    '/services/svc-a/tokens',
  )
  assert.equal(apiClientEndpoint({ defaults: { baseURL: '' } }, '/admin/tenants/a/services'), '/admin/tenants/a/services')
})

test('admin service location keeps only explicit tenant scope and never secrets', () => {
  assert.deepEqual(parseServiceLocation('?tenant_id=tenant%20a&token=mcp_secret&service_id=svc-a'), {
    tenantId: 'tenant a',
  })
  assert.equal(serializeServiceLocation({ tenantId: 'tenant /a', token: 'mcp_secret' }), 'tenant_id=tenant+%2Fa')
})

test('default alias is the exact stable backend identity', () => {
  assert.equal(defaultToolAlias('wecom_abcd1234', 'users.get'), 'wecom_abcd1234__users.get')
  assert.equal(defaultToolAlias('East China', 'Display Tool'), 'East China__Display Tool')
})

test('binding alias preview requires the authoritative alias even when id differs', () => {
  assert.equal(bindingAliasPreview(
    { connection_id: 'uuid-connection-id', connection_alias: 'renamed_alias' },
    { tool_key: 'users.get', mcp_name: 'Public.Users' },
  ), 'renamed_alias__Public.Users')
  assert.throws(() => bindingAliasPreview(
    { connection_id: 'uuid-connection-id', connection_alias: '' },
    { tool_key: 'users.get', mcp_name: 'Public.Users' },
  ), /authoritative connection alias/i)
})

test('service tool selection uses the authoritative alias helper without id fallback', () => {
  const source = readFileSync(new URL('./Services.jsx', import.meta.url), 'utf8')
  assert.match(source, /bindingAliasPreview\(connection, tool\)/)
  assert.doesNotMatch(source, /connection\?\.connection_alias\s*\|\|\s*connection\?\.connection_id/)
})

test('alias conflicts are deterministic and include every duplicate alias', () => {
  assert.deepEqual(aliasConflicts([
    { connection_id: 'conn-b', source_tool_key: 'users.get', tool_alias: 'shared' },
    { connection_id: 'conn-a', source_tool_key: 'users.list', tool_alias: 'unique' },
    { connection_id: 'conn-a', source_tool_key: 'users.get', tool_alias: 'shared' },
    { connection_id: 'conn-c', source_tool_key: 'users.get', tool_alias: 'shared' },
  ]), ['shared'])
  assert.deepEqual(aliasConflicts([{ tool_alias: 'once' }, { tool_alias: 'twice' }]), [])
})

test('new mixed-case aliases conflict using backend ASCII identifier semantics', () => {
  assert.deepEqual(aliasConflicts([
    { tool_alias: 'Public.Users' },
    { tool_alias: 'public.users' },
  ]), ['Public.Users'])
})

test('editing an existing alias blocks a mixed-case collision and preserves display spelling', () => {
  const edited = [
    { binding_id: 'binding-a', tool_alias: 'Orders.List' },
    { binding_id: 'binding-b', tool_alias: 'ORDERS.LIST' },
  ]
  assert.deepEqual(aliasConflicts(edited), ['Orders.List'])
  assert.equal(edited[1].tool_alias, 'ORDERS.LIST')
})

test('binding payload is explicit and user status is limited to active or disabled', () => {
  const input = {
    binding_id: 'ignored-by-client',
    connection_id: 'conn-a',
    source_tool_key: 'users.get',
    tool_alias: 'conn-a__users.get',
    binding_status: 'active',
    policy: { allowed: true },
    extra: 'ignored',
  }
  assert.deepEqual(bindingPayload(input), {
    connection_id: 'conn-a',
    source_tool_key: 'users.get',
    tool_alias: 'conn-a__users.get',
    binding_status: 'active',
    policy: { allowed: true },
  })
  assert.throws(() => bindingPayload({ ...input, binding_status: 'broken' }), /binding status/i)
  assert.throws(() => bindingPayload({ ...input, policy: [] }), /policy/i)
})

test('connector catalog exposes only fixed read-only cards', () => {
  assert.deepEqual(CONNECTOR_CARDS.map(({ key }) => key), ['wecom', 'http_declarative'])
  assert.equal(connectorCard('wecom')?.title, '企业微信')
  assert.equal(connectorCard('arbitrary-user-key'), null)
  assert.equal(Object.isFrozen(CONNECTOR_CARDS), true)
})

test('connection creation renders fixed connector cards without a typed key control', () => {
  const source = readFileSync(new URL('./Connections.jsx', import.meta.url), 'utf8')
  assert.match(source, /CONNECTOR_CARDS\.map/)
  assert.doesNotMatch(source, /aria-label="连接器 Key"/)
  assert.doesNotMatch(source, /<datalist/)
})

test('token list metadata and prefix are never copyable as raw token', () => {
  assert.equal(tokenCanCopy({ prefix: 'mcp_abcd', raw_value: undefined }), false)
  assert.equal(tokenCanCopy({ token_prefix: 'mcp_abcd', raw_value: undefined }), false)
  assert.equal(tokenCanCopy({ raw_value: 'mcp_full_value' }), true)
  assert.equal(tokenCanCopy({ raw_value: '   ' }), false)
})

test('service token issue payload normalizes a selected local instant and uses null for no expiry', () => {
  assert.deepEqual(serviceTokenIssuePayload('  automation  ', ''), {
    label: 'automation',
    expires_at: null,
  })
  assert.deepEqual(serviceTokenIssuePayload('automation', '2030-01-02T03:04:05'), {
    label: 'automation',
    expires_at: new Date(2030, 0, 2, 3, 4, 5).toISOString().replace('.000Z', 'Z'),
  })
  assert.throws(() => serviceTokenIssuePayload('', '2030-01-02 03:04:05'), /expiry/i)
  assert.throws(() => serviceTokenIssuePayload('', '2030-02-30T03:04:05'), /expiry/i)
  assert.throws(() => serviceTokenIssuePayload('', '2030-01-02T03:04:05Z'), /expiry/i)
})

test('reveal eligibility fails closed at expiry while revoke eligibility remains independent', () => {
  const now = new Date('2026-07-22T10:00:00Z')
  assert.equal(tokenCanReveal({ token_id: 'tok-a', revoked_at: null, expires_at: null }, now), true)
  assert.equal(tokenCanReveal({ token_id: 'tok-a', expires_at: '2026-07-22T10:00:01Z' }, now), true)
  assert.equal(tokenCanReveal({ token_id: 'tok-a', expires_at: '2026-07-22T10:00:00Z' }, now), false)
  assert.equal(tokenCanReveal({ token_id: 'tok-a', expires_at: 'not-a-date' }, now), false)
  assert.equal(tokenCanReveal({ token_id: 'tok-a', expires_at: '' }, now), false)
  assert.equal(tokenCanReveal({ token_id: 'tok-a', expires_at: '2026-07-22T11:00:00' }, now), false)
  assert.equal(tokenCanReveal({ token_id: 'tok-a', revoked_at: '2026-07-01T00:00:00Z' }, now), false)
  assert.equal(tokenCanReveal({ token_id: '', expires_at: null }, now), false)

  assert.equal(tokenCanRevoke({ token_id: 'tok-expired', expires_at: '2020-01-01T00:00:00Z' }), true)
  assert.equal(tokenCanRevoke({ token_id: 'tok-revoked', revoked_at: '2026-01-01T00:00:00Z' }), false)
})

test('token lifecycle and canonical metadata distinguish active expired and revoked', () => {
  const now = new Date('2026-07-22T10:00:00Z')
  assert.equal(tokenLifecycleStatus({ token_id: 'a', expires_at: null }, now), 'active')
  assert.equal(tokenLifecycleStatus({ token_id: 'a', expires_at: '2026-07-22T10:00:00Z' }, now), 'expired')
  assert.equal(tokenLifecycleStatus({ token_id: 'a', revoked_at: '2026-07-01T00:00:00Z' }, now), 'revoked')
  assert.equal(tokenLifecycleStatus({ token_id: 'a', expires_at: 'malformed' }, now), 'expired')
  assert.equal(canonicalTokenTimestamp('2026-07-22T18:00:00+08:00'), '2026-07-22T10:00:00Z')
  assert.equal(canonicalTokenTimestamp('2026-07-22T10:00:00'), '')
  assert.equal(canonicalTokenTimestamp('2026-02-30T10:00:00Z'), '')
  assert.equal(canonicalTokenTimestamp('invalid'), '')
})

test('generic audit failures render only the fixed local fallback', () => {
  const fallback = 'Token 查看失败，请重试'
  const rendered = safeServiceError({
    message: 'decrypt exception included mcp_secret',
    response: { data: { detail: 'service operation failed', token: 'mcp_secret' } },
  }, fallback)
  assert.equal(rendered, fallback)
  assert.doesNotMatch(rendered, /secret|exception|service operation failed/i)
})

test('shared token modal wires expiry payload, reset, canonical metadata, and independent revoke controls', () => {
  const source = readFileSync(new URL('./ServiceTokenModal.jsx', import.meta.url), 'utf8')
  assert.match(source, /serviceTokenIssuePayload\(label, expiresAt\)/)
  assert.match(source, /setExpiresAt\(''\)/)
  assert.match(source, /aria-label="服务 Token 过期时间"/)
  assert.match(source, /canonicalTokenTimestamp\(token\.created_at\)/)
  assert.match(source, /canonicalTokenTimestamp\(token\.expires_at\)/)
  assert.match(source, /canonicalTokenTimestamp\(token\.last_used_at\)/)
  assert.match(source, /tokenCanRevoke\(token\)/)
})

test('reveal sequence rejects stale token, service, replacement, and invalidated responses', () => {
  const sequence = createTokenRevealSequence()
  const first = sequence.begin('svc-a', 'tok-a')
  const replacement = sequence.begin('svc-a', 'tok-a')
  assert.equal(sequence.isCurrent(first, 'svc-a', 'tok-a'), false)
  assert.equal(sequence.isCurrent(replacement, 'svc-b', 'tok-a'), false)
  assert.equal(sequence.isCurrent(replacement, 'svc-a', 'tok-b'), false)
  assert.equal(sequence.isCurrent(replacement, 'svc-a', 'tok-a'), true)
  sequence.invalidate()
  assert.equal(sequence.isCurrent(replacement, 'svc-a', 'tok-a'), false)
})

test('deferred clipboard rejection cannot write after service switch or close', async () => {
  let rejectClipboard
  const pendingClipboard = new Promise((_, reject) => { rejectClipboard = reject })
  const sequence = createTokenRevealSequence()
  let visible = true
  let activeService = 'service-old'
  const failures = []
  const copy = copyTokenWithLifecycle({
    sequence,
    serviceId: 'service-old',
    tokenId: 'token-old',
    rawToken: 'mcp_secret',
    writeText: () => pendingClipboard,
    isActive: () => visible && activeService === 'service-old',
    onCurrentFailure: error => failures.push(error.message),
  })

  visible = false
  activeService = 'service-new'
  sequence.invalidate()
  rejectClipboard(new Error('late clipboard failure'))

  assert.equal(await copy, 'stale')
  assert.deepEqual(failures, [])
})

test('deferred clipboard success is stale after a replacement request', async () => {
  let resolveClipboard
  const pendingClipboard = new Promise(resolve => { resolveClipboard = resolve })
  const sequence = createTokenRevealSequence()
  const copy = copyTokenWithLifecycle({
    sequence,
    serviceId: 'service-a',
    tokenId: 'token-a',
    rawToken: 'mcp_secret',
    writeText: () => pendingClipboard,
    isActive: () => true,
    onCurrentFailure: () => assert.fail('stale success must not report failure'),
  })

  sequence.begin('service-a', 'token-b')
  resolveClipboard()

  assert.equal(await copy, 'stale')
})

test('current clipboard rejection reports one guarded failure', async () => {
  const sequence = createTokenRevealSequence()
  const failures = []
  const result = await copyTokenWithLifecycle({
    sequence,
    serviceId: 'service-a',
    tokenId: 'token-a',
    rawToken: 'mcp_secret',
    writeText: async () => { throw new Error('clipboard failed') },
    isActive: () => true,
    onCurrentFailure: error => failures.push(error.message),
  })

  assert.equal(result, 'failed')
  assert.deepEqual(failures, ['clipboard failed'])
})

test('closing token state removes every raw-bearing field', () => {
  assert.deepEqual(closeServiceTokenState({
    open: true,
    serviceId: 'svc-a',
    tokenId: 'tok-a',
    rawToken: 'mcp_secret',
    revealBusy: true,
    error: 'secret-bearing error',
  }), {
    open: false,
    serviceId: '',
    tokenId: '',
    rawToken: '',
    revealBusy: false,
    error: '',
  })
})

test('failed token request keeps only safe modal context and clears raw state', () => {
  assert.deepEqual(failedServiceTokenState('svc-a', 'Token 查看失败，请重试'), {
    open: true,
    serviceId: 'svc-a',
    tokenId: '',
    rawToken: '',
    revealBusy: false,
    error: 'Token 查看失败，请重试',
  })
})
