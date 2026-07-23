export const EMPTY_FILTERS = Object.freeze({
  query: '',
  enabled: 'all',
})

const normalized = (value) => String(value ?? '').trim().toLocaleLowerCase()

function requireString(value, label) {
  if (typeof value !== 'string') throw new TypeError(`${label} must be a string`)
  return value
}

function encodedTenantId(tenantId) {
  const value = requireString(tenantId, 'tenant id')
  if (!value.trim()) throw new Error('tenant id is required')
  return encodeURIComponent(value)
}

export function buildTenantLoginPatch(value) {
  if (value === undefined || value === null || value === '') return {}
  return { tenant_password: requireString(value, 'password') }
}

export function buildTenantIdentityPayload(values, { editing = false } = {}) {
  if (!values || Array.isArray(values) || typeof values !== 'object') {
    throw new TypeError('tenant values must be an object')
  }
  const payload = {
    display_name: typeof values.display_name === 'string' ? values.display_name : '',
    enabled: Boolean(values.enabled),
  }
  if (editing) return payload
  payload.tenant_id = requireString(values.tenant_id, 'tenant id')
  const password = requireString(values.tenant_password, 'password')
  if (password === '') throw new Error('password is required')
  return { ...payload, tenant_password: password }
}

export function buildTenantPasswordReset(value) {
  const password = requireString(value, 'password')
  if (password === '') throw new Error('password is required')
  return { password }
}

export function tenantPasswordValidationError(value, { optional = false } = {}) {
  const password = requireString(value, 'password')
  if (password === '') return optional ? '' : 'Password is required'
  const characterCount = [...password].length
  if (characterCount < 12 || characterCount > 256) return 'Password must be 12-256 characters'
  if (password.trim() !== password) return 'Password must not start or end with space'
  if (password.toLowerCase().includes('password')) return 'Password must not contain password'
  return ''
}

export function tenantLoginPasswordEndpoint(tenantId) {
  return `/admin/tenants/${encodedTenantId(tenantId)}/login-password`
}

export function tenantLoginStatusEndpoint(tenantId) {
  return `/admin/tenants/${encodedTenantId(tenantId)}/login-status`
}

export function buildTenantLoginStatusPatch(status) {
  if (status !== 'active' && status !== 'disabled') throw new Error('invalid login status')
  return { status }
}

export function confirmedTenantLoginStatus(responseData, requestedStatus) {
  if (requestedStatus !== 'active' && requestedStatus !== 'disabled') return null
  if (!responseData || Array.isArray(responseData) || typeof responseData !== 'object') return null
  return responseData.ok === true && responseData.status === requestedStatus ? requestedStatus : null
}

export function createTenantActionLock() {
  const owners = new Map()
  return {
    acquire(tenantId, action) {
      const id = requireString(tenantId, 'tenant id')
      const owner = requireString(action, 'action')
      if (owners.has(id)) return false
      owners.set(id, owner)
      return true
    },
    release(tenantId, action) {
      const id = requireString(tenantId, 'tenant id')
      if (owners.get(id) !== action) return false
      owners.delete(id)
      return true
    },
    isBusy(tenantId) {
      return owners.has(requireString(tenantId, 'tenant id'))
    },
  }
}

export function projectTenantLoginState(row = {}) {
  if (row?.has_login_account === false) return { hasAccount: false, status: null, kind: 'none' }
  if (row?.has_login_account !== true) return { hasAccount: null, status: null, kind: 'unknown' }
  if (row.login_status === 'active' || row.login_status === 'disabled') {
    return { hasAccount: true, status: row.login_status, kind: row.login_status }
  }
  return { hasAccount: true, status: null, kind: 'unknown' }
}

export function createTenantRequestGeneration() {
  let current = 0
  return {
    begin(tenantId, action) {
      current += 1
      return { generation: current, tenantId: String(tenantId ?? ''), action: String(action ?? '') }
    },
    isCurrent(ticket, tenantId, action) {
      return Boolean(ticket)
        && ticket.generation === current
        && ticket.tenantId === String(tenantId ?? '')
        && ticket.action === String(action ?? '')
    },
    invalidate() { current += 1 },
  }
}

export function getTenantStats(items = []) {
  return items.reduce((stats, row) => {
    stats.total += 1
    if (row.enabled) stats.enabled += 1
    else stats.disabled += 1
    return stats
  }, { total: 0, enabled: 0, disabled: 0 })
}

export function filterTenants(items = [], filters = EMPTY_FILTERS) {
  const query = normalized(filters.query)
  return items.filter((row) => {
    const matchesQuery = !query || [row.display_name, row.tenant_id]
      .some((value) => normalized(value).includes(query))
    const matchesEnabled = filters.enabled === 'all'
      || (filters.enabled === 'enabled' ? Boolean(row.enabled) : !row.enabled)
    return matchesQuery && matchesEnabled
  })
}
