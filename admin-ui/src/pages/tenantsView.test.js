import test from 'node:test'
import assert from 'node:assert/strict'
import {
  EMPTY_FILTERS,
  buildTenantLoginPatch,
  buildTenantLoginStatusPatch,
  buildTenantPasswordReset,
  confirmedTenantLoginStatus,
  createTenantActionLock,
  createTenantRequestGeneration,
  filterTenants,
  getDirectModeReason,
  getTenantStats,
  projectTenantLoginState,
  tenantLoginPasswordEndpoint,
  tenantLoginStatusEndpoint,
  tenantPasswordValidationError,
} from './tenantsView.js'

const tenants = [
  {
    tenant_id: 'alpha',
    display_name: '北区门店',
    corpid: 'wwAlpha',
    data_mode: 'stored',
    enabled: true,
    has_secret: true,
  },
  {
    tenant_id: 'beta',
    display_name: '华南直连',
    corpid: 'wwBeta',
    data_mode: 'direct',
    enabled: true,
    has_secret: false,
  },
  {
    tenant_id: 'gamma',
    display_name: '停用租户',
    corpid: 'wwGamma',
    data_mode: 'stored',
    enabled: false,
    has_secret: true,
  },
]

test('getTenantStats derives stable full-list counts', () => {
  assert.deepEqual(getTenantStats(tenants), {
    total: 3,
    running: 2,
    direct: 1,
    attention: 2,
  })
})

test('filterTenants searches name, tenant id, and CorpID case-insensitively', () => {
  assert.deepEqual(
    filterTenants(tenants, { ...EMPTY_FILTERS, query: 'WWBETA' }).map((row) => row.tenant_id),
    ['beta'],
  )
  assert.deepEqual(
    filterTenants(tenants, { ...EMPTY_FILTERS, query: '北区' }).map((row) => row.tenant_id),
    ['alpha'],
  )
})

test('filterTenants combines mode and enabled filters', () => {
  assert.deepEqual(
    filterTenants(tenants, { query: '', dataMode: 'stored', enabled: 'disabled' })
      .map((row) => row.tenant_id),
    ['gamma'],
  )
})

test('getDirectModeReason explains unavailable synchronization actions', () => {
  assert.equal(getDirectModeReason(tenants[1]), '直连模式实时调用企微 API，无需同步')
  assert.equal(getDirectModeReason(tenants[0]), '')
})

test('buildTenantLoginPatch includes only explicitly nonempty exact passwords', () => {
  assert.deepEqual(buildTenantLoginPatch(undefined), {})
  assert.deepEqual(buildTenantLoginPatch(null), {})
  assert.deepEqual(buildTenantLoginPatch(''), {})
  assert.deepEqual(buildTenantLoginPatch(' exact-secret-123 '), {
    tenant_password: ' exact-secret-123 ',
  })
  assert.throws(() => buildTenantLoginPatch(123), /string/i)
})

test('password reset payload preserves the exact password and rejects empty values', () => {
  assert.deepEqual(buildTenantPasswordReset(' exact-secret-123 '), {
    password: ' exact-secret-123 ',
  })
  assert.throws(() => buildTenantPasswordReset(''), /password/i)
  assert.throws(() => buildTenantPasswordReset(null), /string/i)
})

test('tenant password validation mirrors length, edge-space, and forbidden-word policy', () => {
  assert.equal(tenantPasswordValidationError('', { optional: true }), '')
  assert.match(tenantPasswordValidationError('', { optional: false }), /required/i)
  assert.match(tenantPasswordValidationError('a'.repeat(11)), /12/)
  assert.equal(tenantPasswordValidationError('a'.repeat(12)), '')
  assert.equal(tenantPasswordValidationError('🔐'.repeat(12)), '')
  assert.equal(tenantPasswordValidationError('a'.repeat(256)), '')
  assert.match(tenantPasswordValidationError('a'.repeat(257)), /256/)
  assert.match(tenantPasswordValidationError(' abcdefghijk'), /space/i)
  assert.match(tenantPasswordValidationError('abcdefghijk '), /space/i)
  assert.match(tenantPasswordValidationError('safe-PassWord-value'), /password/i)
  assert.throws(() => tenantPasswordValidationError(123), /string/i)
})

