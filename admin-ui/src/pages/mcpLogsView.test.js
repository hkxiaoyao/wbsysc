import test from 'node:test'
import assert from 'node:assert/strict'
import {
  DEFAULT_LOG_FILTERS,
  buildDeleteSpec,
  buildLogQuery,
  formatDuration,
  normalizeLogKeyword,
  parseLogLocation,
  serializeLogFilters,
  statusMeta,
} from './mcpLogsView.js'

const EXPLICIT_FILTERS = Object.freeze({
  tenantId: 'tenant-a',
  category: 'tool',
  eventName: 'wecom_list_reports',
  status: 'partial',
  from: '2026-07-13T01:02:03.000Z',
  to: '2026-07-14T04:05:06.000Z',
  keyword: 'quarterly report',
  requestId: 'req-42',
  clientIp: '203.0.113.8',
  costMin: 25,
  costMax: 2500,
})

test('normalizeLogKeyword trims and caps API keywords at 100 Unicode characters', () => {
  assert.equal(normalizeLogKeyword('  quarterly report  '), 'quarterly report')
  assert.equal(normalizeLogKeyword('x'.repeat(101)), 'x'.repeat(100))
  assert.equal([...normalizeLogKeyword('😀'.repeat(101))].length, 100)
})

test('parseLogLocation restores tenant and defaults to the last 24 hours', () => {
  const before = Date.now()
  const filters = parseLogLocation('?tenant_id=tenant-a&status=error')
  const after = Date.now()

  assert.equal(filters.tenantId, 'tenant-a')
  assert.equal(filters.status, 'error')
  assert.ok(Date.parse(filters.to) >= before)
  assert.ok(Date.parse(filters.to) <= after)
  assert.equal(Date.parse(filters.to) - Date.parse(filters.from), 24 * 60 * 60 * 1000)
})

test('parseLogLocation restores every structured filter and normalizes timestamps', () => {
  const filters = parseLogLocation(
    '?tenant_id=tenant-a&category=tool&event_name=wecom_list_reports&status=partial'
      + '&from=2026-07-13T01%3A02%3A03Z&to=2026-07-14T04%3A05%3A06Z'
      + '&q=quarterly+report&request_id=req-42&client_ip=203.0.113.8'
      + '&cost_min=25&cost_max=2500',
  )

  assert.deepEqual(filters, EXPLICIT_FILTERS)
})

test('parseLogLocation ignores invalid enums and numbers and repairs an invalid range', () => {
  const before = Date.now()
  const filters = parseLogLocation(
    '?category=unknown&status=success&cost_min=-1&cost_max=fast'
      + '&from=2026-07-14T00%3A00%3A00Z&to=2026-07-13T00%3A00%3A00Z',
  )
  const after = Date.now()

  assert.equal(filters.category, '')
  assert.equal(filters.status, '')
  assert.equal(filters.costMin, '')
  assert.equal(filters.costMax, '')
  assert.ok(Date.parse(filters.to) >= before)
  assert.ok(Date.parse(filters.to) <= after)
  assert.equal(Date.parse(filters.to) - Date.parse(filters.from), 24 * 60 * 60 * 1000)
})

test('parseLogLocation rejects timestamps without an explicit timezone', () => {
  const before = Date.now()
  const filters = parseLogLocation(
    '?from=2026-07-13T01%3A02%3A03&to=2026-07-14T04%3A05%3A06',
  )
  const after = Date.now()

  assert.ok(Date.parse(filters.to) >= before)
  assert.ok(Date.parse(filters.to) <= after)
  assert.equal(Date.parse(filters.to) - Date.parse(filters.from), 24 * 60 * 60 * 1000)
})

test('serializeLogFilters is deterministic, omits defaults, and round trips filters', () => {
  const search = serializeLogFilters(EXPLICIT_FILTERS)

  assert.equal(
    search,
    'tenant_id=tenant-a&category=tool&event_name=wecom_list_reports&status=partial'
      + '&from=2026-07-13T01%3A02%3A03.000Z&to=2026-07-14T04%3A05%3A06.000Z'
      + '&q=quarterly+report&request_id=req-42&client_ip=203.0.113.8'
      + '&cost_min=25&cost_max=2500',
  )
  assert.deepEqual(parseLogLocation(`?${search}`), EXPLICIT_FILTERS)
  assert.equal(serializeLogFilters(DEFAULT_LOG_FILTERS), '')
})

test('buildLogQuery maps filters to API fields and normalizes pagination', () => {
  assert.deepEqual(buildLogQuery(EXPLICIT_FILTERS, 3, 50), {
    tenant_id: 'tenant-a',
    category: 'tool',
    event_name: 'wecom_list_reports',
    status: 'partial',
    from: '2026-07-13T01:02:03.000Z',
    to: '2026-07-14T04:05:06.000Z',
    q: 'quarterly report',
    request_id: 'req-42',
    client_ip: '203.0.113.8',
    cost_min: 25,
    cost_max: 2500,
    page: 3,
    page_size: 50,
  })
  assert.deepEqual(buildLogQuery(DEFAULT_LOG_FILTERS, 0, 101), {
    page: 1,
    page_size: 20,
  })
})

