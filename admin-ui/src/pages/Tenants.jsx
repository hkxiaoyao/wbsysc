import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Grid,
  Input,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  FileSearchOutlined,
  GlobalOutlined,
  MoreOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons'
import api from '../api.js'
import {
  EMPTY_FILTERS,
  buildTenantIdentityPayload,
  buildTenantLoginStatusPatch,
  buildTenantPasswordReset,
  confirmedTenantLoginStatus,
  createTenantActionLock,
  createTenantRequestGeneration,
  filterTenants,
  getTenantStats,
  projectTenantLoginState,
  tenantLoginPasswordEndpoint,
  tenantLoginStatusEndpoint,
  tenantPasswordValidationError,
} from './tenantsView.js'
import './Tenants.css'

const { Text, Paragraph } = Typography

export default function Tenants({
  onViewLogs = () => {},
  onViewConnections = () => {},
}) {
  const [data, setData] = useState([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [filters, setFilters] = useState({ ...EMPTY_FILTERS })
  const [rowActions, setRowActions] = useState(() => new Set())
  const [editorOpen, setEditorOpen] = useState(false)
  const [editorDirty, setEditorDirty] = useState(false)
  const [editing, setEditing] = useState(null)
  const [saving, setSaving] = useState(false)
  const [resetPassword, setResetPassword] = useState('')
  const [form] = Form.useForm()
  const [modal, modalContextHolder] = Modal.useModal()
  const [messageApi, messageContextHolder] = message.useMessage()
  const screens = Grid.useBreakpoint()
  const compactTable = !screens.md
  const mounted = useRef(true)
  const editorTenantId = useRef('')
  const rowActionLocks = useRef(createTenantActionLock())
  const listGeneration = useRef(createTenantRequestGeneration())
  const listController = useRef(null)
  const editorMutationGeneration = useRef(createTenantRequestGeneration())
  const editorMutationController = useRef(null)

  const stats = useMemo(() => getTenantStats(data), [data])
  const visibleTenants = useMemo(() => filterTenants(data, filters), [data, filters])
  const editingLoginState = useMemo(() => projectTenantLoginState(editing || {}), [editing])
  const hasFilters = Boolean(filters.query) || filters.enabled !== 'all'

  const load = async () => {
    const ticket = listGeneration.current.begin('__list__', 'load')
    listController.current?.abort()
    const controller = new AbortController()
    listController.current = controller
    const isCurrent = () => mounted.current
      && !controller.signal.aborted
      && listGeneration.current.isCurrent(ticket, '__list__', 'load')
    setLoading(true)
    setLoadError('')
    try {
      const response = await api.get('/admin/tenants', { signal: controller.signal })
      if (!isCurrent()) return null
      const items = response.data.items || []
      setData(items)
      setEditing((current) => {
        if (!current || current.tenant_id !== editorTenantId.current) return current
        return items.find((row) => row.tenant_id === current.tenant_id) || current
      })
      return items
    } catch (error) {
      if (isCurrent()) {
        setLoadError(`租户列表加载失败：${error.response?.data?.detail || error.message}`)
      }
      return null
    } finally {
      if (isCurrent()) setLoading(false)
    }
  }

  useEffect(() => {
    mounted.current = true
    load()
    return () => {
      mounted.current = false
      listGeneration.current.invalidate()
      editorMutationGeneration.current.invalidate()
      listController.current?.abort()
      editorMutationController.current?.abort()
    }
  }, [])

  const invalidateEditorMutation = () => {
    editorMutationGeneration.current.invalidate()
    editorMutationController.current?.abort()
  }

  const beginEditorMutation = (tenantId, action) => {
    listGeneration.current.invalidate()
    listController.current?.abort()
    if (mounted.current) setLoading(false)
    editorMutationController.current?.abort()
    const controller = new AbortController()
    editorMutationController.current = controller
    return {
      controller,
      ticket: editorMutationGeneration.current.begin(tenantId, action),
    }
  }

  const isEditorMutationCurrent = (ticket, controller, tenantId, action) => (
    mounted.current
    && !controller.signal.aborted
    && editorTenantId.current === tenantId
    && editorMutationGeneration.current.isCurrent(ticket, tenantId, action)
  )

  const beginRowAction = (row, action) => {
    if (!rowActionLocks.current.acquire(row.tenant_id, action)) return false
    setRowActions((current) => new Set(current).add(row.tenant_id))
    return true
  }

  const endRowAction = (row, action) => {
    if (!rowActionLocks.current.release(row.tenant_id, action) || !mounted.current) return
    setRowActions((current) => {
      const next = new Set(current)
      next.delete(row.tenant_id)
      return next
    })
  }

  const isRowBusy = (row) => rowActions.has(row.tenant_id)

  const openCreate = () => {
    invalidateEditorMutation()
    editorTenantId.current = ''
    setResetPassword('')
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ enabled: true })
    setEditorDirty(false)
    setEditorOpen(true)
  }

  const openEdit = (row) => {
    invalidateEditorMutation()
    editorTenantId.current = row.tenant_id
    setResetPassword('')
    setEditing(row)
    form.resetFields()
    form.setFieldsValue({
      tenant_id: row.tenant_id,
      display_name: row.display_name,
      enabled: row.enabled,
    })
    setEditorDirty(false)
    setEditorOpen(true)
  }

  const closeEditor = () => {
    invalidateEditorMutation()
    editorTenantId.current = ''
    setResetPassword('')
    setEditorDirty(false)
    setEditorOpen(false)
    setEditing(null)
    form.resetFields()
  }

  const requestCloseEditor = () => {
    if (saving) return
    if (!editorDirty) {
      closeEditor()
      return
    }
    modal.confirm({
      title: '放弃未保存的修改？',
      content: '当前租户信息已发生变化，关闭后这些修改不会保留。',
      okText: '放弃修改',
      cancelText: '继续编辑',
      okButtonProps: { danger: true },
      onOk: closeEditor,
    })
  }

  const submit = async () => {
    if (saving) return
    setSaving(true)
    let mutationIsCurrent = () => mounted.current
    let lockedRow = null
    try {
      const values = await form.validateFields()
      const payload = buildTenantIdentityPayload(values, { editing: Boolean(editing) })
      const tenantId = editing?.tenant_id || ''
      const action = editing ? 'save' : 'create'
      if (editing) {
        if (!beginRowAction(editing, action)) return
        lockedRow = editing
      }
      const { ticket, controller } = beginEditorMutation(tenantId, action)
      const isCurrent = () => isEditorMutationCurrent(ticket, controller, tenantId, action)
      mutationIsCurrent = isCurrent
      if (editing) {
        await api.put(
          `/admin/tenants/${encodeURIComponent(editing.tenant_id)}`,
          payload,
          { signal: controller.signal },
        )
        if (!isCurrent()) return
        messageApi.success('租户信息已更新')
      } else {
        await api.post('/admin/tenants', payload, { signal: controller.signal })
        if (!isCurrent()) return
        messageApi.success('租户已创建')
      }
      closeEditor()
      load()
    } catch (error) {
      if (!error.errorFields && mutationIsCurrent()) {
        messageApi.error(`保存失败：${error.response?.data?.detail || error.message}`)
      }
    } finally {
      if (lockedRow) endRowAction(lockedRow, 'save')
      if (mounted.current) setSaving(false)
    }
  }

  const remove = (row) => {
    modal.confirm({
      title: `删除租户 ${row.tenant_id}？`,
      content: '此操作会删除租户身份配置，请确认该租户已不再使用。',
      okType: 'danger',
      onOk: async () => {
        try {
          await api.delete(`/admin/tenants/${encodeURIComponent(row.tenant_id)}`)
          messageApi.success('租户已删除')
          load()
        } catch (error) {
          messageApi.error(`删除失败：${error.response?.data?.detail || error.message}`)
          throw error
        }
      },
    })
  }

  const resetTenantLoginPassword = (row) => {
    const validationError = tenantPasswordValidationError(resetPassword)
    if (validationError) {
      messageApi.error(validationError)
      return
    }
    const exactPassword = resetPassword
    modal.confirm({
      title: `重置登录密码 · ${row.display_name || row.tenant_id}`,
      content: '重置后现有租户登录会话将失效。',
      okText: '确认重置',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        if (!beginRowAction(row, 'login-reset')) return
        const tenantId = row.tenant_id
        const action = 'login-reset'
        const { ticket, controller } = beginEditorMutation(tenantId, action)
        const isCurrent = () => isEditorMutationCurrent(ticket, controller, tenantId, action)
        try {
          await api.put(
            tenantLoginPasswordEndpoint(tenantId),
            buildTenantPasswordReset(exactPassword),
            { signal: controller.signal },
          )
          if (!isCurrent()) return
          setResetPassword('')
          messageApi.success('登录密码已设置，旧会话已失效')
          await load()
        } catch (error) {
          if (isCurrent()) {
            messageApi.error(`重置失败：${error.response?.data?.detail || error.message}`)
          }
        } finally {
          endRowAction(row, action)
        }
      },
    })
  }

  const updateTenantLoginStatus = (row, status) => {
    const run = async () => {
      if (!beginRowAction(row, 'login-status')) return
      const tenantId = row.tenant_id
      const action = 'login-status'
      const { ticket, controller } = beginEditorMutation(tenantId, action)
      const isCurrent = () => isEditorMutationCurrent(ticket, controller, tenantId, action)
      try {
        const response = await api.put(
          tenantLoginStatusEndpoint(tenantId),
          buildTenantLoginStatusPatch(status),
          { signal: controller.signal },
        )
        if (!isCurrent()) return
        const confirmedStatus = confirmedTenantLoginStatus(response.data, status)
        if (!confirmedStatus) throw new Error('租户登录状态响应未确认')
        const refreshedItems = await load()
        if (!isCurrent()) return
        const refreshed = Array.isArray(refreshedItems)
          ? refreshedItems.find((item) => item.tenant_id === tenantId)
          : null
        if (projectTenantLoginState(refreshed).status !== confirmedStatus) {
          messageApi.error('登录状态尚未得到列表确认，请刷新后重试')
          return
        }
        setResetPassword('')
        messageApi.success(status === 'active' ? '租户登录已启用' : '租户登录已禁用')
      } catch (error) {
        if (!isCurrent()) return
        messageApi.error(error.response?.status === 409
          ? '该租户尚未设置登录密码，请先设置密码'
          : '登录状态更新失败，请刷新后重试')
      } finally {
        endRowAction(row, action)
      }
    }

    if (status === 'active') {
      run()
      return
    }
    modal.confirm({
      title: `禁用登录 · ${row.display_name || row.tenant_id}`,
      content: '禁用后该租户无法登录，现有登录会话也会立即失效。',
      okText: '确认禁用',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: run,
    })
  }

  const actionMenu = (row) => ({
    items: [
      { key: 'connections', icon: <GlobalOutlined />, label: '连接中心' },
      { key: 'logs', icon: <FileSearchOutlined />, label: '查看调用日志' },
      { type: 'divider' },
      { key: 'delete', icon: <DeleteOutlined />, label: '删除租户', danger: true },
    ],
    onClick: ({ key }) => {
      if (key === 'connections') onViewConnections(row.tenant_id)
      if (key === 'logs') onViewLogs(row.tenant_id)
      if (key === 'delete') remove(row)
    },
  })

  const loginStatusTag = (row) => {
    const state = projectTenantLoginState(row)
    if (state.kind === 'active') return <Tag color="success">已启用</Tag>
    if (state.kind === 'disabled') return <Tag>已禁用</Tag>
    if (state.kind === 'none') return <Tag color="warning">未设置密码</Tag>
    return <Tag color="warning">未知</Tag>
  }

  const columns = [
    {
      title: '租户名称',
      dataIndex: 'display_name',
      key: 'display_name',
      width: compactTable ? 130 : 220,
      rowScope: 'row',
      render: (name, row) => <Text strong>{name || row.tenant_id}</Text>,
    },
    {
      title: '租户 ID',
      dataIndex: 'tenant_id',
      key: 'tenant_id',
      width: compactTable ? 130 : 220,
      render: (tenantId) => (
        <Tooltip title={tenantId}>
          <Text className="tenant-code" copyable={compactTable ? false : { text: tenantId }}>
            {tenantId}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: '租户状态',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 120,
      render: (enabled) => (
        <Badge status={enabled ? 'success' : 'default'} text={enabled ? '已启用' : '已禁用'} />
      ),
    },
    {
      title: '登录状态',
      key: 'login_status',
      width: 130,
      render: (_, row) => loginStatusTag(row),
    },
    {
      title: '操作',
      key: 'operation',
      width: compactTable ? 84 : 180,
      fixed: 'right',
      render: (_, row) => (
        <Space size={compactTable ? 4 : 8}>
          <Button
            type="primary"
            ghost
            size={compactTable ? 'small' : 'middle'}
            icon={<EditOutlined />}
            aria-label={`编辑租户：${row.display_name || row.tenant_id}`}
            onClick={() => openEdit(row)}
          >
            {compactTable ? null : '编辑'}
          </Button>
          <Dropdown menu={actionMenu(row)} trigger={['click']}>
            <Button
              size={compactTable ? 'small' : 'middle'}
              icon={compactTable ? <MoreOutlined /> : null}
              aria-label={`更多操作：${row.display_name || row.tenant_id}`}
              loading={isRowBusy(row)}
            >
              {compactTable ? null : <>更多 <DownOutlined /></>}
            </Button>
          </Dropdown>
        </Space>
      ),
    },
  ]

  return (
    <>
      {modalContextHolder}
      {messageContextHolder}
      <main className="tenant-workbench">
        <header className="tenant-heading">
          <div>
            <Text className="tenant-eyebrow">TENANT MANAGEMENT</Text>
            <h1>租户管理</h1>
            <Paragraph>管理租户身份、租户状态与登录权限；连接配置请前往连接中心。</Paragraph>
          </div>
          <Button type="primary" size="large" icon={<PlusOutlined />} onClick={openCreate}>
            新增租户
          </Button>
        </header>

        <section className="tenant-status-rail" aria-label="租户状态概览">
          {[
            ['全部租户', stats.total, 'total'],
            ['已启用', stats.enabled, 'running'],
            ['已禁用', stats.disabled, 'attention'],
          ].map(([label, value, tone]) => (
            <div className={`status-rail-item status-rail-item--${tone}`} key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
            </div>
          ))}
        </section>

        <section className="tenant-panel">
          <div className="tenant-toolbar">
            <Input
              allowClear
              aria-label="搜索租户"
              prefix={<SearchOutlined />}
              value={filters.query}
              placeholder="搜索租户名称或 ID"
              onChange={(event) => setFilters((current) => ({
                ...current,
                query: event.target.value,
              }))}
            />
            <Select
              value={filters.enabled}
              aria-label="按租户状态筛选"
              onChange={(enabled) => setFilters((current) => ({ ...current, enabled }))}
              options={[
                { value: 'all', label: '全部状态' },
                { value: 'enabled', label: '已启用' },
                { value: 'disabled', label: '已禁用' },
              ]}
            />
            <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button>
          </div>

          {loadError && (
            <Alert
              type="error"
              showIcon
              message={loadError}
              action={<Button size="small" onClick={load}>重试</Button>}
            />
          )}

          <Table
            rowKey="tenant_id"
            columns={columns}
            dataSource={visibleTenants}
            loading={loading}
            pagination={false}
            size="middle"
            scroll={{ x: compactTable ? 760 : 900 }}
            locale={{
              emptyText: loading || loadError ? null : !data.length ? (
                <Empty description="还没有租户">
                  <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                    新增租户
                  </Button>
                </Empty>
              ) : (
                <Empty description="没有匹配的租户">
                  {hasFilters && (
                    <Button onClick={() => setFilters({ ...EMPTY_FILTERS })}>清空筛选</Button>
                  )}
                </Empty>
              ),
            }}
          />
        </section>
      </main>

      <Drawer
        rootClassName="tenant-editor"
        title={editing ? '编辑租户' : '新增租户'}
        open={editorOpen}
        onClose={requestCloseEditor}
        width={620}
        destroyOnHidden={false}
        maskClosable={!saving}
        keyboard={!saving}
        closable={!saving}
        footer={(
          <div className="tenant-editor-footer">
            <Button onClick={requestCloseEditor} disabled={saving}>取消</Button>
            <Button
              type="primary"
              onClick={submit}
              loading={saving}
              disabled={Boolean(editing && isRowBusy(editing))}
            >
              保存
            </Button>
          </div>
        )}
      >
        <Form
          form={form}
          layout="vertical"
          requiredMark="optional"
          onValuesChange={() => setEditorDirty(true)}
        >
          <section className="tenant-form-section">
            <div className="tenant-form-section__heading">
              <div>
                <h2>租户身份</h2>
                <p>连接、凭据、同步策略和 MCP Token 均在全局连接中心管理。</p>
              </div>
            </div>
            <Form.Item
              name="tenant_id"
              label="租户 ID"
              rules={[{ required: true, message: '请输入租户 ID' }]}
            >
              <Input disabled={Boolean(editing)} placeholder="如 customerA" />
            </Form.Item>
            <Form.Item name="display_name" label="租户名称">
              <Input placeholder="企业或项目名称" />
            </Form.Item>
            <Form.Item name="enabled" label="启用租户" valuePropName="checked">
              <Switch />
            </Form.Item>
            {!editing && (
              <Form.Item
                name="tenant_password"
                label="初始登录密码"
                extra="12–256 个字符，首尾不能有空格，且不能包含 password（不区分大小写）。"
                rules={[{
                  required: true,
                  message: '请输入初始登录密码',
                }, {
                  validator: (_, value) => {
                    const error = tenantPasswordValidationError(value ?? '', { optional: false })
                    return error ? Promise.reject(new Error(error)) : Promise.resolve()
                  },
                }]}
              >
                <Input.Password
                  autoComplete="new-password"
                  aria-label="租户初始登录密码"
                  placeholder="输入租户初始登录密码"
                />
              </Form.Item>
            )}
          </section>

          {editing && (
            <section className="tenant-form-section" aria-labelledby="tenant-login-security-heading">
              <div className="tenant-form-section__heading">
                <div>
                  <h2 id="tenant-login-security-heading">登录安全</h2>
                  <p>密码不会被读取或回显。重置密码和禁用登录都会撤销现有会话。</p>
                </div>
              </div>
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <div aria-live="polite">
                  <Text>登录状态：</Text>{' '}
                  {loginStatusTag(editing)}
                </div>
                <Space wrap>
                  <Button
                    onClick={() => updateTenantLoginStatus(editing, 'active')}
                    loading={isRowBusy(editing)}
                    disabled={editingLoginState.kind === 'active'
                      || editingLoginState.kind === 'none'
                      || editingLoginState.kind === 'unknown'}
                  >
                    启用登录
                  </Button>
                  <Button
                    danger
                    onClick={() => updateTenantLoginStatus(editing, 'disabled')}
                    loading={isRowBusy(editing)}
                    disabled={editingLoginState.kind === 'disabled'
                      || editingLoginState.kind === 'none'
                      || editingLoginState.kind === 'unknown'}
                  >
                    禁用登录
                  </Button>
                </Space>
                <div>
                  <Text strong>设置或重置登录密码</Text>
                  <Paragraph type="secondary" style={{ margin: '4px 0 8px' }}>
                    12–256 个字符，首尾不能有空格，且不能包含 password。
                  </Paragraph>
                  <Space.Compact style={{ width: '100%' }}>
                    <Input.Password
                      value={resetPassword}
                      onChange={(event) => setResetPassword(event.target.value)}
                      autoComplete="new-password"
                      aria-label="租户新登录密码"
                      placeholder="输入新的租户登录密码"
                      disabled={isRowBusy(editing)}
                    />
                    <Button
                      danger
                      onClick={() => resetTenantLoginPassword(editing)}
                      loading={isRowBusy(editing)}
                      disabled={!resetPassword || isRowBusy(editing)}
                    >
                      确认重置
                    </Button>
                  </Space.Compact>
                </div>
              </Space>
            </section>
          )}
        </Form>
      </Drawer>
    </>
  )
}
