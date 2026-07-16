import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Button, Drawer, Empty, Form, Grid, Input, Modal, Select, Space, Switch, Table, Tabs, Tag, Typography, message,
} from 'antd'
import {
  ApiOutlined, CopyOutlined, FileSearchOutlined, PlusOutlined, ReloadOutlined,
  SafetyCertificateOutlined, SyncOutlined,
} from '@ant-design/icons'
import api from '../api.js'
import DeclarativeSpecWizard from './DeclarativeSpecWizard.jsx'
import {
  buildConnectionMcpConfig, canEnableWriteTool, closeTokenModal, safeServerError,
  createRequestSequence, isActiveDeclarativeConfigReadOnly, selectActiveTokenHint,
  setExplicitToolPolicy,
} from './connectionView.js'
import './Connections.css'

const { Paragraph, Text, Title } = Typography
const STATUS_META = {
  active: { label: '运行中', color: 'success' },
  draft: { label: '草稿', color: 'gold' },
  disabled: { label: '已停用', color: 'default' },
  error: { label: '异常', color: 'error' },
}
const DATA_MODE = { direct: '直连', stored: '存储', hybrid: '混合' }

function parseObject(text, label) {
  try {
    const value = JSON.parse(text || '{}')
    if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error()
    return value
  } catch { throw new Error(`${label}必须是 JSON 对象`) }
}

function endpoint(connectionId) {
  return `${window.location.origin}/mcp/${encodeURIComponent(connectionId)}`
}

