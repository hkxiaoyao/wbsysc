import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Dropdown,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from 'antd'
import {
  CopyOutlined,
  DeleteOutlined,
  DownOutlined,
  EditOutlined,
  GlobalOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import api from '../api.js'
import { EMPTY_FILTERS, filterTenants, getDirectModeReason, getTenantStats } from './tenantsView.js'

const { Text, Paragraph, Link } = Typography
const MODULES = ['report', 'approval', 'checkin']

function buildMcpConfig(row) {
  // 优先租户可信域名（反代后对外域名），否则当前访问 origin
  const origin = row.trusted_domain
    ? `https://${row.trusted_domain}`
    : window.location.origin
  const serverKey = row.tenant_id || 'wecom-gateway'
  return {
    mcpServers: {
      [serverKey]: {
        type: 'http',
        url: `${origin}/mcp`,
        headers: {
          Authorization: `Bearer ${row.mcp_token}`,
        },
      },
    },
  }
}

export default function Tenants() {
  const [data, setData] = useState([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [filters, setFilters] = useState({ ...EMPTY_FILTERS })
  const [rowActions, setRowActions] = useState(() => new Set())
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [saving, setSaving] = useState(false)
  const [mcpLoadingTenant, setMcpLoadingTenant] = useState(null)
  const [mcpModal, setMcpModal] = useState({ open: false, title: '', text: '' })
  const [domainModal, setDomainModal] = useState({
    open: false, tenant: null, domain: '', fileList: [], uploading: false, info: null,
  })
  const [form] = Form.useForm()

  const stats = useMemo(() => getTenantStats(data), [data])
  const visibleTenants = useMemo(() => filterTenants(data, filters), [data, filters])
  const hasFilters = Boolean(filters.query) || filters.dataMode !== 'all' || filters.enabled !== 'all'

  const load = async () => {
    setLoading(true)
    setLoadError('')
    try {
      const r = await api.get('/admin/tenants')
      setData(r.data.items || [])
    } catch (e) {
      setLoadError('租户列表加载失败：' + (e.response?.data?.detail || e.message))
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({
      enabled_modules: MODULES,
      sync_interval_min: 30,
      enabled: true,
      data_mode: 'stored',
    })
    setModalOpen(true)
  }

  const openEdit = (row) => {
    setEditing(row)
    form.resetFields()
    form.setFieldsValue({
      ...row,
      enabled_modules: (row.enabled_modules || '').split(',').filter(Boolean),
      secret: '',          // 编辑时密钥留空=不改
      contact_secret: '',
      mcp_token: '',
      trusted_domain: row.trusted_domain || '',
    })
    setModalOpen(true)
  }

  const submit = async () => {
    if (saving) return
    setSaving(true)
    try {
      const v = await form.validateFields()
      const payload = {
        ...v,
        enabled_modules: (v.enabled_modules || []).join(','),
        checkin_userids: v.checkin_userids || '',
        trusted_domain: (v.trusted_domain || '').trim(),
      }
      if (editing) {
        await api.put(`/admin/tenants/${editing.tenant_id}`, payload)
        message.success('已更新')
      } else {
        await api.post('/admin/tenants', payload)
        message.success('已新增(已建schema)')
      }
      setModalOpen(false)
      load()
    } catch (e) {
      if (!e.errorFields) {
        message.error('保存失败: ' + (e.response?.data?.detail || e.message))
      }
    } finally {
      setSaving(false)
    }
  }

  const remove = (row) => {
    Modal.confirm({
      title: `删除租户 ${row.tenant_id}?`,
      content: '仅删除配置，历史数据schema保留(需另行手动删)。',
      okType: 'danger',
      onOk: async () => {
        await api.delete(`/admin/tenants/${row.tenant_id}`)
        message.success('已删除')
        load()
      },
    })
  }

  const rowActionKey = (row, action) => `${row.tenant_id}:${action}`
  const beginRowAction = (row, action) => setRowActions((current) => {
    const next = new Set(current)
    next.add(rowActionKey(row, action))
    return next
  })
  const endRowAction = (row, action) => setRowActions((current) => {
    const next = new Set(current)
    next.delete(rowActionKey(row, action))
    return next
  })
  const isRowBusy = (row) => [...rowActions].some((key) => key.startsWith(`${row.tenant_id}:`))

  const syncNow = async (row, opts = {}) => {
    const lookback = opts.lookback_days ?? 30
    const reset = !!opts.reset_cursor
    const action = reset ? 'force-sync' : 'sync'
    beginRowAction(row, action)
    try {
      const qs = new URLSearchParams({
        lookback_days: String(lookback),
        force: reset ? 'true' : 'false',
        reset_cursor: reset ? 'true' : 'false',
      })
      const r = await api.post(`/admin/tenants/${row.tenant_id}/sync?${qs.toString()}`)
      message.success(r.data?.msg || `${row.tenant_id} 同步已触发(后台执行)`)
    } catch (e) {
      message.error('触发失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      endRowAction(row, action)
    }
  }

  const openForceSync = (row) => {
    let days = 90
    Modal.confirm({
      title: `强制全量回拨同步 · ${row.tenant_id}`,
      content: (
        <div>
          <p style={{ marginBottom: 8 }}>
            将游标回拨到「现在 − N 天」，并强制按该窗口重新拉取汇报/审批/打卡。
            用于企微有数据但库条数偏少（如企微3条库只有2条）。
          </p>
          <div>
            回拨天数 N：
            <InputNumber
              min={1}
              max={180}
              defaultValue={90}
              style={{ marginLeft: 8, width: 100 }}
              onChange={(v) => { days = Number(v) || 90 }}
            />
          </div>
        </div>
      ),
      okText: '开始全量同步',
      cancelText: '取消',
      onOk: () => syncNow(row, { lookback_days: days, reset_cursor: true }),
    })
  }

  const diagnoseSync = async (row) => {
    beginRowAction(row, 'diagnose')
    try {
      const r = await api.get(`/admin/tenants/${row.tenant_id}/sync-diagnose`, {
        params: { lookback_days: 90 },
      })
      const d = r.data || {}
      Modal.info({
        title: `同步诊断 · ${row.tenant_id}`,
        width: 560,
        content: (
          <div style={{ fontSize: 13, lineHeight: 1.7 }}>
            <div>企微列表 errcode: <Text code>{String(d.errcode)}</Text> {d.errmsg}</div>
            <div>企微 journaluuid 条数: <Text strong>{d.list_len}</Text></div>
            <div>库内 wecom_report 条数: <Text strong>{d.db_report_count}</Text></div>
            <div>游标 last_value: <Text code>{String(d.db_report_cursor ?? '—')}</Text></div>
            <div>窗口: [{d.starttime}, {d.endtime}] lookback={d.lookback_days}天</div>
            <div>样例单号: {(d.sample_uuids || []).join(', ') || '—'}</div>
            {d.list_len > d.db_report_count && (
              <Paragraph type="warning" style={{ marginTop: 8 }}>
                企微条数 &gt; 库条数：请点「全量回拨」再同步。
              </Paragraph>
            )}
            {d.list_len === 0 && (
              <Paragraph type="danger" style={{ marginTop: 8 }}>
                企微 API 返回 0 条。请核对汇报应用授权、可见范围、是否为「汇报」单据。
              </Paragraph>
            )}
            {d.list_len > 0 && d.list_len === d.db_report_count && (
              <Paragraph type="success" style={{ marginTop: 8 }}>
                条数一致。若业务侧看到更多，可能不在该应用 API 可见范围或时间窗外。
              </Paragraph>
            )}
          </div>
        ),
      })
    } catch (e) {
      message.error('诊断失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      endRowAction(row, 'diagnose')
    }
  }

  const openMcpConfig = async (row) => {
    if (mcpLoadingTenant) return
    if (!row.has_mcp_token) {
      message.warning('该租户未配置 MCP Token')
      return
    }
    setMcpLoadingTenant(row.tenant_id)
    try {
      const r = await api.get(`/admin/tenants/${row.tenant_id}/mcp-config`)
      const text = JSON.stringify(buildMcpConfig({ ...row, ...r.data }), null, 2)
      setMcpModal({
        open: true,
        title: `MCP 配置 · ${row.display_name || row.tenant_id}`,
        text,
      })
    } catch (e) {
      message.error('读取 MCP 配置失败: ' + (e.response?.data?.detail || e.message))
    } finally {
      setMcpLoadingTenant(null)
    }
  }

  const openDomain = async (row) => {
    setDomainModal({
      open: true,
      tenant: row,
      domain: row.trusted_domain || '',
      fileList: [],
      uploading: false,
      info: null,
    })
    try {
      const r = await api.get(`/admin/tenants/${row.tenant_id}/domain-verify`)
      setDomainModal((s) => ({
        ...s,
        domain: r.data.trusted_domain || row.trusted_domain || '',
        info: r.data,
      }))
    } catch (e) {
      // 列表字段已有基础信息，查询失败不阻断
    }
  }

  const uploadDomainVerify = async () => {
    const { tenant, domain, fileList } = domainModal
    if (!tenant) return
    if (!fileList.length) {
      message.warning('请选择企微下载的校验文件（.txt）')
      return
    }
    const raw = fileList[0].originFileObj || fileList[0]
    const fd = new FormData()
    fd.append('file', raw, raw.name)
    if (domain) fd.append('trusted_domain', domain.trim())
    setDomainModal((s) => ({ ...s, uploading: true }))
    try {
      const r = await api.post(`/admin/tenants/${tenant.tenant_id}/domain-verify`, fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      message.success(r.data.msg || '上传成功')
      setDomainModal((s) => ({
        ...s,
        uploading: false,
        fileList: [],
        domain: r.data.trusted_domain || s.domain,
        info: {
          trusted_domain: r.data.trusted_domain,
          verify_filename: r.data.verify_filename,
          verify_url: r.data.verify_url,
          has_file: true,
        },
      }))
      load()
    } catch (e) {
      setDomainModal((s) => ({ ...s, uploading: false }))
      message.error('上传失败: ' + (e.response?.data?.detail || e.message))
    }
  }

  const removeDomainVerify = () => {
    const { tenant } = domainModal
    if (!tenant) return
    Modal.confirm({
      title: '删除校验文件？',
      content: '删除后根路径将无法访问该文件；可信域名配置会保留。',
      okType: 'danger',
      onOk: async () => {
        await api.delete(`/admin/tenants/${tenant.tenant_id}/domain-verify`)
        message.success('已删除')
        setDomainModal((s) => ({
          ...s,
          info: { ...(s.info || {}), has_file: false, verify_filename: '', verify_url: '' },
          fileList: [],
        }))
        load()
      },
    })
  }

  const copyText = async (text) => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.left = '-9999px'
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        document.body.removeChild(ta)
      }
      message.success('已复制到剪贴板')
    } catch (e) {
      message.error('复制失败，请手动选择文本复制')
    }
  }

  const actionMenu = (row) => {
    const directReason = getDirectModeReason(row)
    const syncLabel = (label) => directReason ? (
      <span className="tenant-menu-label">
        <span>{label}</span>
        <small>{directReason}</small>
      </span>
    ) : label

    return {
      items: [
        { key: 'mcp', icon: <CopyOutlined />, label: 'MCP 配置' },
        { key: 'domain', icon: <GlobalOutlined />, label: '可信域名' },
        { type: 'divider' },
        { key: 'sync', icon: <ThunderboltOutlined />, label: syncLabel('立即同步'), disabled: Boolean(directReason) },
        { key: 'force-sync', label: syncLabel('全量回拨'), disabled: Boolean(directReason) },
        { key: 'diagnose', label: syncLabel('同步诊断'), disabled: Boolean(directReason) },
        { type: 'divider' },
        { key: 'delete', icon: <DeleteOutlined />, label: '删除租户', danger: true },
      ],
      onClick: ({ key }) => {
        if (key === 'mcp') openMcpConfig(row)
        if (key === 'domain') openDomain(row)
        if (key === 'sync') syncNow(row)
        if (key === 'force-sync') openForceSync(row)
        if (key === 'diagnose') diagnoseSync(row)
        if (key === 'delete') remove(row)
      },
    }
  }

  const columns = [
    {
      title: '租户', key: 'tenant', width: 220, rowScope: 'row',
      render: (_, row) => (
        <div className="tenant-identity">
          <Text strong>{row.display_name || row.tenant_id}</Text>
          <Tooltip title={row.tenant_id}>
            <Text className="tenant-code" copyable={{ text: row.tenant_id }}>{row.tenant_id}</Text>
          </Tooltip>
        </div>
      ),
    },
    {
      title: '企业信息', key: 'company', width: 230, responsive: ['md'],
      render: (_, row) => (
        <div className="tenant-company">
          <Tooltip title={row.corpid}><Text className="tenant-code">{row.corpid}</Text></Tooltip>
          <Text type="secondary">{row.trusted_domain || '未配置可信域名'}</Text>
          <Space size={4} wrap>
            <Tag color={row.has_secret ? 'success' : 'error'}>应用{row.has_secret ? '已配置' : '缺失'}</Tag>
            <Tag color={row.has_contact_secret ? 'success' : 'default'}>通讯录{row.has_contact_secret ? '已配置' : '可选'}</Tag>
          </Space>
        </div>
      ),
    },
    {
      title: '数据模式', dataIndex: 'data_mode', key: 'data_mode', width: 130,
      render: (mode) => (
        <Tag className={`mode-tag mode-tag--${mode}`}>
          {mode === 'direct' ? '企微直连' : 'MySQL 存储'}
        </Tag>
      ),
    },
    {
      title: '同步策略', key: 'policy', width: 250, responsive: ['lg'],
      render: (_, row) => (
        <div className="tenant-policy">
          <Text>{row.data_mode === 'direct' ? '实时调用企微 API' : `每 ${row.sync_interval_min || 30} 分钟同步`}</Text>
          <Text type="secondary">
            {(row.enabled_modules || '').split(',').filter(Boolean).join(' · ') || '未启用模块'}
          </Text>
        </div>
      ),
    },
    {
      title: '状态', key: 'status', width: 140,
      render: (_, row) => (
        <div className="tenant-status">
          <Badge status={row.enabled ? 'success' : 'default'} text={row.enabled ? '已启用' : '已禁用'} />
          {!row.has_secret && <Text type="danger">缺少应用凭据</Text>}
        </div>
      ),
    },
    {
      title: '操作', key: 'operation', width: 176, fixed: 'right',
      render: (_, row) => (
        <Space size={8}>
          <Button type="primary" ghost icon={<EditOutlined />} onClick={() => openEdit(row)}>配置</Button>
          <Dropdown menu={actionMenu(row)} trigger={['click']}>
            <Button
              aria-label={`更多操作：${row.display_name || row.tenant_id}`}
              loading={isRowBusy(row) || mcpLoadingTenant === row.tenant_id}
            >
              更多 <DownOutlined />
            </Button>
          </Dropdown>
        </Space>
      ),
    },
  ]

  return (
    <>
      <main className="tenant-workbench">
        <header className="tenant-heading">
          <div>
            <Text className="tenant-eyebrow">TENANT OPERATIONS</Text>
            <h1>租户管理</h1>
            <Paragraph>管理企业接入、数据模式与同步状态。</Paragraph>
          </div>
          <Button type="primary" size="large" icon={<PlusOutlined />} onClick={openCreate}>新增租户</Button>
        </header>

        <section className="tenant-status-rail" aria-label="租户状态概览">
          {[
            ['全部租户', stats.total, 'total'],
            ['正常运行', stats.running, 'running'],
            ['直连模式', stats.direct, 'direct'],
            ['需要关注', stats.attention, 'attention'],
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
              placeholder="搜索名称、租户 ID 或 CorpID"
              onChange={(event) => setFilters((current) => ({ ...current, query: event.target.value }))}
            />
            <Select
              value={filters.dataMode}
              aria-label="按数据模式筛选"
              onChange={(dataMode) => setFilters((current) => ({ ...current, dataMode }))}
              options={[
                { value: 'all', label: '全部模式' },
                { value: 'stored', label: 'MySQL 存储' },
                { value: 'direct', label: '企微直连' },
              ]}
            />
            <Select
              value={filters.enabled}
              aria-label="按启用状态筛选"
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
            rowClassName={(row) => (
              `tenant-table-row tenant-table-row--${row.data_mode === 'direct' ? 'direct' : 'stored'}`
            )}
            scroll={{ x: 960 }}
            locale={{
              emptyText: loading || loadError ? null : !data.length ? (
                <Empty description="还没有租户，先添加第一个企业接入">
                  <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增租户</Button>
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

      <Modal title={editing ? '编辑租户' : '新增租户'} open={modalOpen} onOk={submit}
        onCancel={() => setModalOpen(false)} width={620} okText="保存" cancelText="取消"
        confirmLoading={saving}>
        <Form form={form} layout="vertical">
          <Form.Item name="tenant_id" label="租户ID" rules={[{ required: true }]}>
            <Input disabled={!!editing} placeholder="如 customerA" />
          </Form.Item>
          <Form.Item name="display_name" label="显示名称"><Input /></Form.Item>
          <Form.Item name="corpid" label="企业CorpID" rules={[{ required: true }]}>
            <Input placeholder="wwXXXXXXXX" />
          </Form.Item>
          <Form.Item name="mcp_token" label="MCP连接Token(workbuddy用)" rules={[{ required: !editing }]}
            extra={editing ? '留空=保留现有 Token' : '给客户配在 workbuddy 的 MCP Server headers；也可用列表「MCP配置」一键复制'}>
            <Input.Password placeholder={editing ? '****（不改留空）' : '长随机串'} />
          </Form.Item>
          <Form.Item name="secret" label="自建应用Secret"
            extra={editing ? '留空=不修改' : '必填'}>
            <Input.Password placeholder={editing ? '****（不改留空）' : ''} />
          </Form.Item>
          <Form.Item name="contact_secret" label="通讯录同步Secret（可选）"
            extra="配置后自动拉全企业userid喂打卡；留空=不改">
            <Input.Password placeholder={editing ? '****（不改留空）' : ''} />
          </Form.Item>
          <Form.Item name="data_mode" label="数据模式" rules={[{ required: true }]}
            extra="缓存模式定时写入 MySQL；企微直连每次实时请求且不保存业务数据">
            <Select options={[
              { value: 'stored', label: '缓存模式（MySQL）' },
              { value: 'direct', label: '企微直连（不保存业务数据）' },
            ]} />
          </Form.Item>
          <Form.Item name="enabled_modules" label="启用模块" rules={[{ required: true }]}>
            <Select mode="multiple" options={MODULES.map(m => ({ value: m, label: m }))} />
          </Form.Item>
          <Form.Item name="sync_interval_min" label="同步间隔(分钟)">
            <InputNumber min={1} max={1440} />
          </Form.Item>
          <Form.Item name="checkin_userids" label="打卡userid(逗号分隔,可选)"
            extra="无通讯录secret时用；有则优先自动拉">
            <Input.TextArea rows={2} placeholder="userA,userB" />
          </Form.Item>
          <Form.Item name="trusted_domain" label="可信域名（可选）"
            extra="反代后的对外域名，如 mcp.example.com；也可在列表「域名」里上传校验文件">
            <Input placeholder="mcp.example.com" />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={mcpModal.title}
        open={mcpModal.open}
        onCancel={() => setMcpModal({ open: false, title: '', text: '' })}
        width={640}
        footer={[
          <Button key="close" onClick={() => setMcpModal({ open: false, title: '', text: '' })}>关闭</Button>,
          <Button key="copy" type="primary" icon={<CopyOutlined />} onClick={() => copyText(mcpModal.text)}>
            复制 JSON
          </Button>,
        ]}
      >
        <Paragraph type="secondary" style={{ marginBottom: 8 }}>
          粘贴到 WorkBuddy / CodeBuddy 的 MCP 配置中。优先用租户可信域名，否则用当前访问域名。
        </Paragraph>
        <pre style={{
          background: '#f5f5f5',
          border: '1px solid #eee',
          borderRadius: 6,
          padding: 12,
          maxHeight: 360,
          overflow: 'auto',
          fontSize: 12,
          lineHeight: 1.5,
          margin: 0,
        }}>{mcpModal.text}</pre>
      </Modal>

      <Modal
        title={domainModal.tenant
          ? `可信域名 · ${domainModal.tenant.display_name || domainModal.tenant.tenant_id}`
          : '可信域名'}
        open={domainModal.open}
        onCancel={() => setDomainModal({ open: false, tenant: null, domain: '', fileList: [], uploading: false, info: null })}
        width={640}
        footer={[
          domainModal.info?.has_file
            ? <Button key="del" danger onClick={removeDomainVerify}>删除校验文件</Button>
            : null,
          <Button key="close" onClick={() => setDomainModal({ open: false, tenant: null, domain: '', fileList: [], uploading: false, info: null })}>
            关闭
          </Button>,
          <Button key="up" type="primary" icon={<UploadOutlined />} loading={domainModal.uploading} onClick={uploadDomainVerify}>
            上传并覆盖
          </Button>,
        ]}
      >
        <Paragraph type="secondary">
          企微「应用主页/可信域名」校验：把域名反代到本服务后，上传企微提供的校验文件。
          新上传会替换该租户旧文件；公网访问 <Text code>https://域名/文件名.txt</Text> 即可通过。
        </Paragraph>
        <Form layout="vertical">
          <Form.Item label="可信域名" extra="不要带 https://，例如 mcp.example.com">
            <Input
              value={domainModal.domain}
              onChange={(e) => setDomainModal((s) => ({ ...s, domain: e.target.value }))}
              placeholder="mcp.example.com"
            />
          </Form.Item>
          <Form.Item label="校验文件" extra="仅 .txt / .html，UTF-8 文本，≤64KB；同租户新文件覆盖旧文件">
            <Upload
              maxCount={1}
              beforeUpload={() => false}
              fileList={domainModal.fileList}
              onChange={({ fileList }) => setDomainModal((s) => ({ ...s, fileList }))}
              accept=".txt,.html,.htm,text/plain,text/html"
            >
              <Button icon={<UploadOutlined />}>选择文件</Button>
            </Upload>
          </Form.Item>
        </Form>
        {domainModal.info?.has_file && (
          <div style={{ background: '#f6ffed', border: '1px solid #b7eb8f', borderRadius: 6, padding: 12 }}>
            <div>当前文件：<Text code>{domainModal.info.verify_filename}</Text></div>
            <div style={{ marginTop: 4 }}>
              访问地址：
              {domainModal.info.verify_url
                ? <Link href={domainModal.info.verify_url.startsWith('http')
                  ? domainModal.info.verify_url
                  : `${window.location.origin}${domainModal.info.verify_url}`} target="_blank">
                    {domainModal.info.verify_url.startsWith('http')
                      ? domainModal.info.verify_url
                      : `${window.location.origin}${domainModal.info.verify_url}`}
                  </Link>
                : '—'}
            </div>
            <div style={{ marginTop: 8 }}>
              <Button size="small" icon={<CopyOutlined />} onClick={() => {
                const u = domainModal.info.verify_url?.startsWith('http')
                  ? domainModal.info.verify_url
                  : `${window.location.origin}${domainModal.info.verify_url || ''}`
                copyText(u)
              }}>复制访问 URL</Button>
            </div>
          </div>
        )}
      </Modal>
    </>
  )
}
