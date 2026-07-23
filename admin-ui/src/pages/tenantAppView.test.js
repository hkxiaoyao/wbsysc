import assert from 'node:assert/strict'
import test from 'node:test'

import tenantApi, { loginTenant, logoutTenant } from '../tenantApi.js'
import {
  createTenantLogoutSequence,
  executeTenantLogout,
  normalizeTenantView,
  parseTenantLocation,
  serializeTenantLocation,
} from './tenantAppView.js'

test('tenant views are allowlisted and admin-only views normalize to overview', () => {
  for (const view of ['overview', 'connections', 'services', 'logs', 'account']) {
    assert.equal(normalizeTenantView(view), view)
  }
  for (const view of ['', 'tenants', 'admin', 'unknown']) {
    assert.equal(normalizeTenantView(view), 'overview')
  }
})

test('failed tenant logout preserves the shell and exposes only a retryable error', async () => {
  let sessionState = 'authed'
  let logoutError = ''
  let request
  const sequence = createTenantLogoutSequence()
  const requestId = sequence.begin()
  const originalAdapter = tenantApi.defaults.adapter
  tenantApi.defaults.adapter = async config => {
    request = config
    const error = new Error('transport failed')
    error.config = config
    error.response = { status: 503, data: { detail: 'session-secret-value' } }
    throw error
  }
  let outcome
  try {
    outcome = await executeTenantLogout({
      request: () => logoutTenant(),
      isCurrent: () => sequence.isCurrent(requestId),
      onLoggedOut: () => { sessionState = 'logged-out' },
      onError: message => { logoutError = message },
    })
  } finally {
    tenantApi.defaults.adapter = originalAdapter
  }

  assert.equal(outcome, 'failed')
  assert.equal(request.url, '/logout')
  assert.equal(request.suppressSessionExpired, true)
  assert.equal(sessionState, 'authed')
  assert.equal(logoutError, '退出登录失败，请重试')
  assert.equal(logoutError.includes('session-secret-value'), false)
})

test('only a current successful logout clears authenticated state', async () => {
  let sessionState = 'authed'
  const sequence = createTenantLogoutSequence()
  const requestId = sequence.begin()
  const outcome = await executeTenantLogout({
    request: async () => ({ data: { ok: true, session: 'ignored-session-value' } }),
    isCurrent: () => sequence.isCurrent(requestId),
    onLoggedOut: () => { sessionState = 'logged-out' },
    onError: () => assert.fail('successful logout must not report an error'),
  })

  assert.equal(outcome, 'logged-out')
  assert.equal(sessionState, 'logged-out')
})

test('late logout completion cannot corrupt a newer session', async () => {
  let resolveLogout
  const pendingLogout = new Promise(resolve => { resolveLogout = resolve })
  let sessionState = 'authed'
  let logoutError = ''
  const sequence = createTenantLogoutSequence()
  const requestId = sequence.begin()
  const execution = executeTenantLogout({
    request: () => pendingLogout,
    isCurrent: () => sequence.isCurrent(requestId),
    onLoggedOut: () => { sessionState = 'logged-out' },
    onError: message => { logoutError = message },
  })

  sequence.invalidate()
  resolveLogout({ data: { ok: true } })

  assert.equal(await execution, 'stale')
  assert.equal(sessionState, 'authed')
  assert.equal(logoutError, '')
})

test('tenant location accepts only the view and drops tenant scope', () => {
  assert.deepEqual(
    parseTenantLocation('?view=services&tenant_id=tenant-b&connection_id=conn-a'),
    { view: 'services' },
  )
  assert.deepEqual(parseTenantLocation('?view=tenants&tenant_id=tenant-b'), { view: 'overview' })
  assert.equal(serializeTenantLocation('logs'), '?view=logs')
  assert.equal(serializeTenantLocation('tenants'), '?view=overview')
  assert.equal(serializeTenantLocation('services', { tenant_id: 'tenant-b' }), '?view=services')
})

test('tenant API is cookie-only and login sends only tenant credentials', async () => {
  assert.equal(tenantApi.defaults.baseURL, '/tenant')
  assert.equal(tenantApi.defaults.withCredentials, true)
  assert.equal(tenantApi.defaults.headers.common.Authorization, undefined)

  let request
  const originalAdapter = tenantApi.defaults.adapter
  tenantApi.defaults.adapter = async (config) => {
    request = config
    return { data: { ok: true, session: 'must-not-be-stored' }, status: 200, statusText: 'OK', headers: {}, config }
  }
  try {
    await loginTenant('tenant-a', 'Tenant-Secure-123')

    assert.equal(request.url, '/login')
    assert.equal(request.baseURL, '/tenant')
    assert.equal(request.withCredentials, true)
    assert.equal(request.headers.Authorization, undefined)
    assert.deepEqual(JSON.parse(request.data), {
      tenant_id: 'tenant-a',
      password: 'Tenant-Secure-123',
    })

    await logoutTenant()
    assert.equal(request.url, '/logout')
    assert.equal(request.suppressSessionExpired, true)
    assert.equal(request.headers.Authorization, undefined)
  } finally {
    tenantApi.defaults.adapter = originalAdapter
  }
})
