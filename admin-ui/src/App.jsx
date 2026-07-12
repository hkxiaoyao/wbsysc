import { useEffect, useState } from 'react'
import { Layout, message } from 'antd'
import Login from './pages/Login.jsx'
import Tenants from './pages/Tenants.jsx'
import api, { getToken, setToken, clearToken } from './api.js'

const { Header, Content } = Layout

export default function App() {
  const [authed, setAuthed] = useState(!!getToken())

  // 校验 session 是否仍有效
  useEffect(() => {
    if (!getToken()) { setAuthed(false); return }
    api.get('/admin/session').then(r => setAuthed(r.data.authed)).catch(() => setAuthed(false))
  }, [])

  const onLogin = (token) => {
    setToken(token)
    setAuthed(true)
    message.success('登录成功')
  }

  const onLogout = async () => {
    try { await api.post('/admin/logout') } catch {}
    clearToken()
    setAuthed(false)
  }

  if (!authed) return <Login onLogin={onLogin} />

  return (
    <Layout style={{ height: '100%' }}>
      <Header style={{ color: '#fff', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>企微数据中转 · 管理后台</span>
        <a onClick={onLogout} style={{ color: '#fff' }}>退出登录</a>
      </Header>
      <Content style={{ padding: 24 }}>
        <Tenants />
      </Content>
    </Layout>
  )
}
