import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Card, Col, Layout, Row, Spin, Statistic, Typography } from 'antd'

import Connections from './pages/Connections.jsx'
import McpLogs from './pages/McpLogs.jsx'
import Services from './pages/Services.jsx'
import TenantLogin from './pages/TenantLogin.jsx'
import tenantApi, { logoutTenant } from './tenantApi.js'
import { parseScopedLogLocation } from './pages/mcpLogsView.js'
import {
  createTenantLogoutSequence,
  executeTenantLogout,
  parseTenantLocation,
  serializeTenantLocation,
  tenantUrl,
} from './pages/tenantAppView.js'

const { Header, Content } = Layout
const { Paragraph, Title } = Typography

const NAV_ITEMS = [
  ['overview', '概览'],
  ['connections', '连接'],
  ['services', '服务'],
  ['logs', '日志'],
  ['account', '账户设置'],
]

const VIEW_COPY = {
  overview: ['租户概览', '查看当前租户的连接、服务和运行状态。'],
  connections: ['连接', '管理当前租户可用的数据连接。'],
  services: ['MCP 服务', '管理当前租户的 MCP 服务与工具。'],
  logs: ['调用日志', '查看当前租户的 MCP 调用记录。'],
  account: ['账户设置', '管理当前租户账户的安全设置。'],
}

function readLocation() {
  return parseTenantLocation(window.location.search)
}

function TenantOverview({ apiClient }) {
  const [state, setState] = useState({ loading: true, error: '', data: null })

  useEffect(() => {
    const controller = new AbortController()
    setState({ loading: true, error: '', data: null })
    apiClient.get('/overview', { signal: controller.signal })
      .then((response) => {
        if (!controller.signal.aborted) setState({ loading: false, error: '', data: response.data || {} })
      })
      .catch(() => {
        if (!controller.signal.aborted) setState({ loading: false, error: '概览暂时无法加载', data: null })
      })
    return () => controller.abort()
  }, [apiClient])

  if (state.loading) return <Card><Spin tip="正在加载租户概览" /></Card>
  if (state.error) return <Alert type="error" showIcon message={state.error} />

  const data = state.data || {}
  return (
    <Card className="tenant-view-card">
      <Title level={2}>租户概览</Title>
      <Paragraph type="secondary">所有数据均由当前登录会话确定租户范围。</Paragraph>
      <Row gutter={[16, 16]}>
        <Col xs={12} lg={6}><Statistic title="连接" value={data.connections?.total || 0} suffix={`/ ${data.connections?.active || 0} 运行中`} /></Col>
        <Col xs={12} lg={6}><Statistic title="服务" value={data.services?.total || 0} suffix={`/ ${data.services?.active || 0} 运行中`} /></Col>
        <Col xs={12} lg={6}><Statistic title="工具" value={data.tools?.total || 0} suffix={`/ ${data.tools?.active || 0} 可用`} /></Col>
        <Col xs={12} lg={6}><Statistic title="调用日志" value={data.logs?.total || 0} /></Col>
      </Row>
    </Card>
  )
}

