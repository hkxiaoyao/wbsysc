import axios from 'axios'

// 全局 axios：带 session token，401 跳登录
const api = axios.create({ baseURL: '', withCredentials: true })

const TOKEN_KEY = 'wbg_admin_token'
export function getToken() { return localStorage.getItem(TOKEN_KEY) || '' }
export function setToken(t) { localStorage.setItem(TOKEN_KEY, t) }
export function clearToken() { localStorage.removeItem(TOKEN_KEY) }

api.interceptors.request.use(cfg => {
  const t = getToken()
  if (t) cfg.headers.Authorization = `Bearer ${t}`
  return cfg
})

api.interceptors.response.use(
  r => r,
  err => {
    if (err.response?.status === 401) {
      clearToken()
      // 跳登录（由 App 监听 token 清空处理，避免硬跳转）
    }
    return Promise.reject(err)
  }
)

export default api
