const DAY_MS = 24 * 60 * 60 * 1000
const DEFAULT_PAGE = 1
const DEFAULT_PAGE_SIZE = 20
export const MAX_DELETE_IDS = 200
export const MAX_LOG_ID = '9223372036854775807'

const CATEGORY_VALUES = new Set(['tool', 'protocol', 'auth'])
const STATUS_VALUES = new Set(['ok', 'partial', 'error', 'denied'])
const ISO_TIMESTAMP_WITH_ZONE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/i

const STATUS_META = Object.freeze({
  ok: Object.freeze({ label: '成功', color: 'success' }),
  partial: Object.freeze({ label: '部分成功', color: 'warning' }),
  error: Object.freeze({ label: '错误', color: 'error' }),
  denied: Object.freeze({ label: '已拒绝', color: 'default' }),
})

const UNKNOWN_STATUS_META = Object.freeze({ label: '未知', color: 'default' })

export const DEFAULT_LOG_FILTERS = Object.freeze({
  tenantId: '',
  connectionId: '',
  connectorKey: '',
  category: '',
  eventName: '',
  status: '',
  from: '',
  to: '',
  keyword: '',
  requestId: '',
  clientIp: '',
  costMin: '',
  costMax: '',
})

function normalizeText(value) {
  return String(value ?? '').trim()
}

export function normalizeLogKeyword(value) {
  return [...normalizeText(value)].slice(0, 100).join('')
}

function normalizeEnum(value, allowedValues) {
  const normalized = normalizeText(value)
  return allowedValues.has(normalized) ? normalized : ''
}

function normalizeCost(value) {
  const candidate = typeof value === 'string' ? value.trim() : value
  if (candidate === '' || candidate === null || candidate === undefined) return ''
  const normalized = Number(candidate)
  return Number.isFinite(normalized) && normalized >= 0 ? normalized : ''
}

function normalizeIso(value) {
  if (value === '' || value === null || value === undefined) return ''
  const text = value instanceof Date ? '' : normalizeText(value)
  if (!(value instanceof Date) && !ISO_TIMESTAMP_WITH_ZONE.test(text)) return ''
  const timestamp = value instanceof Date ? value.getTime() : Date.parse(text)
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : ''
}

function normalizeRange(fromValue, toValue, defaultWhenInvalid = false) {
  const from = normalizeIso(fromValue)
  const to = normalizeIso(toValue)
  if (from && to && Date.parse(from) <= Date.parse(to)) return { from, to }
  if (!defaultWhenInvalid) return { from: '', to: '' }

  const now = Date.now()
  return {
    from: new Date(now - DAY_MS).toISOString(),
    to: new Date(now).toISOString(),
  }
}

function normalizeCosts(minValue, maxValue) {
  const costMin = normalizeCost(minValue)
  const costMax = normalizeCost(maxValue)
  if (costMin !== '' && costMax !== '' && costMin > costMax) {
    return { costMin: '', costMax: '' }
  }
  return { costMin, costMax }
}

function filterQuery(filters = DEFAULT_LOG_FILTERS) {
  const query = {}
  const tenantId = normalizeText(filters?.tenantId)
  const serviceId = normalizeText(filters?.serviceId)
  const toolAlias = normalizeText(filters?.toolAlias)
  const connectionId = normalizeText(filters?.connectionId)
  const sourceToolKey = normalizeText(filters?.sourceToolKey)
  const connectorKey = normalizeText(filters?.connectorKey)
  const category = normalizeEnum(filters?.category, CATEGORY_VALUES)
  const eventName = normalizeText(filters?.eventName)
  const status = normalizeEnum(filters?.status, STATUS_VALUES)
  const keyword = normalizeLogKeyword(filters?.keyword)
  const requestId = normalizeText(filters?.requestId)
  const clientIp = normalizeText(filters?.clientIp)
  const range = normalizeRange(filters?.from, filters?.to)
  const costs = normalizeCosts(filters?.costMin, filters?.costMax)

  if (tenantId) query.tenant_id = tenantId
  if (serviceId) query.service_id = serviceId
  if (toolAlias) query.tool_alias = toolAlias
  if (connectionId) query.connection_id = connectionId
  if (sourceToolKey) query.tool_key = sourceToolKey
  if (connectorKey) query.connector_key = connectorKey
  if (category) query.category = category
  if (eventName) query.event_name = eventName
  if (status) query.status = status
  if (range.from) query.from = range.from
  if (range.to) query.to = range.to
  if (keyword) query.q = keyword
  if (requestId) query.request_id = requestId
  if (clientIp) query.client_ip = clientIp
  if (costs.costMin !== '') query.cost_min = costs.costMin
  if (costs.costMax !== '') query.cost_max = costs.costMax

  return query
}