export default function TenantApp() {
  const [sessionState, setSessionState] = useState('checking')
  const [locationState, setLocationState] = useState(readLocation)
  const [logoutBusy, setLogoutBusy] = useState(false)
  const [logoutError, setLogoutError] = useState('')
  const [logFilters, setLogFilters] = useState(() => parseScopedLogLocation('tenant', ''))
  const mounted = useRef(true)
  const logoutSequence = useRef(createTenantLogoutSequence())
  const logoutController = useRef(null)

  useEffect(() => {
    let active = true
    tenantApi.get('/session')
      .then(response => {
        if (active) setSessionState(response.data?.authed === true ? 'authed' : 'logged-out')
      })
      .catch(() => {
        if (active) setSessionState('logged-out')
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      logoutSequence.current.invalidate()
      logoutController.current?.abort()
    }
  }, [])

  useEffect(() => {
    const restoreLocation = () => {
      const nextLocation = readLocation()
      const nextUrl = tenantUrl(nextLocation.view, window.location.pathname)
      const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`
      if (nextUrl !== currentUrl) window.history.replaceState({}, '', nextUrl)
      setLocationState(nextLocation)
    }
    const expireSession = () => {
      logoutSequence.current.invalidate()
      logoutController.current?.abort()
      setLogoutBusy(false)
      setLogoutError('')
      setSessionState('logged-out')
    }
    window.addEventListener('popstate', restoreLocation)
    window.addEventListener('tenant-session-expired', expireSession)
    return () => {
      window.removeEventListener('popstate', restoreLocation)
      window.removeEventListener('tenant-session-expired', expireSession)
    }
  }, [])

  useEffect(() => {
    const canonicalSearch = serializeTenantLocation(locationState.view)
    if (window.location.search !== canonicalSearch || window.location.hash) {
      window.history.replaceState({}, '', tenantUrl(locationState.view, window.location.pathname))
    }
  }, [locationState.view])

  const navigate = useCallback(view => {
    const nextUrl = tenantUrl(view, window.location.pathname)
    const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`
    if (nextUrl !== currentUrl) window.history.pushState({}, '', nextUrl)
    setLocationState(parseTenantLocation(new URL(nextUrl, window.location.origin).search))
  }, [])

  const logout = async () => {
    const requestId = logoutSequence.current.begin()
    logoutController.current?.abort()
    const controller = new AbortController()
    logoutController.current = controller
    const isCurrent = () => (
      mounted.current
      && !controller.signal.aborted
      && logoutSequence.current.isCurrent(requestId)
    )
    setLogoutBusy(true)
    setLogoutError('')
    await executeTenantLogout({
      request: () => logoutTenant({ signal: controller.signal }),
      isCurrent,
      onLoggedOut: () => {
        setLogoutError('')
        setSessionState('logged-out')
      },
      onError: setLogoutError,
    })
    if (isCurrent()) setLogoutBusy(false)
  }

  const login = () => {
    logoutSequence.current.invalidate()
    logoutController.current?.abort()
    setLogoutBusy(false)
    setLogoutError('')
    setSessionState('authed')
  }

  if (sessionState === 'checking') {
    return <main className="tenant-session-check"><Spin size="large" tip="正在验证会话" /></main>
  }

  if (sessionState !== 'authed') {
    return <TenantLogin onLogin={login} />
  }

  const [title, description] = VIEW_COPY[locationState.view]
  let viewContent
  if (locationState.view === 'overview') {
    viewContent = <TenantOverview apiClient={tenantApi} />
  } else if (locationState.view === 'connections') {
    viewContent = (
      <Connections
        scope="tenant"
        apiClient={tenantApi}
        embedded
        onViewLogs={(connection) => {
          setLogFilters((current) => ({ ...current, connectionId: connection.connection_id }))
          navigate('logs')
        }}
      />
    )
  } else if (locationState.view === 'services') {
    viewContent = <Services scope="tenant" apiClient={tenantApi} />
  } else if (locationState.view === 'logs') {
    viewContent = (
      <McpLogs
        scope="tenant"
        apiClient={tenantApi}
        filters={logFilters}
        onFiltersChange={setLogFilters}
      />
    )
  } else {
    viewContent = (
      <Card className="tenant-view-card">
        <Title level={2}>{title}</Title>
        <Paragraph type="secondary">{description}</Paragraph>
      </Card>
    )
  }
  return (
    <Layout className="tenant-shell">
      <Header className="tenant-header">
        <span className="tenant-brand">企微数据中转 <span>· 租户控制台</span></span>
        <nav className="tenant-nav" aria-label="租户控制台主导航">
          {NAV_ITEMS.map(([view, label]) => (
            <Button
              key={view}
              type="text"
              className={locationState.view === view ? 'tenant-nav__item tenant-nav__item--active' : 'tenant-nav__item'}
              aria-current={locationState.view === view ? 'page' : undefined}
              onClick={() => navigate(view)}
            >
              {label}
            </Button>
          ))}
        </nav>
        <Button type="text" className="tenant-logout" loading={logoutBusy} onClick={logout}>退出登录</Button>
      </Header>
      <Content className="tenant-content">
        {logoutError ? (
          <Alert
            className="tenant-logout-error"
            type="error"
            message={logoutError}
            action={<Button size="small" danger onClick={logout}>重试</Button>}
            showIcon
          />
        ) : null}
        {viewContent}
      </Content>
    </Layout>
  )
}
