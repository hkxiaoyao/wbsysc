import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Button, Drawer, Empty, Form, Input, Modal, Select, Space, Table, Tag, Typography, message,
} from 'antd'
import { ApiOutlined, KeyOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'

import defaultApi from '../api.js'
import { connectionCollectionEndpoint, connectionResourceEndpoint, createRequestSequence } from './connectionView.js'
import ServiceTokenModal from './ServiceTokenModal.jsx'
import {
  aliasConflicts,
  apiClientEndpoint,
  bindingAliasPreview,
  bindingPayload,
  safeServiceError,
  serviceCollectionEndpoint,
  serviceResourceEndpoint,
} from './servicesView.js'

const { Paragraph, Text, Title } = Typography
const SERVICE_STATUS = {
  draft: { label: '草稿', color: 'gold' },
  active: { label: '已发布', color: 'success' },
  disabled: { label: '已停用', color: 'default' },
}

function parsePolicy(text) {
  try {
    const policy = JSON.parse(text || '{}')
    if (!policy || Array.isArray(policy) || typeof policy !== 'object') throw new Error()
    return policy
  } catch {
    throw new Error('策略必须是 JSON 对象')
  }
}

export default function Services({
  scope = 'admin',
  tenantId = '',
  apiClient = defaultApi,
  onTenantChange,
}) {
  const tenantScope = scope === 'tenant'
  const [scopeTenant, setScopeTenant] = useState(tenantId)
  const [tenants, setTenants] = useState([])
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [detail, setDetail] = useState(null)
  const [bindings, setBindings] = useState([])
  const [tokens, setTokens] = useState([])
  const [connections, setConnections] = useState([])
  const [availableTools, setAvailableTools] = useState([])
  const [detailLoading, setDetailLoading] = useState(false)
  const [mutationBusy, setMutationBusy] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [createBusy, setCreateBusy] = useState(false)
  const [tokenOpen, setTokenOpen] = useState(false)
  const [draftBinding, setDraftBinding] = useState({
    connection_id: '', source_tool_key: '', tool_alias: '', binding_status: 'active', policyText: '{}',
  })
  const [form] = Form.useForm()
  const [messageApi, contextHolder] = message.useMessage()
  const [modal, modalContextHolder] = Modal.useModal()
  const mounted = useRef(true)
  const openServiceId = useRef('')
  const listSequence = useRef(createRequestSequence())
  const detailSequence = useRef(createRequestSequence())
  const listController = useRef(null)
  const detailController = useRef(null)
  const mutationController = useRef(null)
  const toolController = useRef(null)
  const selectedTenant = tenantScope ? '' : (tenantId || scopeTenant)
  const hasScope = tenantScope || Boolean(selectedTenant)

  const serviceEndpoint = useCallback((serviceId, suffix = '') => apiClientEndpoint(
    apiClient,
    serviceResourceEndpoint(scope, selectedTenant, serviceId, suffix),
  ), [apiClient, scope, selectedTenant])

  const collectionEndpoint = useCallback(() => apiClientEndpoint(
    apiClient,
    serviceCollectionEndpoint(scope, selectedTenant),
  ), [apiClient, scope, selectedTenant])

  const resetWorkbench = useCallback(() => {
    detailSequence.current.invalidate()
    detailController.current?.abort()
    mutationController.current?.abort()
    toolController.current?.abort()
    openServiceId.current = ''
    setDetail(null)
    setBindings([])
    setTokens([])
    setConnections([])
    setAvailableTools([])
    setTokenOpen(false)
    setMutationBusy('')
    setDraftBinding({ connection_id: '', source_tool_key: '', tool_alias: '', binding_status: 'active', policyText: '{}' })
  }, [])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      listSequence.current.invalidate()
      detailSequence.current.invalidate()
      listController.current?.abort()
      detailController.current?.abort()
      mutationController.current?.abort()
      toolController.current?.abort()
    }
  }, [])

  useEffect(() => {
    setScopeTenant(tenantId)
    setItems([])
    resetWorkbench()
  }, [tenantId, resetWorkbench])

  useEffect(() => {
    if (tenantScope) return undefined
    const controller = new AbortController()
    apiClient.get('/admin/tenants', { signal: controller.signal })
      .then(response => { if (!controller.signal.aborted) setTenants(response.data?.items || []) })
      .catch(() => { if (!controller.signal.aborted) setTenants([]) })
    return () => controller.abort()
  }, [apiClient, tenantScope])

  const load = useCallback(async () => {
    listSequence.current.invalidate()
    listController.current?.abort()
    if (!hasScope) {
      setItems([])
      setLoading(false)
      setLoadError('')
      return
    }
    const requestId = listSequence.current.begin()
    const controller = new AbortController()
    listController.current = controller
    setLoading(true)
    setLoadError('')
    try {
      const response = await apiClient.get(collectionEndpoint(), { signal: controller.signal })
      if (mounted.current && !controller.signal.aborted && listSequence.current.isCurrent(requestId)) {
        setItems(response.data?.items || [])
      }
    } catch (error) {
      if (mounted.current && !controller.signal.aborted && listSequence.current.isCurrent(requestId)) {
        setLoadError(safeServiceError(error, 'MCP 服务加载失败，请重试'))
      }
    } finally {
      if (mounted.current && !controller.signal.aborted && listSequence.current.isCurrent(requestId)) setLoading(false)
    }
  }, [apiClient, collectionEndpoint, hasScope])

  useEffect(() => {
    resetWorkbench()
    load()
    return () => {
      listSequence.current.invalidate()
      listController.current?.abort()
    }
  }, [load, resetWorkbench])

  const loadTokens = useCallback(async (serviceId = openServiceId.current) => {
    if (!serviceId || openServiceId.current !== serviceId) return
    const response = await apiClient.get(serviceEndpoint(serviceId, 'tokens'))
    if (mounted.current && openServiceId.current === serviceId) setTokens(response.data?.items || [])
  }, [apiClient, serviceEndpoint])

  const openWorkbench = useCallback(async (service) => {
    resetWorkbench()
    openServiceId.current = service.service_id
    const requestId = detailSequence.current.begin()
    const controller = new AbortController()
    detailController.current = controller
    setDetail(service)
    setDetailLoading(true)
    try {
      const [serviceResponse, bindingResponse, tokenResponse, connectionResponse] = await Promise.all([
        apiClient.get(serviceEndpoint(service.service_id), { signal: controller.signal }),
        apiClient.get(serviceEndpoint(service.service_id, 'tools'), { signal: controller.signal }),
        apiClient.get(serviceEndpoint(service.service_id, 'tokens'), { signal: controller.signal }),
        apiClient.get(apiClientEndpoint(apiClient, connectionCollectionEndpoint(scope, selectedTenant)), { signal: controller.signal }),
      ])
      if (
        !mounted.current || controller.signal.aborted
        || openServiceId.current !== service.service_id
        || !detailSequence.current.isCurrent(requestId)
      ) return
      setDetail(serviceResponse.data?.service || service)
      setBindings((bindingResponse.data?.items || []).map(binding => ({
        ...binding,
        policyText: JSON.stringify(binding.policy || {}, null, 2),
      })))
      setTokens(tokenResponse.data?.items || [])
      setConnections(connectionResponse.data?.items || [])
    } catch (error) {
      if (!controller.signal.aborted && openServiceId.current === service.service_id) {
        messageApi.error(safeServiceError(error, '服务工作台加载失败，请重试'))
      }
    } finally {
      if (!controller.signal.aborted && openServiceId.current === service.service_id) setDetailLoading(false)
    }
  }, [apiClient, messageApi, resetWorkbench, scope, selectedTenant, serviceEndpoint])

  const selectConnection = async (connectionId) => {
    toolController.current?.abort()
    const controller = new AbortController()
    toolController.current = controller
    const connection = connections.find(item => item.connection_id === connectionId)
    setAvailableTools([])
    setDraftBinding(current => ({ ...current, connection_id: connectionId, source_tool_key: '', tool_alias: '' }))
    if (!connection) return
    if (!String(connection.connection_alias || '').trim()) {
      messageApi.error('连接缺少权威别名，无法创建工具绑定')
      return
    }
    try {
      const response = await apiClient.get(
        apiClientEndpoint(apiClient, connectionResourceEndpoint(scope, connectionId, 'tools')),
        { signal: controller.signal },
      )
      if (!controller.signal.aborted && openServiceId.current) setAvailableTools(response.data?.items || [])
    } catch (error) {
      if (!controller.signal.aborted) messageApi.error(safeServiceError(error, '连接工具加载失败，请重试'))
    }
  }

  const selectTool = toolKey => {
    const connection = connections.find(item => item.connection_id === draftBinding.connection_id)
    const tool = availableTools.find(item => item.tool_key === toolKey)
    try {
      setDraftBinding(current => ({
        ...current,
        source_tool_key: toolKey,
        tool_alias: bindingAliasPreview(connection, tool),
      }))
    } catch {
      setDraftBinding(current => ({ ...current, source_tool_key: '', tool_alias: '' }))
      messageApi.error('连接缺少权威别名，无法生成工具别名')
    }
  }

  const pendingConflicts = useMemo(() => aliasConflicts([
    ...bindings,
    ...(draftBinding.connection_id && draftBinding.source_tool_key && draftBinding.tool_alias ? [draftBinding] : []),
  ]), [bindings, draftBinding])

  const addBinding = () => {
    if (!draftBinding.connection_id || !draftBinding.source_tool_key || !draftBinding.tool_alias.trim()) {
      messageApi.warning('请选择连接和工具，并确认工具别名')
      return
    }
    try {
      const policy = parsePolicy(draftBinding.policyText)
      const next = {
        connection_id: draftBinding.connection_id,
        source_tool_key: draftBinding.source_tool_key,
        tool_alias: draftBinding.tool_alias.trim(),
        binding_status: draftBinding.binding_status,
        policy,
        policyText: JSON.stringify(policy, null, 2),
      }
      const duplicateSource = bindings.some(binding => (
        binding.connection_id === next.connection_id && binding.source_tool_key === next.source_tool_key
      ))
      if (duplicateSource) {
        messageApi.warning('该连接工具已经绑定')
        return
      }
      if (aliasConflicts([...bindings, next]).length) {
        messageApi.warning('工具别名冲突，请修改后再添加')
        return
      }
      setBindings(current => [...current, next])
      setAvailableTools([])
      setDraftBinding({ connection_id: '', source_tool_key: '', tool_alias: '', binding_status: 'active', policyText: '{}' })
    } catch (error) {
      messageApi.warning(error.message)
    }
  }

  const saveBindings = async () => {
    if (!detail || pendingConflicts.length) return
    let itemsPayload
    try {
      itemsPayload = bindings.map(binding => bindingPayload({
        ...binding,
        policy: parsePolicy(binding.policyText),
      }))
    } catch (error) {
      messageApi.warning(error.message)
      return
    }
    mutationController.current?.abort()
    const controller = new AbortController()
    mutationController.current = controller
    const serviceId = detail.service_id
    setMutationBusy('bindings')
    try {
      const response = await apiClient.put(serviceEndpoint(serviceId, 'tools'), {
        items: itemsPayload,
        expected_config_version: detail.config_version,
      }, { signal: controller.signal })
      if (!controller.signal.aborted && openServiceId.current === serviceId) {
        setDetail(response.data?.service || detail)
        messageApi.success('服务工具绑定已保存')
        load()
      }
    } catch (error) {
      if (!controller.signal.aborted && openServiceId.current === serviceId) {
        messageApi.error(safeServiceError(error, '工具绑定保存失败，请刷新后重试'))
      }
    } finally {
      if (!controller.signal.aborted && openServiceId.current === serviceId) setMutationBusy('')
    }
  }

  const changeStatus = async (status) => {
    if (!detail || (status === 'active' && pendingConflicts.length)) return
    mutationController.current?.abort()
    const controller = new AbortController()
    mutationController.current = controller
    const serviceId = detail.service_id
    setMutationBusy(status)
    try {
      const response = await apiClient.patch(serviceEndpoint(serviceId), {
        status,
        expected_config_version: detail.config_version,
      }, { signal: controller.signal })
      if (!controller.signal.aborted && openServiceId.current === serviceId) {
        setDetail(response.data?.service || { ...detail, status })
        messageApi.success(status === 'active' ? '服务已发布并激活' : '服务已停用')
        load()
      }
    } catch (error) {
      if (!controller.signal.aborted && openServiceId.current === serviceId) {
        messageApi.error(safeServiceError(error, '服务状态更新失败，请刷新后重试'))
      }
    } finally {
      if (!controller.signal.aborted && openServiceId.current === serviceId) setMutationBusy('')
    }
  }

  const confirmDisable = () => modal.confirm({
    title: '停用这个 MCP 服务？',
    content: '停用后，现有服务 Token 将无法调用该服务。',
    okText: '确认停用',
    okButtonProps: { danger: true },
    cancelText: '取消',
    onOk: () => changeStatus('disabled'),
  })

  const create = async () => {
    setCreateBusy(true)
    try {
      const values = await form.validateFields()
      const response = await apiClient.post(collectionEndpoint(), {
        display_name: values.display_name.trim(),
        service_key: values.service_key.trim(),
      })
      form.resetFields()
      setCreateOpen(false)
      messageApi.success('MCP 服务已创建')
      await load()
      if (response.data?.service) openWorkbench(response.data.service)
    } catch (error) {
      if (!error?.errorFields) messageApi.error(safeServiceError(error, 'MCP 服务创建失败，请重试'))
    } finally {
      setCreateBusy(false)
    }
  }

  const visibleItems = useMemo(() => items.filter(item => {
    const term = query.trim().toLowerCase()
    return (!term || [item.display_name, item.service_key, item.service_id].some(value => String(value || '').toLowerCase().includes(term)))
      && (statusFilter === 'all' || item.status === statusFilter)
  }), [items, query, statusFilter])

  const columns = [
    {
      title: '服务', key: 'service', rowScope: 'row',
      render: (_, service) => <Space direction="vertical" size={0}><Text strong>{service.display_name}</Text><Text code>{service.service_key}</Text></Space>,
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 110,
      render: status => <Tag color={(SERVICE_STATUS[status] || SERVICE_STATUS.disabled).color}>{(SERVICE_STATUS[status] || SERVICE_STATUS.disabled).label}</Tag>,
    },
    { title: '配置版本', dataIndex: 'config_version', key: 'config_version', width: 110, responsive: ['md'] },
    { title: '服务 ID', dataIndex: 'service_id', key: 'service_id', responsive: ['lg'], render: value => <Text code>{value}</Text> },
    { title: '操作', key: 'actions', width: 120, render: (_, service) => <Button type="link" onClick={() => openWorkbench(service)}>编辑/发布</Button> },
  ]

  const selectTenant = value => {
    const next = value || ''
    setScopeTenant(next)
    setItems([])
    resetWorkbench()
    onTenantChange?.(next)
  }

  return (
    <main className="connection-workbench connection-workbench--embedded">
      {contextHolder}{modalContextHolder}
      <header className="connection-heading">
        <div><Text className="connection-eyebrow">SERVICE CONTROL / MCP</Text><Title level={1}>MCP 服务</Title><Paragraph>明确绑定连接工具，检查稳定别名，再发布租户级 MCP 服务。</Paragraph></div>
        <Button type="primary" size="large" icon={<PlusOutlined />} disabled={!hasScope} onClick={() => setCreateOpen(true)}>新建服务</Button>
      </header>

      <section className="connection-panel">
        <div className="connection-toolbar">
          {!tenantScope ? <Select aria-label="选择服务租户范围" showSearch optionFilterProp="label" placeholder="先选择租户" value={selectedTenant || undefined} options={tenants.map(tenant => ({ value: tenant.tenant_id, label: tenant.display_name ? `${tenant.display_name} · ${tenant.tenant_id}` : tenant.tenant_id }))} onChange={selectTenant} /> : null}
          <Input aria-label="搜索 MCP 服务" allowClear value={query} placeholder="搜索服务名称、Key 或 ID" onChange={event => setQuery(event.target.value)} />
          <Select aria-label="按状态筛选 MCP 服务" value={statusFilter} options={[{ value: 'all', label: '全部状态' }, ...Object.entries(SERVICE_STATUS).map(([value, meta]) => ({ value, label: meta.label }))]} onChange={setStatusFilter} />
          <Button icon={<ReloadOutlined />} disabled={!hasScope} loading={loading} onClick={load}>刷新</Button>
        </div>
        {!hasScope ? <Alert type="info" showIcon message="请先选择租户范围" description="管理后台不会跨租户列出或修改 MCP 服务。" /> : null}
        {loadError ? <Alert type="error" showIcon message="MCP 服务加载失败" description={loadError} action={<Button size="small" onClick={load}>重试</Button>} /> : null}
        <Table rowKey="service_id" columns={columns} dataSource={visibleItems} loading={loading} pagination={{ pageSize: 20, responsive: true }} locale={{ emptyText: <Empty description={hasScope ? '当前租户暂无 MCP 服务' : '请选择租户'}>{hasScope ? <Button type="primary" onClick={() => setCreateOpen(true)}>创建第一个服务</Button> : null}</Empty> }} />
      </section>

      <Drawer
        title={detail ? `${detail.display_name} · 服务工作台` : '服务工作台'}
        open={Boolean(detail)}
        loading={detailLoading}
        onClose={resetWorkbench}
        width="min(980px, 100vw)"
        extra={detail ? <Tag color={(SERVICE_STATUS[detail.status] || SERVICE_STATUS.disabled).color}>{(SERVICE_STATUS[detail.status] || SERVICE_STATUS.disabled).label}</Tag> : null}
      >
        {detail ? <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <Alert type="info" showIcon message="先保存绑定，再发布服务" description={`配置版本 ${detail.config_version}。发布和保存都使用版本检查，冲突时请刷新重试。`} />
          <Space wrap>
            <Button icon={<KeyOutlined />} onClick={() => setTokenOpen(true)}>管理 Token（{tokens.length}）</Button>
            {detail.status !== 'active' ? <Button type="primary" icon={<ApiOutlined />} loading={mutationBusy === 'active'} disabled={Boolean(mutationBusy) || pendingConflicts.length > 0} onClick={() => changeStatus('active')}>发布并激活</Button> : null}
            {detail.status !== 'disabled' ? <Button danger loading={mutationBusy === 'disabled'} disabled={Boolean(mutationBusy)} onClick={confirmDisable}>停用服务</Button> : null}
          </Space>
          {pendingConflicts.length ? <Alert type="error" showIcon message="工具别名冲突，无法保存或发布" description={pendingConflicts.join('、')} /> : null}

          <section aria-labelledby="binding-editor-title">
            <Title level={3} id="binding-editor-title">工具绑定</Title>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: 12, alignItems: 'start' }}>
              <label>连接<Select aria-label="绑定连接" style={{ width: '100%' }} value={draftBinding.connection_id || undefined} placeholder="选择连接" options={connections.map(connection => ({ value: connection.connection_id, label: connection.display_name ? `${connection.display_name} · ${connection.connection_alias || connection.connection_id}` : connection.connection_alias || connection.connection_id }))} onChange={selectConnection} /></label>
              <label>来源工具<Select aria-label="绑定来源工具" style={{ width: '100%' }} disabled={!draftBinding.connection_id} value={draftBinding.source_tool_key || undefined} placeholder="选择工具" options={availableTools.map(tool => ({ value: tool.tool_key, label: `${tool.mcp_name || tool.tool_key} · ${tool.tool_key}` }))} onChange={selectTool} /></label>
              <label>工具别名<Input aria-label="绑定工具别名" value={draftBinding.tool_alias} placeholder="选择工具后生成稳定预览" onChange={event => setDraftBinding(current => ({ ...current, tool_alias: event.target.value }))} /></label>
              <label>状态<Select aria-label="绑定状态" style={{ width: '100%' }} value={draftBinding.binding_status} options={[{ value: 'active', label: '启用' }, { value: 'disabled', label: '停用' }]} onChange={binding_status => setDraftBinding(current => ({ ...current, binding_status }))} /></label>
            </div>
            <label>绑定策略（JSON 对象）<Input.TextArea aria-label="新绑定策略 JSON" rows={3} value={draftBinding.policyText} onChange={event => setDraftBinding(current => ({ ...current, policyText: event.target.value }))} /></label>
            <Space style={{ marginTop: 12 }}><Button onClick={addBinding}>添加绑定</Button>{draftBinding.tool_alias ? <Text type="secondary">别名预览：<Text code>{draftBinding.tool_alias}</Text></Text> : null}</Space>
          </section>

          <Table
            rowKey={binding => binding.binding_id || `${binding.connection_id}:${binding.source_tool_key}`}
            pagination={false}
            dataSource={bindings}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未绑定工具" /> }}
            columns={[
              { title: '连接 / 来源工具', key: 'source', render: (_, binding) => <Space direction="vertical" size={0}><Text>{connections.find(item => item.connection_id === binding.connection_id)?.display_name || binding.connection_id}</Text><Text code>{binding.source_tool_key}</Text></Space> },
              { title: '工具别名', key: 'alias', render: (_, binding, index) => <Input aria-label={`${binding.source_tool_key} 工具别名`} value={binding.tool_alias} onChange={event => setBindings(current => current.map((item, itemIndex) => itemIndex === index ? { ...item, tool_alias: event.target.value } : item))} /> },
              { title: '状态', key: 'status', width: 120, render: (_, binding, index) => <Select aria-label={`${binding.source_tool_key} 绑定状态`} value={binding.binding_status} options={[{ value: 'active', label: '启用' }, { value: 'disabled', label: '停用' }]} onChange={binding_status => setBindings(current => current.map((item, itemIndex) => itemIndex === index ? { ...item, binding_status } : item))} /> },
              { title: '策略', key: 'policy', render: (_, binding, index) => <Input.TextArea aria-label={`${binding.source_tool_key} 绑定策略 JSON`} autoSize={{ minRows: 1, maxRows: 4 }} value={binding.policyText} onChange={event => setBindings(current => current.map((item, itemIndex) => itemIndex === index ? { ...item, policyText: event.target.value } : item))} /> },
              { title: '操作', key: 'remove', width: 80, render: (_, __, index) => <Button danger type="link" onClick={() => setBindings(current => current.filter((_, itemIndex) => itemIndex !== index))}>移除</Button> },
            ]}
          />
          <Button type="primary" loading={mutationBusy === 'bindings'} disabled={Boolean(mutationBusy) || pendingConflicts.length > 0} onClick={saveBindings}>保存全部绑定</Button>
        </Space> : null}
      </Drawer>

      <Modal title="新建 MCP 服务" open={createOpen} onCancel={() => { setCreateOpen(false); form.resetFields() }} onOk={create} confirmLoading={createBusy} okText="创建服务" cancelText="取消">
        <Form form={form} layout="vertical">
          <Form.Item name="display_name" label="服务名称" rules={[{ required: true, whitespace: true, max: 128 }]}><Input placeholder="例如 通讯录查询服务" /></Form.Item>
          <Form.Item name="service_key" label="服务 Key" extra="创建后保持稳定；使用字母开头，仅含字母、数字、_、- 或 ." rules={[{ required: true, whitespace: true, max: 64 }, { pattern: /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/, message: '请输入合法服务 Key' }]}><Input aria-label="服务 Key" autoComplete="off" /></Form.Item>
        </Form>
      </Modal>

      <ServiceTokenModal
        open={tokenOpen}
        service={detail}
        tokens={tokens}
        scope={scope}
        tenantId={selectedTenant}
        apiClient={apiClient}
        onClose={() => setTokenOpen(false)}
        onTokensChange={() => loadTokens(detail?.service_id)}
      />
    </main>
  )
}