function tenantFilterQuery(filters = DEFAULT_LOG_FILTERS) {
  const query = {}
  const connectionId = normalizeText(filters?.connectionId)
  const sourceToolKey = normalizeText(filters?.sourceToolKey)
  const status = normalizeEnum(filters?.status, STATUS_VALUES)

  if (connectionId) query.connection_id = connectionId
  if (sourceToolKey) query.source_tool_key = sourceToolKey
  if (status) query.status = status
  return query
}

export function logCollectionEndpoint(scope = 'admin') {
  return scope === 'tenant' ? '/tenant/mcp-logs' : '/admin/mcp-logs'
}

export function logScopeCopy(scope = 'admin', filters = {}, tenantLabel = '全部租户') {
  if (scope === 'tenant') {
    return {
      perspective: '当前租户视角',
      identity: filters?.connectionId
        ? `当前会话租户 · ${normalizeText(filters.connectionId)}`
        : '当前会话租户',
    }
  }
  return {
    perspective: filters?.connectionId ? '连接视角' : filters?.tenantId ? '租户视角' : '全局视角',
    identity: normalizeText(filters?.connectionId) || tenantLabel,
  }
}

function positiveInteger(value, fallback, maximum = Number.MAX_SAFE_INTEGER) {
  const normalized = Number(value)
  return Number.isSafeInteger(normalized) && normalized > 0 && normalized <= maximum
    ? normalized
    : fallback
}

function hasValue(value) {
  return value !== null && value !== undefined && normalizeText(value) !== ''
}

function normalizeDeleteId(value) {
  let text
  if (typeof value === 'string') {
    text = value
  } else if (typeof value === 'number' && Number.isSafeInteger(value) && value > 0) {
    text = String(value)
  } else {
    throw new TypeError('Each log ID must be a valid decimal log ID or safe integer')
  }

  if (!/^[1-9][0-9]*$/.test(text)) {
    throw new TypeError('Each log ID must be a valid decimal log ID without signs or leading zeroes')
  }
  if (text.length > MAX_LOG_ID.length
    || (text.length === MAX_LOG_ID.length && text > MAX_LOG_ID)) {
    throw new RangeError('Each log ID must be a valid decimal log ID within signed BIGINT range')
  }
  return text
}

export function isDeleteSelectionOverLimit(selectedIds = []) {
  return selectedIds.length > MAX_DELETE_IDS
}

function validateDeleteFilters(filters) {
  if (hasValue(filters?.category) && !CATEGORY_VALUES.has(normalizeText(filters.category))) {
    throw new TypeError('Delete filter category must be a supported value')
  }
  if (hasValue(filters?.status) && !STATUS_VALUES.has(normalizeText(filters.status))) {
    throw new TypeError('Delete filter status must be a supported value')
  }

  const hasFrom = hasValue(filters?.from)
  const hasTo = hasValue(filters?.to)
  if (hasFrom !== hasTo || (hasFrom && !normalizeRange(filters.from, filters.to).from)) {
    throw new RangeError('Delete filter time range must contain valid ordered timestamps')
  }

  const hasCostMin = hasValue(filters?.costMin)
  const hasCostMax = hasValue(filters?.costMax)
  const costMin = normalizeCost(filters?.costMin)
  const costMax = normalizeCost(filters?.costMax)
  if ((hasCostMin && costMin === '') || (hasCostMax && costMax === '')) {
    throw new TypeError('Delete filter cost range must contain non-negative numbers')
  }
  if (hasCostMin && hasCostMax && costMin > costMax) {
    throw new RangeError('Delete filter cost range must be ordered')
  }
}

