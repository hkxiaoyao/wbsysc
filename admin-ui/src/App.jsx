import { useCallback, useEffect, useState } from 'react'
import { Button, Layout, message } from 'antd'
import Login from './pages/Login.jsx'
import McpLogs from './pages/McpLogs.jsx'
import Tenants from './pages/Tenants.jsx'
import Connections from './pages/Connections.jsx'
import { parseLogLocation, serializeLogFilters } from './pages/mcpLogsView.js'
import { parseConnectionLocation, serializeConnectionLocation } from './pages/connectionView.js'
import api, { getToken, setToken, clearToken } from './api.js'

const { Header, Content } = Layout

function readAdminLocation() {
  const params = new URLSearchParams(window.location.search)
  return {
    view: ['logs', 'connections'].includes(params.get('view')) ? params.get('view') : 'tenants',
    logFilters: parseLogLocation(window.location.search),
    connectionFilters: parseConnectionLocation(window.location.search),
  }
}

function adminUrl(view, logFilters, connectionFilters) {
  const params = new URLSearchParams()
  params.set('view', view)
  if (view === 'logs') {
    const filterParams = new URLSearchParams(serializeLogFilters(logFilters))
    for (const [key, value] of filterParams) params.set(key, value)
  }
  if (view === 'connections') {
    const filterParams = new URLSearchParams(serializeConnectionLocation(connectionFilters))
    for (const [key, value] of filterParams) params.set(key, value)
  }
  return `${window.location.pathname}?${params.toString()}${window.location.hash}`
}

export default function App() {
  const [authed, setAuthed] = useState(!!getToken())
  const [locationState, setLocationState] = useState(readAdminLocation)
  const [messageApi, messageContextHolder] = message.useMessage()

  // 校验 session 是否仍有效
  useEffect(() => {
    if (!getToken()) { setAuthed(false); return }
    api.get('/admin/session').then(r => setAuthed(r.data.authed)).catch(() => setAuthed(false))
  }, [])

  useEffect(() => {
    const restoreLocation = () => setLocationState(readAdminLocation())
    window.addEventListener('popstate', restoreLocation)
    return () => window.removeEventListener('popstate', restoreLocation)
  }, [])

  const onLogin = (token) => {
    setToken(token)
    setAuthed(true)
    messageApi.success('登录成功')
  }

  const onLogout = async () => {
    try { await api.post('/admin/logout') } catch {}
    clearToken()
    setAuthed(false)
  }

  const applyLocation = useCallback((view, logFilters, connectionFilters = locationState.connectionFilters) => {
    const nextUrl = adminUrl(view, logFilters, connectionFilters)
    const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`
    if (nextUrl !== currentUrl) window.history.pushState({}, '', nextUrl)
    setLocationState({ view, logFilters, connectionFilters })
  }, [locationState.connectionFilters])

  const navigate = useCallback((view, logFilters = locationState.logFilters, connectionFilters = locationState.connectionFilters) => {
    if (view === locationState.view && logFilters === locationState.logFilters) return
    applyLocation(view, logFilters, connectionFilters)
  }, [applyLocation, locationState])

  const onLogFiltersChange = useCallback((logFilters) => {
    applyLocation('logs', logFilters)
  }, [applyLocation])

  const onViewLogs = useCallback((tenantId) => {
    navigate('logs', { ...parseLogLocation(''), tenantId })
  }, [navigate])

  const onViewConnectionLogs = useCallback((connection) => {
    navigate('logs', {
      ...parseLogLocation(''),
      tenantId: connection.tenant_id,
      connectionId: connection.connection_id,
      connectorKey: connection.connector_key,
    })
  }, [navigate])

  const onViewConnections = useCallback((tenantId) => {
    navigate('connections', locationState.logFilters, { tenantId, connectionId: '' })
  }, [navigate, locationState.logFilters])

  if (!authed) {
    return (
      <>
        {messageContextHolder}
        <Login onLogin={onLogin} />
      </>
    )
  }

  return (
    <>
      {messageContextHolder}
      <Layout className="admin-shell">
        <Header className="admin-header">
          <span className="admin-brand">企微数据中转 <span>· 管理后台</span></span>
          <nav className="admin-nav" aria-label="管理后台主导航">
            <Button
              type="text"
              className={locationState.view === 'connections' ? 'admin-nav__item admin-nav__item--active' : 'admin-nav__item'}
              aria-current={locationState.view === 'connections' ? 'page' : undefined}
              aria-label="连接实例"
              onClick={() => navigate('connections')}
            >
              <span className="admin-nav__full" aria-hidden="true">连接实例</span>
              <span className="admin-nav__short" aria-hidden="true">连接</span>
            </Button>
            <Button
              type="text"
              className={locationState.view === 'tenants' ? 'admin-nav__item admin-nav__item--active' : 'admin-nav__item'}
              aria-current={locationState.view === 'tenants' ? 'page' : undefined}
              aria-label="租户管理"
              onClick={() => navigate('tenants')}
            >
              <span className="admin-nav__full" aria-hidden="true">租户管理</span>
              <span className="admin-nav__short" aria-hidden="true">租户</span>
            </Button>
            <Button
              type="text"
              className={locationState.view === 'logs' ? 'admin-nav__item admin-nav__item--active' : 'admin-nav__item'}
              aria-current={locationState.view === 'logs' ? 'page' : undefined}
              aria-label="调用日志"
              onClick={() => navigate('logs')}
            >
              <span className="admin-nav__full" aria-hidden="true">调用日志</span>
              <span className="admin-nav__short" aria-hidden="true">日志</span>
            </Button>
          </nav>
          <Button type="text" className="admin-logout" onClick={onLogout}>退出登录</Button>
        </Header>
        <Content className="admin-content">
          {locationState.view === 'logs' ? (
            <McpLogs
              filters={locationState.logFilters}
              onFiltersChange={onLogFiltersChange}
            />
          ) : locationState.view === 'connections' ? (
            <Connections
              tenantId={locationState.connectionFilters.tenantId}
              initialConnectionId={locationState.connectionFilters.connectionId}
              onViewLogs={onViewConnectionLogs}
            />
          ) : (
            <Tenants onViewLogs={onViewLogs} onViewConnections={onViewConnections} onViewConnectionLogs={onViewConnectionLogs} />
          )}
        </Content>
      </Layout>
    </>
  )
}
