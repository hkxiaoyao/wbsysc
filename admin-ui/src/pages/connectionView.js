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
    mustDisable: connection.status === 'active',
  }
}

export function hasExplicitPolicies(tools = [], policies = []) {
  const expected = new Set(tools.map((tool) => cleanText(tool.tool_key)).filter(Boolean))
  const actual = new Set(policies.map((policy) => cleanText(policy.tool_key)).filter(Boolean))
  if (expected.size !== actual.size) return false
  return [...expected].every((toolKey) => actual.has(toolKey))
}