export function parseLogLocation(search = '') {
  const params = new URLSearchParams(search)
  const range = normalizeRange(params.get('from'), params.get('to'), true)
  const costs = normalizeCosts(params.get('cost_min'), params.get('cost_max'))

  return {
    tenantId: normalizeText(params.get('tenant_id')),
    connectionId: normalizeText(params.get('connection_id')),
    connectorKey: normalizeText(params.get('connector_key')),
    category: normalizeEnum(params.get('category'), CATEGORY_VALUES),
    eventName: normalizeText(params.get('event_name')),
    status: normalizeEnum(params.get('status'), STATUS_VALUES),
    from: range.from,
    to: range.to,
    keyword: normalizeLogKeyword(params.get('q')),
    requestId: normalizeText(params.get('request_id')),
    clientIp: normalizeText(params.get('client_ip')),
    costMin: costs.costMin,
    costMax: costs.costMax,
  }
}

export function parseScopedLogLocation(scope = 'admin', search = '') {
  const filters = parseLogLocation(search)
  if (scope !== 'tenant') return filters
  const params = new URLSearchParams(search)
  return {
    ...filters,
    tenantId: '',
    sourceToolKey: normalizeText(params.get('source_tool_key')),
  }
}

export function serializeLogFilters(filters = DEFAULT_LOG_FILTERS) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filterQuery(filters))) {
    params.set(key, String(value))
  }
  return params.toString()
}

export function serializeScopedLogFilters(scope = 'admin', filters = DEFAULT_LOG_FILTERS) {
  const query = scope === 'tenant' ? tenantFilterQuery(filters) : filterQuery(filters)
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) params.set(key, String(value))
  return params.toString()
}

export function buildLogQuery(filters = DEFAULT_LOG_FILTERS, page = DEFAULT_PAGE, pageSize = DEFAULT_PAGE_SIZE) {
  return {
    ...filterQuery(filters),
    page: positiveInteger(page, DEFAULT_PAGE),
    page_size: positiveInteger(pageSize, DEFAULT_PAGE_SIZE, 100),
  }
}

export function buildScopedLogQuery(scope = 'admin', filters = DEFAULT_LOG_FILTERS, page = DEFAULT_PAGE, pageSize = DEFAULT_PAGE_SIZE) {
  return {
    ...(scope === 'tenant' ? tenantFilterQuery(filters) : filterQuery(filters)),
    page: positiveInteger(page, DEFAULT_PAGE),
    page_size: positiveInteger(pageSize, DEFAULT_PAGE_SIZE, 100),
  }
}

export function buildDeleteSpec(mode, filters = DEFAULT_LOG_FILTERS, selectedIds = [], beforeDate = null) {
  if (mode === 'ids') {
    const ids = [...new Set(selectedIds.map(normalizeDeleteId))]
    if (ids.length === 0) throw new RangeError('At least one log ID is required')
    if (ids.length > MAX_DELETE_IDS) {
      throw new RangeError(`一次最多清理 ${MAX_DELETE_IDS} 条日志 ID`)
    }
    return { mode, ids }
  }

  if (mode === 'filter') {
    validateDeleteFilters(filters)
    return { mode, filter: filterQuery(filters) }
  }

  if (mode === 'before_date') {
    const normalizedDate = normalizeIso(beforeDate)
    if (!normalizedDate) throw new TypeError('before date must be a valid timestamp')
    return { mode, before_date: normalizedDate }
  }

  if (mode === 'all') return { mode }

  throw new RangeError(`Unsupported delete mode: ${String(mode)}`)
}

export function formatDuration(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(Number(ms)) || Number(ms) < 0) return '—'
  const duration = Number(ms)
  return duration < 1000 ? `${Math.round(duration)} ms` : `${(duration / 1000).toFixed(2)} s`
}

export function statusMeta(status) {
  return STATUS_META[status] ?? UNKNOWN_STATUS_META
}
