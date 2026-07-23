function cleanText(value) {
  return typeof value === 'string' ? value.trim() : ''
}

export const CONNECTOR_CARDS = Object.freeze([
  Object.freeze({ key: 'wecom', title: '企业微信', description: '平台内置代码连接器' }),
  Object.freeze({ key: 'http_declarative', title: 'OpenAPI', description: '通过受控接口配置生成 MCP 工具' }),
])

export function connectorCard(key) {
  return CONNECTOR_CARDS.find(card => card.key === key) || null
}

function requireAdminTenant(tenantId) {
  const value = cleanText(tenantId)
  if (!value) throw new Error('Admin service operations require a tenant scope')
  return value
}

export function serviceCollectionEndpoint(scope, tenantId = '') {
  if (scope === 'tenant') return '/tenant/services'
  return `/admin/tenants/${encodeURIComponent(requireAdminTenant(tenantId))}/services`
}

export function serviceResourceEndpoint(scope, tenantId, serviceId, suffix = '') {
  const base = `${serviceCollectionEndpoint(scope, tenantId)}/${encodeURIComponent(serviceId)}`
  const segments = cleanText(suffix).split('/').filter(Boolean).map(encodeURIComponent)
  return segments.length ? `${base}/${segments.join('/')}` : base
}

export function apiClientEndpoint(apiClient, endpoint) {
  const baseURL = String(apiClient?.defaults?.baseURL || '').replace(/\/$/, '')
  if (baseURL === '/tenant' && endpoint.startsWith('/tenant/')) return endpoint.slice('/tenant'.length)
  return endpoint
}

export function parseServiceLocation(search = '') {
  const params = new URLSearchParams(search)
  return { tenantId: cleanText(params.get('tenant_id')) }
}

export function serializeServiceLocation(filters = {}) {
  const params = new URLSearchParams()
  const tenantId = cleanText(filters.tenantId)
  if (tenantId) params.set('tenant_id', tenantId)
  return params.toString()
}

export function defaultToolAlias(connectionAlias, sourceMcpName) {
  return `${connectionAlias}__${sourceMcpName}`
}

export function bindingAliasPreview(connection = {}, tool = {}) {
  const connectionAlias = cleanText(connection.connection_alias)
  if (!connectionAlias) throw new Error('Authoritative connection alias is required')
  const sourceMcpName = cleanText(tool.mcp_name || tool.tool_key)
  if (!sourceMcpName) throw new Error('Source MCP name is required')
  return defaultToolAlias(connectionAlias, sourceMcpName)
}

export function aliasConflicts(bindings = []) {
  const counts = new Map()
  for (const binding of bindings) {
    const alias = cleanText(binding?.tool_alias)
    if (!alias) continue
    const canonical = alias.toLowerCase()
    const current = counts.get(canonical)
    counts.set(canonical, current
      ? { alias: current.alias, count: current.count + 1 }
      : { alias, count: 1 })
  }
  return [...counts.values()]
    .filter(({ count }) => count > 1)
    .map(({ alias }) => alias)
    .sort()
}

export function bindingPayload(binding = {}) {
  if (!['active', 'disabled'].includes(binding.binding_status)) {
    throw new Error('Binding status must be active or disabled')
  }
  if (!binding.policy || Array.isArray(binding.policy) || typeof binding.policy !== 'object') {
    throw new Error('Binding policy must be an object')
  }
  return {
    connection_id: cleanText(binding.connection_id),
    source_tool_key: cleanText(binding.source_tool_key),
    tool_alias: cleanText(binding.tool_alias),
    binding_status: binding.binding_status,
    policy: { ...binding.policy },
  }
}

export function tokenCanCopy(token = {}) {
  return cleanText(token.raw_value).length > 0
}

const RFC3339_SECONDS = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:Z|[+-]\d{2}:\d{2})$/
const LOCAL_DATE_TIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/

function tokenIsRevoked(token = {}) {
  return Boolean(token.revoked_at) || token.revoked === true || token.status === 'revoked'
}