export default function Connections({ tenantId = '', initialConnectionId = '', embedded = false, onViewLogs = () => {} }) {
  const [tenants, setTenants] = useState([])
  const [scopeTenant, setScopeTenant] = useState(tenantId)
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [connectorFilter, setConnectorFilter] = useState('all')
  const [detail, setDetail] = useState(null)
  const [tools, setTools] = useState([])
  const [tokens, setTokens] = useState([])
  const [drawerBusy, setDrawerBusy] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [createBusy, setCreateBusy] = useState(false)
  const [credentialsText, setCredentialsText] = useState('{}')
  const [configText, setConfigText] = useState('{}')
  const [tokenLabel, setTokenLabel] = useState('')
  const [activeTab, setActiveTab] = useState('config')
  const [tokenModal, setTokenModal] = useState(closeTokenModal)
  const [form] = Form.useForm()
  const [messageApi, contextHolder] = message.useMessage()
  const [modal, modalContextHolder] = Modal.useModal()
  const screens = Grid.useBreakpoint()
  const compact = !screens.md
  const listSequence = useRef(createRequestSequence())
  const detailSequence = useRef(createRequestSequence())
  const listController = useRef(null)
  const detailController = useRef(null)
  const openedInitialConnection = useRef('')
  const openDetailId = useRef('')
  const tokenSecret = useRef('')

  const load = useCallback(async () => {
    const requestId = listSequence.current.begin()
    listController.current?.abort()
    const controller = new AbortController()
    listController.current = controller
    setLoading(true)
    setLoadError('')
    try {
      let tenantRows = tenants
      if (!tenantId || tenantRows.length === 0) {
        const tenantResponse = await api.get('/admin/tenants', { signal: controller.signal })
        tenantRows = tenantResponse.data?.items || []
        setTenants(tenantRows)
      }
      const tenantIds = scopeTenant ? [scopeTenant] : tenantRows.map((tenant) => tenant.tenant_id)
      const responses = await Promise.all(tenantIds.map((id) => api.get(`/admin/tenants/${encodeURIComponent(id)}/connections`, { signal: controller.signal })))
      const listed = responses.flatMap((response) => response.data?.items || [])
      const enriched = await Promise.all(listed.map(async (connection) => {
        const id = encodeURIComponent(connection.connection_id)
        const [detailResponse, toolResponse] = await Promise.all([
          api.get(`/admin/connections/${id}`, { signal: controller.signal }).catch(() => null),
          api.get(`/admin/connections/${id}/tools`, { signal: controller.signal }).catch(() => null),
        ])
        return {
          ...connection,
          token_prefix: selectActiveTokenHint(detailResponse?.data?.tokens || []),
          tool_count: toolResponse?.data?.items?.length ?? 0,
        }
      }))
      if (listSequence.current.isCurrent(requestId) && !controller.signal.aborted) setItems(enriched)
    } catch (error) {
      if (!controller.signal.aborted && listSequence.current.isCurrent(requestId)) {
        setLoadError(safeServerError(error, '连接实例加载失败'))
      }
    } finally {
      if (listSequence.current.isCurrent(requestId)) setLoading(false)
    }
  }, [scopeTenant, tenantId, tenants.length])

  useEffect(() => {
    listSequence.current.invalidate()
    listController.current?.abort()
    setScopeTenant(tenantId)
    setItems([])
    openedInitialConnection.current = ''
    detailSequence.current.invalidate()
    detailController.current?.abort()
    openDetailId.current = ''
    setDetail(null)
    setTools([])
    setTokens([])
  }, [tenantId])
  useEffect(() => {
    load()
    return () => {
      listSequence.current.invalidate()
      listController.current?.abort()
    }
  }, [scopeTenant])
  useEffect(() => () => {
    listSequence.current.invalidate()
    detailSequence.current.invalidate()
    listController.current?.abort()
    detailController.current?.abort()
    tokenSecret.current = ''
  }, [])

  const openDetail = useCallback(async (row) => {
    openDetailId.current = row.connection_id
    const requestId = detailSequence.current.begin()
    detailController.current?.abort()
    const controller = new AbortController()
    detailController.current = controller
    setDetail(row)
    setActiveTab('config')
    setTools([])
    setTokens([])
    setConfigText(JSON.stringify(row.public_config || {}, null, 2))
    setCredentialsText('{}')
    setDrawerBusy('detail')
    try {
      const [detailResponse, toolResponse] = await Promise.all([
        api.get(`/admin/connections/${encodeURIComponent(row.connection_id)}`, { signal: controller.signal }),
        api.get(`/admin/connections/${encodeURIComponent(row.connection_id)}/tools`, { signal: controller.signal }).catch(() => ({ data: { items: [] } })),
      ])
      if (!detailSequence.current.isCurrent(requestId) || controller.signal.aborted) return
      const next = detailResponse.data?.connection || row
      setDetail(next)
      setTokens(detailResponse.data?.tokens || [])
      setTools(toolResponse.data?.items || [])
      setConfigText(JSON.stringify(next.public_config || {}, null, 2))
    } catch (error) {
      if (!controller.signal.aborted && detailSequence.current.isCurrent(requestId)) messageApi.error(safeServerError(error, '连接详情加载失败'))
    } finally {
      if (detailSequence.current.isCurrent(requestId)) setDrawerBusy('')
    }
  }, [messageApi])

  useEffect(() => {
    detailSequence.current.invalidate()
    detailController.current?.abort()
    openDetailId.current = ''
    setDetail(null)
    setTools([])
    setTokens([])
    openedInitialConnection.current = ''
  }, [initialConnectionId])
  useEffect(() => {
    const targetKey = `${tenantId}:${initialConnectionId}`
    if (initialConnectionId && items.length && openedInitialConnection.current !== targetKey) {
      const target = items.find((item) => (
        item.connection_id === initialConnectionId && (!tenantId || item.tenant_id === tenantId)
      ))
      if (target) {
        openedInitialConnection.current = targetKey
        openDetail(target)
      }
    }
  }, [tenantId, initialConnectionId, items, openDetail])

  const visible = useMemo(() => items.filter((item) => {
    const term = query.trim().toLowerCase()
    return (!term || [item.display_name, item.connection_id, item.tenant_id].some((value) => String(value || '').toLowerCase().includes(term)))
      && (statusFilter === 'all' || item.status === statusFilter)
      && (connectorFilter === 'all' || item.connector_key === connectorFilter)
  }), [items, query, statusFilter, connectorFilter])

  const closeDetail = () => {
    detailSequence.current.invalidate()
    detailController.current?.abort()
    openDetailId.current = ''
    setDetail(null)
    setTools([])
    setTokens([])
    setCredentialsText('{}')
    setConfigText('{}')
    setTokenLabel('')
    setActiveTab('config')
  }

  const showToken = (connection, rawToken) => {
    tokenSecret.current = rawToken
    setTokenModal({ open: true, rawToken, connectionId: connection.connection_id })
  }
  const dismissToken = () => {
    tokenSecret.current = ''
    setTokenModal(closeTokenModal())
  }

  const create = async () => {
    setCreateBusy(true)
    try {
      const values = await form.validateFields()
      const connectorKey = String(values.connector_key || '').trim()
      const declarative = connectorKey === 'http_declarative'
      const response = await api.post(`/admin/tenants/${encodeURIComponent(values.tenant_id)}/connections`, {
        connector_key: connectorKey,
        display_name: values.display_name.trim(),
        data_mode: values.data_mode,
        status: declarative ? 'draft' : 'active',
        public_config: declarative ? {} : parseObject(values.public_config, '公开配置'),
        credentials: declarative ? {} : parseObject(values.credentials, '凭据'),
      })
      const connection = response.data.connection
      form.resetFields()
      setCreateOpen(false)
      showToken(connection, response.data.initial_token)
      messageApi.success('连接已创建；Token 只显示这一次')
      await load()
      if (declarative) openDetail(connection)
    } catch (error) {
      if (!error?.errorFields) messageApi.error(error.message || safeServerError(error, '创建连接失败'))
    } finally { setCreateBusy(false) }
  }

  const saveConfig = async () => {
    if (!detail) return
    if (isActiveDeclarativeConfigReadOnly(detail)) {
      messageApi.warning('活动声明式连接不可通过通用配置修改；请先停用连接，再使用声明式向导发布待激活修订。')
      return
    }
    setDrawerBusy('config')
    const connectionId = detail.connection_id
    try {
      const response = await api.put(`/admin/connections/${encodeURIComponent(detail.connection_id)}`, {
        display_name: detail.display_name,
        data_mode: detail.data_mode,
        public_config: parseObject(configText, '公开配置'),
        status: detail.connector_key === 'http_declarative' ? null : detail.status,
      })
      if (openDetailId.current === connectionId) setDetail(response.data.connection)
      messageApi.success('连接配置已保存')
      load()
    } catch (error) { messageApi.error(error.message || safeServerError(error, '保存配置失败')) }
    finally { setDrawerBusy('') }
  }

  const saveCredentials = async () => {
    setDrawerBusy('credentials')
    try {
      await api.put(`/admin/connections/${encodeURIComponent(detail.connection_id)}/credentials`, {
        credentials: parseObject(credentialsText, '新凭据'),
      })
      setCredentialsText('{}')
      messageApi.success('凭据已替换，输入内容已清除')
    } catch (error) { messageApi.error(error.message || safeServerError(error, '替换凭据失败')) }
    finally { setDrawerBusy('') }
  }

  const issueToken = async (rotate = false) => {
    const connectionId = detail.connection_id
    setDrawerBusy('token')
    try {
      const response = await api.post(
        `/admin/connections/${encodeURIComponent(detail.connection_id)}/tokens${rotate ? '/rotate' : ''}`,
        { label: tokenLabel.trim() },
      )
      setTokenLabel('')
      showToken(detail, response.data.token)
      if (openDetailId.current === connectionId) await openDetail(detail)
    } catch (error) { messageApi.error(safeServerError(error, 'Token 操作失败')) }
    finally { setDrawerBusy('') }
  }

  const revokeToken = (token) => modal.confirm({
    title: '撤销这个 Token？',
    content: `仅凭提示 ${token.prefix || token.token_prefix || '—'} 识别；撤销后使用它的客户端会立即失效。`,
    okText: '撤销 Token',
    okButtonProps: { danger: true },
    cancelText: '取消',
    onOk: async () => {
      const connectionId = detail.connection_id
      try {
        await api.delete(`/admin/connections/${encodeURIComponent(detail.connection_id)}/tokens/${encodeURIComponent(token.token_id)}`)
        messageApi.success('Token 已撤销')
        if (openDetailId.current === connectionId) await openDetail(detail)
      } catch (error) {
        messageApi.error(safeServerError(error, '撤销 Token 失败'))
        throw error
      }
    },
  })

  const saveTools = async () => {
    const invalidWrite = tools.some((tool) => tool.enabled && !canEnableWriteTool(tool, {
      explicitEnable: Boolean(tool.enabled), explicitWrite: tool.policy?.allow_write,
    }))
    if (invalidWrite) { messageApi.warning('启用写操作时必须明确同意写入'); return }
    setDrawerBusy('tools')
    try {
      await api.put(`/admin/connections/${encodeURIComponent(detail.connection_id)}/tools`, {
        policies: tools.map((tool) => ({
          tool_key: tool.tool_key,
          enabled: Boolean(tool.enabled),
          allow_write: tool.operation_kind === 'write' && Boolean(tool.policy?.allow_write),
        })),
      })
      messageApi.success('工具策略已保存')
    } catch (error) { messageApi.error(safeServerError(error, '工具策略保存失败')) }
    finally { setDrawerBusy('') }
  }

  const action = async (name, path, success) => {
    const connectionId = detail.connection_id
    setDrawerBusy(name)
    try {
      const response = await api.post(`/admin/connections/${encodeURIComponent(detail.connection_id)}/${path}`)
      messageApi.success(success)
      if (response.data?.connection && openDetailId.current === connectionId) setDetail(response.data.connection)
      load()
    } catch (error) { messageApi.error(safeServerError(error)) }
    finally { setDrawerBusy('') }
  }

  const columns = [
    {
      title: '连接 / 状态', key: 'identity', width: compact ? 150 : 230, rowScope: 'row',
      render: (_, row) => <div className="connection-identity"><Text strong>{row.display_name}</Text><Text code>{row.connection_id}</Text><Tag color={(STATUS_META[row.status] || STATUS_META.disabled).color}>{(STATUS_META[row.status] || STATUS_META.disabled).label}</Tag></div>,
    },
    { title: '连接器', dataIndex: 'connector_key', key: 'connector_key', width: 160, render: (value) => <Tag color="cyan">{value}</Tag> },
    { title: '数据模式', dataIndex: 'data_mode', key: 'data_mode', width: 100, responsive: ['md'], render: (value) => DATA_MODE[value] || value },
    { title: 'MCP 端点', key: 'endpoint', width: 280, responsive: ['lg'], render: (_, row) => <Text code copyable={{ text: endpoint(row.connection_id) }}>{endpoint(row.connection_id)}</Text> },
    { title: 'Token', key: 'token', width: 120, responsive: ['xl'], render: (_, row) => <Text>{row.token_prefix || row.token_hint || '详情中查看提示'}</Text> },
    { title: '工具', key: 'tools', width: 82, responsive: ['md'], render: (_, row) => row.tool_count ?? '详情' },
    { title: '同步健康', key: 'sync', width: 110, responsive: ['lg'], render: (_, row) => <Tag color={row.data_mode === 'direct' ? 'blue' : row.status === 'active' ? 'green' : 'default'}>{row.data_mode === 'direct' ? '实时' : row.status === 'active' ? '可同步' : '已暂停'}</Tag> },
    {
      title: '操作', key: 'actions', width: compact ? 112 : 170, fixed: 'right',
      render: (_, row) => <Space size={4}><Button type="link" onClick={() => openDetail(row)}>操作</Button><Button type="link" icon={<FileSearchOutlined />} aria-label={`查看 ${row.display_name} 日志`} onClick={() => onViewLogs(row)}>日志</Button></Space>,
    },
  ]

  const configReadOnly = isActiveDeclarativeConfigReadOnly(detail || {})
  const tabs = detail ? [
    {
      key: 'config', label: '配置', children: <div className="connection-editor"><Alert type={configReadOnly ? 'warning' : 'info'} showIcon message={configReadOnly ? '活动声明式连接的配置只读' : '公开配置不会包含凭据或 Token'} description={configReadOnly ? '先停用连接，再使用声明式向导创建并发布待激活修订。通用配置保存不会被调用。' : undefined} /><label>连接名称<Input aria-label="连接名称" disabled={configReadOnly} value={detail.display_name} onChange={(event) => setDetail((current) => ({ ...current, display_name: event.target.value }))} /></label><label>数据模式<Select aria-label="连接数据模式" disabled={configReadOnly} value={detail.data_mode} options={Object.entries(DATA_MODE).map(([value, label]) => ({ value, label }))} onChange={(data_mode) => setDetail((current) => ({ ...current, data_mode }))} /></label><label>公开配置（JSON）<Input.TextArea aria-label="连接公开配置 JSON" disabled={configReadOnly} rows={9} value={configText} onChange={(event) => setConfigText(event.target.value)} /></label><Button type="primary" disabled={configReadOnly} loading={drawerBusy === 'config'} onClick={saveConfig}>保存配置</Button></div>,
    },
    {
      key: 'credentials', label: '凭据', children: <div className="connection-editor"><Alert type="warning" showIcon message="只输入一组完整的新凭据" description="现有值永不返回浏览器。保存或关闭工作台后，输入内容会立即清除。" /><Input.TextArea aria-label="新连接凭据 JSON" rows={8} value={credentialsText} onChange={(event) => setCredentialsText(event.target.value)} placeholder='{"api_key":"new value"}' /><Button type="primary" loading={drawerBusy === 'credentials'} onClick={saveCredentials}>替换凭据并清除输入</Button></div>,
    },
    {
      key: 'tokens', label: `Token · ${tokens.length}`, children: <div className="connection-editor"><Alert type="info" showIcon message="Token 原文只在签发后显示一次" /><Input aria-label="Token 用途标签" maxLength={128} value={tokenLabel} placeholder="用途标签（可选）" onChange={(event) => setTokenLabel(event.target.value)} /><Space><Button loading={drawerBusy === 'token'} onClick={() => issueToken(false)}>签发新 Token</Button><Button danger loading={drawerBusy === 'token'} onClick={() => issueToken(true)}>轮换并撤销旧 Token</Button></Space><div className="connection-token-list">{tokens.map((token) => { const revoked = Boolean(token.revoked_at || token.revoked === true || token.status === 'revoked'); return <div key={token.token_id}><Text code>{token.prefix || token.token_prefix || 'token'}</Text><Text type="secondary">{token.label || token.token_label || '未命名'}</Text>{revoked ? <Tag>已撤销{token.revoked_at ? ` · ${new Date(token.revoked_at).toLocaleString('zh-CN')}` : ''}</Tag> : <Button danger type="link" size="small" onClick={() => revokeToken(token)}>撤销</Button>}</div> })}</div></div>,
    },
    {
      key: 'tools', label: `工具 · ${tools.length}`, children: <div className="connection-editor"><Alert type="warning" showIcon message="写操作需要双重确认" /><div className="connection-tools">{tools.map((tool, index) => <div key={tool.tool_key}><div><Text code>{tool.mcp_name || tool.tool_key}</Text><Tag color={tool.operation_kind === 'write' ? 'orange' : 'blue'}>{tool.operation_kind}</Tag></div><Space wrap><Switch aria-label={`${tool.tool_key} 启用状态`} checked={Boolean(tool.enabled)} checkedChildren="启用" unCheckedChildren="停用" onChange={(enabled) => setTools((current) => current.map((item, i) => i === index ? { ...item, ...setExplicitToolPolicy(item, item, enabled), policy: { ...(item.policy || {}), allow_write: false } } : item))} />{tool.operation_kind === 'write' && <Switch aria-label={`${tool.tool_key} 写入同意`} checked={Boolean(tool.policy?.allow_write)} disabled={!tool.enabled} checkedChildren="同意写入" unCheckedChildren="未授权写入" onChange={(allow_write) => setTools((current) => current.map((item, i) => i === index ? { ...item, policy: { ...(item.policy || {}), allow_write } } : item))} />}</Space></div>)}</div><Button type="primary" loading={drawerBusy === 'tools'} onClick={saveTools}>保存工具策略</Button></div>,
    },
    {
      key: 'operate', label: '测试与同步', children: <div className="connection-editor"><div className="connection-operation-grid"><Button icon={<SafetyCertificateOutlined />} loading={drawerBusy === 'test'} onClick={() => action('test', 'test', '安全连接测试通过')}>安全测试</Button><Button icon={<SyncOutlined />} disabled={detail.data_mode === 'direct' || detail.status !== 'active'} loading={drawerBusy === 'sync'} onClick={() => action('sync', 'sync', '同步已触发')}>立即同步</Button><Button danger disabled={detail.status === 'disabled'} loading={drawerBusy === 'disable'} onClick={() => action('disable', 'disable', '连接已停用')}>停用连接</Button><Button icon={<FileSearchOutlined />} onClick={() => onViewLogs(detail)}>查看该连接日志</Button></div></div>,
    },
    ...(detail.connector_key === 'http_declarative' ? [{ key: 'wizard', label: '声明式向导', children: <DeclarativeSpecWizard active={activeTab === 'wizard'} connection={detail} onChanged={(next) => { if (next && openDetailId.current === next.connection_id) setDetail(next); load() }} /> }] : []),
  ] : []

  return (
    <main className={`connection-workbench ${embedded ? 'connection-workbench--embedded' : ''}`}>
      {contextHolder}{modalContextHolder}
      {!embedded && <header className="connection-heading"><div><Text className="connection-eyebrow">CONNECTION CONTROL / MCP</Text><Title level={1}>连接实例</Title><Paragraph>安全创建、分阶段验证并运行每个 MCP 连接。</Paragraph></div><Button type="primary" size="large" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建连接</Button></header>}

      <section className="connection-control-rail" aria-label="连接控制流程">
        <div><span>Provider</span><strong>连接器</strong><small>能力与凭据边界</small></div><i aria-hidden="true" />
        <div><span>Data mode · policy</span><strong>数据模式与工具策略</strong><small>写操作双重同意</small></div><i aria-hidden="true" />
        <div><span>MCP endpoint · revision</span><strong>实例端点与修订</strong><small>测试、发布、激活</small></div>
      </section>

      <section className="connection-panel">
        <div className="connection-toolbar">
          {!tenantId && <Select aria-label="按租户筛选连接实例" allowClear showSearch optionFilterProp="label" placeholder="全部租户" value={scopeTenant || undefined} options={tenants.map((tenant) => ({ value: tenant.tenant_id, label: tenant.display_name ? `${tenant.display_name} · ${tenant.tenant_id}` : tenant.tenant_id }))} onChange={(value) => setScopeTenant(value || '')} />}
          <Input aria-label="搜索连接实例" allowClear value={query} placeholder="搜索名称、连接 ID 或租户" onChange={(event) => setQuery(event.target.value)} />
          <Select aria-label="按连接器筛选连接实例" value={connectorFilter} options={[{ value: 'all', label: '全部连接器' }, ...[...new Set(items.map((item) => item.connector_key))].map((value) => ({ value, label: value }))]} onChange={setConnectorFilter} />
          <Select aria-label="按状态筛选连接实例" value={statusFilter} options={[{ value: 'all', label: '全部状态' }, ...Object.entries(STATUS_META).map(([value, meta]) => ({ value, label: meta.label }))]} onChange={setStatusFilter} />
          <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
          {embedded && <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建连接</Button>}
        </div>
        {loadError && <Alert showIcon type="error" message="连接实例加载失败" description={loadError} action={<Button size="small" onClick={load}>重试</Button>} />}
        <Table rowKey="connection_id" columns={columns} dataSource={visible} loading={loading} size="middle" scroll={{ x: compact ? 620 : 1260 }} pagination={{ pageSize: 20, responsive: true, showSizeChanger: true }} locale={{ emptyText: <Empty description="当前范围内暂无连接实例"><Button type="primary" onClick={() => setCreateOpen(true)}>创建第一个连接</Button></Empty> }} />
      </section>

      <Drawer rootClassName="connection-drawer" title={detail ? `${detail.display_name} · 连接工作台` : '连接工作台'} open={Boolean(detail)} onClose={closeDetail} width={screens.lg ? 960 : screens.sm ? '92vw' : '100vw'} loading={drawerBusy === 'detail'} extra={detail && <Tag color={(STATUS_META[detail.status] || STATUS_META.disabled).color}>{(STATUS_META[detail.status] || STATUS_META.disabled).label}</Tag>}>
        {detail && <><div className="connection-drawer-rail"><span><ApiOutlined /> {detail.connector_key}</span><span>{DATA_MODE[detail.data_mode] || detail.data_mode}</span><Text code>{endpoint(detail.connection_id)}</Text></div><Tabs activeKey={activeTab} items={tabs} onChange={(key) => { setActiveTab(key); if (key !== 'credentials') setCredentialsText('{}') }} /></>}
      </Drawer>

      <Modal title="新建连接实例" open={createOpen} onCancel={() => { setCreateOpen(false); form.resetFields() }} onOk={create} confirmLoading={createBusy} okText="创建连接" cancelText="取消" width={620}>
        <Form form={form} layout="vertical" initialValues={{ tenant_id: tenantId || scopeTenant || undefined, connector_key: 'wecom', data_mode: 'direct', public_config: '{}', credentials: '{}' }}>
          <Form.Item name="tenant_id" label="租户" rules={[{ required: true, message: '请选择租户' }]}><Select disabled={Boolean(tenantId)} showSearch optionFilterProp="label" options={tenants.map((tenant) => ({ value: tenant.tenant_id, label: tenant.display_name ? `${tenant.display_name} · ${tenant.tenant_id}` : tenant.tenant_id }))} /></Form.Item>
          <Form.Item name="display_name" label="连接名称" rules={[{ required: true, whitespace: true, max: 128 }]}><Input placeholder="例如 华东企微生产连接" /></Form.Item>
          <Form.Item name="connector_key" label="连接器 Key" rules={[{ required: true }, { pattern: /^[a-z][a-z0-9_-]{0,63}$/, message: '使用小写字母开头，仅含小写字母、数字、_ 或 -' }]}><Input aria-label="连接器 Key" list="known-connector-keys" placeholder="输入已注册的连接器 Key" /></Form.Item>
          <datalist id="known-connector-keys"><option value="wecom">企业微信</option><option value="http_declarative">HTTP 声明式</option></datalist>
          <Form.Item name="data_mode" label="数据模式" rules={[{ required: true }]}><Select aria-label="新连接数据模式" options={Object.entries(DATA_MODE).map(([value, label]) => ({ value, label }))} /></Form.Item>
          <Form.Item noStyle shouldUpdate={(previous, current) => previous.connector_key !== current.connector_key}>{({ getFieldValue }) => getFieldValue('connector_key') === 'http_declarative' ? <Alert type="info" showIcon message="连接将以草稿创建" description="创建后进入声明式向导导入 JSON/YAML；这里不会接受代码、模板或预置凭据。" /> : <><Form.Item name="public_config" label="公开配置（JSON 对象）"><Input.TextArea aria-label="新连接公开配置 JSON" rows={5} /></Form.Item><Form.Item name="credentials" label="初始凭据（JSON 对象）"><Input.TextArea aria-label="新连接初始凭据 JSON" rows={5} /></Form.Item></>}</Form.Item>
        </Form>
      </Modal>

      <Modal title="Token 仅显示一次" open={tokenModal.open} onCancel={dismissToken} footer={<Button type="primary" onClick={dismissToken}>我已安全保存并关闭</Button>} closable maskClosable={false}>
        <Alert type="warning" showIcon message="关闭后无法再次查看此 Token" />
        <div className="connection-token-once"><Text code copyable={{ text: tokenModal.rawToken, tooltips: ['复制 Token', '已复制'] }}>{tokenModal.rawToken}</Text><Button icon={<CopyOutlined />} onClick={() => navigator.clipboard?.writeText(buildConnectionMcpConfig({ connection_id: tokenModal.connectionId, initial_token: tokenModal.rawToken }, window.location.origin))}>复制 MCP 配置</Button><Input.TextArea aria-label="一次性 MCP JSON 配置" readOnly rows={9} value={tokenModal.open ? buildConnectionMcpConfig({ connection_id: tokenModal.connectionId, initial_token: tokenModal.rawToken }, window.location.origin) : ''} /></div>
      </Modal>
    </main>
  )
}
