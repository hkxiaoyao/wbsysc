import test from 'node:test'
import assert from 'node:assert/strict'
import {
  EMPTY_FILTERS,
  filterTenants,
  getDirectModeReason,
  getTenantStats,
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