export function canonicalTokenTimestamp(value) {
  const timestamp = cleanText(value)
  const match = RFC3339_SECONDS.exec(timestamp)
  if (!match) return ''
  const [, year, month, day, hour, minute, second] = match
  const calendar = new Date(Date.UTC(+year, +month - 1, +day, +hour, +minute, +second))
  if (
    +year < 1000
    || calendar.getUTCFullYear() !== +year
    || calendar.getUTCMonth() !== +month - 1
    || calendar.getUTCDate() !== +day
    || calendar.getUTCHours() !== +hour
    || calendar.getUTCMinutes() !== +minute
    || calendar.getUTCSeconds() !== +second
  ) return ''
  const parsed = new Date(timestamp)
  if (!Number.isFinite(parsed.getTime())) return ''
  return parsed.toISOString().replace('.000Z', 'Z')
}

export function serviceTokenIssuePayload(label, localExpiresAt = '') {
  const value = cleanText(localExpiresAt)
  if (!value) return { label: cleanText(label), expires_at: null }
  const match = LOCAL_DATE_TIME.exec(value)
  if (!match) throw new Error('Token expiry must be a valid local date and time')
  const [, year, month, day, hour, minute, second = '00'] = match
  const parsed = new Date(+year, +month - 1, +day, +hour, +minute, +second, 0)
  if (
    !Number.isFinite(parsed.getTime())
    || parsed.getFullYear() !== +year
    || parsed.getMonth() !== +month - 1
    || parsed.getDate() !== +day
    || parsed.getHours() !== +hour
    || parsed.getMinutes() !== +minute
    || parsed.getSeconds() !== +second
  ) throw new Error('Token expiry must be a valid local date and time')
  return {
    label: cleanText(label),
    expires_at: parsed.toISOString().replace('.000Z', 'Z'),
  }
}

export function tokenCanReveal(token = {}, now = new Date()) {
  if (!cleanText(token.token_id) || tokenIsRevoked(token)) return false
  if (token.expires_at == null) return true
  const expiresAt = canonicalTokenTimestamp(token.expires_at)
  const nowTime = now instanceof Date ? now.getTime() : Number.NaN
  return Boolean(expiresAt) && Number.isFinite(nowTime) && new Date(expiresAt).getTime() > nowTime
}

export function tokenCanRevoke(token = {}) {
  return Boolean(cleanText(token.token_id))
    && !tokenIsRevoked(token)
}

export function tokenLifecycleStatus(token = {}, now = new Date()) {
  if (tokenIsRevoked(token)) return 'revoked'
  return tokenCanReveal(token, now) ? 'active' : 'expired'
}

export function createTokenRevealSequence() {
  let current = 0
  return {
    begin(serviceId, tokenId) {
      current += 1
      return { requestId: current, serviceId: cleanText(serviceId), tokenId: cleanText(tokenId) }
    },
    isCurrent(ticket, serviceId, tokenId) {
      return Boolean(ticket)
        && ticket.requestId === current
        && ticket.serviceId === cleanText(serviceId)
        && ticket.tokenId === cleanText(tokenId)
    },
    invalidate() { current += 1 },
  }
}

export async function copyTokenWithLifecycle({
  sequence,
  serviceId,
  tokenId,
  rawToken,
  writeText,
  isActive,
  onCurrentFailure,
}) {
  const ticket = sequence.begin(serviceId, tokenId)
  try {
    await writeText(rawToken)
  } catch (error) {
    if (!isActive() || !sequence.isCurrent(ticket, serviceId, tokenId)) return 'stale'
    onCurrentFailure(error)
    return 'failed'
  }
  if (!isActive() || !sequence.isCurrent(ticket, serviceId, tokenId)) return 'stale'
  return 'copied'
}

export function closeServiceTokenState() {
  return {
    open: false,
    serviceId: '',
    tokenId: '',
    rawToken: '',
    revealBusy: false,
    error: '',
  }
}

export function failedServiceTokenState(serviceId, error) {
  return {
    open: true,
    serviceId: cleanText(serviceId),
    tokenId: '',
    rawToken: '',
    revealBusy: false,
    error: cleanText(error),
  }
}

export function safeServiceError(error, fallback = '操作失败，请稍后重试') {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && /^(resource not found|service configuration changed|service operation rejected|request rate limit exceeded)$/i.test(detail)) {
    return detail
  }
  return fallback
}
