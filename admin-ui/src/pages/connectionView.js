const SENSITIVE_DETAIL = /(authorization|bearer|cookie|credential|password|secret|token|api[_-]?key|upstream|response\s*body)/i
const SAFE_DETAIL = /^(connection |declarative |invalid |use declarative |no safe read |tool policy |sync unavailable|connector registry)/i

function cleanText(value) {
  return String(value ?? '').trim()
}

export function buildConnectionMcpConfig(connection, origin) {
  const connectionId = cleanText(connection?.connection_id)
  const rawToken = cleanText(connection?.initial_token ?? connection?.token)
  const normalizedOrigin = cleanText(origin).replace(/\/+$/, '')
  return JSON.stringify({
    mcpServers: {
      [connectionId]: {
        type: 'http',
        url: `${normalizedOrigin}/mcp/${encodeURIComponent(connectionId)}`,
        headers: { Authorization: `Bearer ${rawToken}` },
      },
    },
  }, null, 2)
}

export function closeTokenModal() {
  return { open: false, rawToken: '', connectionId: '' }
}

export function canEnableWriteTool(tool, consent = {}) {
  if (!consent.explicitEnable) return false
  return tool?.operation_kind !== 'write' || consent.explicitWrite === true
}

export function safeServerError(error, fallback = '操作失败，请稍后重试') {
  const detail = error?.response?.data?.detail
  if (typeof detail !== 'string' || !detail.trim()) return fallback
  const normalized = detail.trim().slice(0, 240)
  if (SENSITIVE_DETAIL.test(normalized)) return '操作失败，请检查连接配置'
  return SAFE_DETAIL.test(normalized) ? normalized : fallback
}

export function serializeConnectionLocation(filters = {}) {
  const params = new URLSearchParams()
  const tenantId = cleanText(filters.tenantId)
  const connectionId = cleanText(filters.connectionId)
  if (tenantId) params.set('tenant_id', tenantId)
  if (connectionId) params.set('connection_id', connectionId)
  return params.toString()
}

export function parseConnectionLocation(search = '') {
  const params = new URLSearchParams(search)
  return {
    tenantId: cleanText(params.get('tenant_id')),
    connectionId: cleanText(params.get('connection_id')),
  }
}

export function createWizardState(connection = {}) {
  return {
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
    mustDisable: connection.status === 'active',
  }
}

export function hasExplicitPolicies(tools = [], policies = []) {
  const expected = new Set(tools.map((tool) => cleanText(tool.tool_key)).filter(Boolean))
  const actual = new Set(policies.map((policy) => cleanText(policy.tool_key)).filter(Boolean))
  if (expected.size !== actual.size || policies.length !== expected.size) return false
  return tools.every((tool) => {
    const policy = policies.find((item) => cleanText(item.tool_key) === cleanText(tool.tool_key))
    if (!policy || typeof policy.enabled !== 'boolean') return false
    return tool.operation_kind !== 'write' || !policy.enabled || policy.allow_write === true
  })
}

export function isActiveDeclarativeConfigReadOnly(connection = {}) {
  return connection.connector_key === 'http_declarative' && connection.status === 'active'
}

export function invalidateWizardState(state, scope) {
  if (scope === 'source') {
    return {
      ...state,
      imported: false,
      validated: false,
      published: false,
      mappingReviewed: false,
      credentialsSaved: false,
      policiesSaved: false,
      tested: false,
      activated: false,
    }
  }
  const reset = { ...state, tested: false, activated: false }
  if (scope === 'credentials') reset.credentialsSaved = false
  if (scope === 'mapping') reset.mappingReviewed = false
  if (scope === 'policy') reset.policiesSaved = false
  return reset
}

export function setExplicitToolPolicy(tool, current, enabled) {
  return {
    tool_key: cleanText(tool?.tool_key),
    enabled: Boolean(enabled),
    allow_write: false,
  }
}

export function selectActiveTokenHint(tokens = []) {
  const active = tokens.find((token) => (
    !token?.revoked_at && token?.revoked !== true && token?.status !== 'revoked'
    && (!token?.expires_at || Date.parse(token.expires_at) > Date.now())
  ))
  return cleanText(active?.prefix ?? active?.token_prefix)
}

export function wizardRevisionIdentity(state = {}) {
  return { specId: cleanText(state.specId), revision: Number(state.revision) }
}

export function createRequestSequence() {
  let current = 0
  return {
    begin() { current += 1; return current },
    isCurrent(value) { return value === current },
    invalidate() { current += 1 },
  }
}

export function schemaMetadataSummary(schema) {
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return null
  if (schema.properties && typeof schema.properties === 'object') {
    return {
      required: Array.isArray(schema.required) ? schema.required : [],
      properties: schema.properties,
    }
  }
  return schema
}

export function requiredCredentialKeys(schema) {
  if (!schema || !Array.isArray(schema.required)) return []
  return schema.required.filter((key) => typeof key === 'string' && key.length > 0)
}
