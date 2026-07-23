export const TENANT_VIEWS = Object.freeze([
  'overview',
  'connections',
  'services',
  'logs',
  'account',
])

export function normalizeTenantView(view) {
  return TENANT_VIEWS.includes(view) ? view : 'overview'
}

export function parseTenantLocation(search = '') {
  const params = new URLSearchParams(search)
  return { view: normalizeTenantView(params.get('view')) }
}

export function serializeTenantLocation(view) {
  const params = new URLSearchParams()
  params.set('view', normalizeTenantView(view))
  return `?${params.toString()}`
}

export function tenantUrl(view, pathname = '/tenant/ui/') {
  return `${pathname}${serializeTenantLocation(view)}`
}

export function createTenantLogoutSequence() {
  let revision = 0
  return {
    begin() {
      revision += 1
      return revision
    },
    invalidate() {
      revision += 1
    },
    isCurrent(requestId) {
      return requestId === revision
    },
  }
}

export async function executeTenantLogout({
  request,
  isCurrent,
  onLoggedOut,
  onError,
}) {
  try {
    await request()
    if (!isCurrent()) return 'stale'
    onLoggedOut()
    return 'logged-out'
  } catch {
    if (!isCurrent()) return 'stale'
    onError('退出登录失败，请重试')
    return 'failed'
  }
}
