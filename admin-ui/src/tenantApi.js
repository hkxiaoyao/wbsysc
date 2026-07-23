import axios from 'axios'

const tenantApi = axios.create({
  baseURL: '/tenant',
  withCredentials: true,
})

tenantApi.interceptors.response.use(
  response => response,
  error => {
    if (
      error.response?.status === 401
      && !error.config?.suppressSessionExpired
      && typeof window !== 'undefined'
    ) {
      window.dispatchEvent(new Event('tenant-session-expired'))
    }
    return Promise.reject(error)
  },
)

export function loginTenant(tenantId, password) {
  return tenantApi.post('/login', { tenant_id: tenantId, password })
}

export function logoutTenant(config = {}) {
  return tenantApi.post('/logout', undefined, {
    ...config,
    suppressSessionExpired: true,
  })
}

export default tenantApi