test('login mutation endpoints encode tenant IDs and reject blank identifiers', () => {
  assert.equal(
    tenantLoginPasswordEndpoint('tenant /a'),
    '/admin/tenants/tenant%20%2Fa/login-password',
  )
  assert.equal(
    tenantLoginStatusEndpoint('tenant /a'),
    '/admin/tenants/tenant%20%2Fa/login-status',
  )
  assert.throws(() => tenantLoginPasswordEndpoint('   '), /tenant/i)
  assert.throws(() => tenantLoginStatusEndpoint(null), /tenant/i)
})

test('login status payload accepts only active or disabled', () => {
  assert.deepEqual(buildTenantLoginStatusPatch('active'), { status: 'active' })
  assert.deepEqual(buildTenantLoginStatusPatch('disabled'), { status: 'disabled' })
  for (const invalid of ['', 'enabled', 'unknown', true, null]) {
    assert.throws(() => buildTenantLoginStatusPatch(invalid), /status/i)
  }
})

test('login status mutation is confirmed only by an exact successful response', () => {
  assert.equal(confirmedTenantLoginStatus({ ok: true, status: 'active' }, 'active'), 'active')
  assert.equal(confirmedTenantLoginStatus({ ok: true, status: 'disabled' }, 'disabled'), 'disabled')
  for (const response of [
    undefined,
    null,
    {},
    { ok: false, status: 'active' },
    { ok: 1, status: 'active' },
    { ok: true },
    { ok: true, status: 'unknown' },
    { ok: true, status: 'disabled' },
  ]) {
    assert.equal(confirmedTenantLoginStatus(response, 'active'), null)
  }
  assert.equal(confirmedTenantLoginStatus({ ok: true, status: 'active' }, 'enabled'), null)
})

test('login metadata projection is fail closed and never infers from tenant enabled', () => {
  assert.deepEqual(projectTenantLoginState({ has_login_account: true, login_status: 'active', enabled: false }), {
    hasAccount: true, status: 'active', kind: 'active',
  })
  assert.deepEqual(projectTenantLoginState({ has_login_account: true, login_status: 'disabled', enabled: true }), {
    hasAccount: true, status: 'disabled', kind: 'disabled',
  })
  assert.deepEqual(projectTenantLoginState({ has_login_account: false, login_status: 'active', enabled: true }), {
    hasAccount: false, status: null, kind: 'none',
  })
  assert.deepEqual(projectTenantLoginState({ enabled: true }), {
    hasAccount: null, status: null, kind: 'unknown',
  })
  assert.deepEqual(projectTenantLoginState({ has_login_account: true, login_status: 'future-state', enabled: true }), {
    hasAccount: true, status: null, kind: 'unknown',
  })
})

test('request generations reject old tickets and explicit invalidation', () => {
  const generation = createTenantRequestGeneration()
  const first = generation.begin('alpha', 'reset')
  assert.equal(generation.isCurrent(first, 'alpha', 'reset'), true)
  const second = generation.begin('beta', 'status')
  assert.equal(generation.isCurrent(first, 'alpha', 'reset'), false)
  assert.equal(generation.isCurrent(second, 'beta', 'status'), true)
  assert.equal(generation.isCurrent(second, 'alpha', 'status'), false)
  generation.invalidate()
  assert.equal(generation.isCurrent(second, 'beta', 'status'), false)
})

test('tenant action locks use exact tenant identity and serialize actions per tenant', () => {
  const lock = createTenantActionLock()
  assert.equal(lock.acquire('acme', 'sync'), true)
  assert.equal(lock.acquire('acme:west', 'reset'), true)
  assert.equal(lock.isBusy('acme'), true)
  assert.equal(lock.isBusy('acme:west'), true)
  assert.equal(lock.acquire('acme', 'diagnose'), false)
  assert.equal(lock.release('acme', 'diagnose'), false)
  assert.equal(lock.release('acme', 'sync'), true)
  assert.equal(lock.isBusy('acme'), false)
  assert.equal(lock.isBusy('acme:west'), true)
  assert.equal(lock.release('acme:west', 'reset'), true)
  assert.equal(lock.isBusy('acme:west'), false)
})