test('buildDeleteSpec produces every supported delete payload without UI pagination', () => {
  const pollutedFilters = { ...EXPLICIT_FILTERS, page: 9, pageSize: 100 }

  assert.deepEqual(buildDeleteSpec('ids', pollutedFilters, ['9', 2, 9, -1, 'no'], null), {
    mode: 'ids',
    ids: [9, 2],
  })
  assert.deepEqual(buildDeleteSpec('filter', pollutedFilters, [], null), {
    mode: 'filter',
    filter: {
      tenant_id: 'tenant-a',
      category: 'tool',
      event_name: 'wecom_list_reports',
      status: 'partial',
      from: '2026-07-13T01:02:03.000Z',
      to: '2026-07-14T04:05:06.000Z',
      q: 'quarterly report',
      request_id: 'req-42',
      client_ip: '203.0.113.8',
      cost_min: 25,
      cost_max: 2500,
    },
  })
  assert.deepEqual(buildDeleteSpec('before_date', pollutedFilters, [], '2026-07-01T00:00:00Z'), {
    mode: 'before_date',
    before_date: '2026-07-01T00:00:00.000Z',
  })
  assert.deepEqual(buildDeleteSpec('all', pollutedFilters, [], null), { mode: 'all' })
  assert.throws(() => buildDeleteSpec('sql', pollutedFilters, [], null), /delete mode/i)
})

test('buildDeleteSpec filter mode omits empty values and pagination fields', () => {
  const body = buildDeleteSpec(
    'filter',
    { ...DEFAULT_LOG_FILTERS, tenantId: 't1', page: 4, pageSize: 100 },
    [],
    null,
  )

  assert.deepEqual(body, { mode: 'filter', filter: { tenant_id: 't1' } })
})

test('buildDeleteSpec rejects destructive payloads with invalid explicit bounds', () => {
  assert.throws(
    () => buildDeleteSpec('ids', DEFAULT_LOG_FILTERS, [], null),
    /at least one log id/i,
  )
  assert.throws(
    () => buildDeleteSpec('filter', {
      ...DEFAULT_LOG_FILTERS,
      from: '2026-07-14T00:00:00Z',
      to: '2026-07-13T00:00:00Z',
    }, [], null),
    /time range/i,
  )
  assert.throws(
    () => buildDeleteSpec('filter', {
      ...DEFAULT_LOG_FILTERS,
      costMin: 500,
      costMax: 100,
    }, [], null),
    /cost range/i,
  )
  assert.throws(
    () => buildDeleteSpec('before_date', DEFAULT_LOG_FILTERS, [], '2026-07-01T00:00:00'),
    /valid timestamp/i,
  )
})

test('buildDeleteSpec rejects an explicitly invalid enum as the only filter', () => {
  assert.throws(
    () => buildDeleteSpec('filter', {
      ...DEFAULT_LOG_FILTERS,
      status: 'success',
    }, [], null),
    /status/i,
  )
})

test('buildDeleteSpec rejects tenant plus an explicitly invalid enum', () => {
  assert.throws(
    () => buildDeleteSpec('filter', {
      ...DEFAULT_LOG_FILTERS,
      tenantId: 'tenant-a',
      category: 'database',
    }, [], null),
    /category/i,
  )
})

test('whitespace-only cost strings are omitted instead of coerced to zero', () => {
  assert.deepEqual(buildLogQuery({
    ...DEFAULT_LOG_FILTERS,
    costMin: '   ',
    costMax: '\t',
  }, 1, 20), {
    page: 1,
    page_size: 20,
  })
})

test('formatDuration formats milliseconds and seconds with an invalid-value fallback', () => {
  assert.equal(formatDuration(0), '0 ms')
  assert.equal(formatDuration(999.4), '999 ms')
  assert.equal(formatDuration(1250), '1.25 s')
  assert.equal(formatDuration(null), '—')
  assert.equal(formatDuration(-1), '—')
  assert.equal(formatDuration(Number.NaN), '—')
})

test('statusMeta returns fixed metadata for every status and a safe fallback', () => {
  assert.deepEqual(statusMeta('ok'), { label: '成功', color: 'success' })
  assert.deepEqual(statusMeta('partial'), { label: '部分成功', color: 'warning' })
  assert.deepEqual(statusMeta('error'), { label: '错误', color: 'error' })
  assert.deepEqual(statusMeta('denied'), { label: '已拒绝', color: 'default' })
  assert.deepEqual(statusMeta('other'), { label: '未知', color: 'default' })
})
