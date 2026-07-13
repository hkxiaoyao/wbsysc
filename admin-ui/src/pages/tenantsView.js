export const EMPTY_FILTERS = Object.freeze({
  query: '',
  dataMode: 'all',
  enabled: 'all',
})

const normalized = (value) => String(value ?? '').trim().toLocaleLowerCase()

export function getTenantStats(items = []) {
  return items.reduce((stats, row) => {
    stats.total += 1
    if (row.enabled) stats.running += 1
    if (row.data_mode === 'direct') stats.direct += 1
    if (!row.enabled || !row.has_secret) stats.attention += 1
    return stats
  }, { total: 0, running: 0, direct: 0, attention: 0 })
}

export function filterTenants(items = [], filters = EMPTY_FILTERS) {
  const query = normalized(filters.query)
  return items.filter((row) => {
    const matchesQuery = !query || [row.display_name, row.tenant_id, row.corpid]
      .some((value) => normalized(value).includes(query))
    const matchesMode = filters.dataMode === 'all' || row.data_mode === filters.dataMode
    const matchesEnabled = filters.enabled === 'all'
      || (filters.enabled === 'enabled' ? Boolean(row.enabled) : !row.enabled)
    return matchesQuery && matchesMode && matchesEnabled
  })
}

export function getDirectModeReason(row) {
  return row?.data_mode === 'direct' ? '直连模式实时调用企微 API，无需同步' : ''
}
