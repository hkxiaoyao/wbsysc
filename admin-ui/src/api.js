import axios from 'axios'

// 全局 axios：带 session token，401 跳登录
const api = axios.create({ baseURL: '', withCredentials: true })

const TOKEN_KEY = 'wbg_admin_token'
const SESSION_EXPIRED_EVENT = 'admin-session-expired'
export function getToken() { return localStorage.getItem(TOKEN_KEY) || '' }
export function setToken(t) { localStorage.setItem(TOKEN_KEY, t) }
export function clearToken() { localStorage.removeItem(TOKEN_KEY) }
export function expireAdminSession(storage = globalThis.localStorage, eventTarget = globalThis.window) {
  storage?.removeItem(TOKEN_KEY)
  const EventConstructor = eventTarget?.Event || globalThis.Event
  if (typeof eventTarget?.dispatchEvent !== 'function') return
  if (typeof EventConstructor !== 'function') throw new Error('Event constructor unavailable')
  eventTarget.dispatchEvent(new EventConstructor(SESSION_EXPIRED_EVENT))
}
export function subscribeAdminSessionExpired(onExpired, eventTarget = globalThis.window) {
  if (typeof eventTarget?.addEventListener !== 'function') return () => {}
  eventTarget.addEventListener(SESSION_EXPIRED_EVENT, onExpired)
  return () => eventTarget.removeEventListener(SESSION_EXPIRED_EVENT, onExpired)
}

api.interceptors.request.use(cfg => {
  const t = getToken()
  if (t) cfg.headers.Authorization = `Bearer ${t}`
  return cfg
})

api.interceptors.response.use(
  r => r,
  err => {
    if (err.response?.status === 401) {
      expireAdminSession()
    }
    return Promise.reject(err)
  }
)

export default api
