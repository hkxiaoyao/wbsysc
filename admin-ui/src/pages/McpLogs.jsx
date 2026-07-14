import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  DatePicker,
  Descriptions,
  Drawer,
  Dropdown,
  Empty,
  Form,
  Grid,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  ClockCircleOutlined,
  DeleteOutlined,
  DownOutlined,
  FilterOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import dayjs from 'dayjs'
import api from '../api.js'
import {
  buildDeleteSpec,
  buildLogQuery,
  formatDuration,
  normalizeLogKeyword,
  parseLogLocation,
  serializeLogFilters,
  statusMeta,
} from './mcpLogsView.js'
import './McpLogs.css'

const { RangePicker } = DatePicker
const { Paragraph, Text, Title } = Typography

const ALL_CLEAR_PHRASE = '清空全部日志'
const CATEGORY_LABELS = Object.freeze({
  tool: '工具调用',
  protocol: '协议访问',
  auth: '鉴权事件',
})
const DETAIL_FIELDS = Object.freeze([
  ['id', '日志 ID'],
  ['created_at', '发生时间'],
  ['tenant_id', '租户 ID'],
  ['category', '类别'],
  ['event_name', '事件'],
  ['target', '目标摘要'],
  ['params_summary', '参数摘要'],
  ['result_status', '结果状态'],
  ['error_code', '错误码'],
  ['error_summary', '错误摘要'],
  ['cost_ms', '耗时'],
  ['request_id', '请求 ID'],
  ['client_ip', '客户端 IP'],
  ['http_method', 'HTTP 方法'],
  ['http_status', 'HTTP 状态码'],
])

function errorMessage(error, fallback) {
  const detail = error?.response?.data?.detail
  return typeof detail === 'string' && detail ? detail : error?.message || fallback
}

function dateValue(value) {
  if (!value) return null
  const parsed = dayjs(value)
  return parsed.isValid() ? parsed : null
}

function formatTimestamp(value) {
  if (!value) return '—'
  if (typeof value === 'number') {
    const milliseconds = value < 1_000_000_000_000 ? value * 1000 : value
    return new Intl.DateTimeFormat('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }).format(new Date(milliseconds))
  }
  const text = String(value).trim().replace(' ', 'T')
  const zoned = /(?:Z|[+-]\d{2}:\d{2})$/i.test(text) ? text : `${text}Z`
  const timestamp = Date.parse(zoned)
  if (!Number.isFinite(timestamp)) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(timestamp))
}

function categoryTag(category) {
  return (
    <Tag className={`mcp-category mcp-category--${category || 'unknown'}`}>
      {CATEGORY_LABELS[category] || '未知类别'}
    </Tag>
  )
}

function statusTag(status) {
  const meta = statusMeta(status)
  return <Tag color={meta.color}>{meta.label}</Tag>
}

function AllClearField({ onValidityChange }) {
  const [value, setValue] = useState('')
  return (
    <div className="mcp-all-clear-field">
      <Paragraph>
        此操作会清理当前可见范围之外的全部日志。请输入
        <Text code>{ALL_CLEAR_PHRASE}</Text>
        继续。
      </Paragraph>
      <Input
        autoFocus
        value={value}
        aria-label="全部清空确认文字"
        placeholder={ALL_CLEAR_PHRASE}
        onChange={(event) => {
          const next = event.target.value
          setValue(next)
          onValidityChange(next === ALL_CLEAR_PHRASE)
        }}
      />
    </div>
  )
}

function MeterList({ items, labelKey = 'event_name' }) {
  const maximum = Math.max(1, ...items.map((item) => Number(item.count) || 0))
  if (!items.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据" />
  return (
    <div className="mcp-meter-list">
      {items.map((item) => {
        const count = Number(item.count) || 0
        const label = String(item[labelKey] || '未知')
        return (
          <div className="mcp-meter" key={label}>
            <div className="mcp-meter__label"><span>{label}</span><strong>{count}</strong></div>
            <div className="mcp-meter__track" aria-label={`${label} ${count} 次`}>
              <span style={{ width: `${Math.max(3, (count / maximum) * 100)}%` }} />
            </div>
          </div>
        )
      })}
    </div>
  )
}

function TrendPanel({ items }) {
  const maximum = Math.max(1, ...items.map((item) => Number(item.count) || 0))
  if (!items.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无趋势数据" />
  return (
    <div
      className="mcp-trend"
      role="region"
      tabIndex={0}
      aria-label={`调用量时间趋势，共 ${items.length} 个时间点，可横向滚动`}
    >
      {items.map((item, index) => {
        const count = Number(item.count) || 0
        return (
          <div className="mcp-trend__item" key={`${item.bucket}-${index}`} title={`${formatTimestamp(item.bucket)} · ${count} 次`}>
            <strong>{count}</strong>
            <span className="mcp-trend__bar" style={{ height: `${Math.max(8, (count / maximum) * 100)}%` }} />
            <small>{formatTimestamp(item.bucket).slice(0, 11)}</small>
          </div>
        )
      })}
    </div>
  )
}

function StatusDistribution({ items }) {
  const total = items.reduce((sum, item) => sum + (Number(item.count) || 0), 0)
  if (!items.length || !total) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无状态数据" />
  }
  return (
    <div className="mcp-status-distribution">
      <div className="mcp-status-distribution__bar" aria-label="状态分布">
        {items.map((item) => (
          <span
            className={`mcp-status-segment mcp-status-segment--${item.result_status}`}
            key={item.result_status}
            style={{ width: `${((Number(item.count) || 0) / total) * 100}%` }}
          />
        ))}
      </div>
      <div className="mcp-status-distribution__legend">
        {items.map((item) => (
          <div key={item.result_status}>
            <span className={`mcp-status-dot mcp-status-dot--${item.result_status}`} />
            <span>{statusMeta(item.result_status).label}</span>
            <strong>{Number(item.count) || 0}</strong>
          </div>
        ))}
      </div>
    </div>
  )
}

function StatsDashboard({ stats, loading, error, onRetry }) {
  const value = stats || {}
  return (
    <section className="mcp-dashboard" aria-label="租户调用概览">
      {error && (
        <Alert
          showIcon
          type="warning"
          message="统计数据暂时不可用"
          description={error}
          action={<Button size="small" onClick={onRetry}>重试统计</Button>}
        />
      )}
      <div className="mcp-stat-grid">
        <div className="mcp-stat-card mcp-stat-card--total">
          <Statistic title="调用总数" value={value.total || 0} loading={loading} />
          <span>当前筛选范围</span>
        </div>
        <div className="mcp-stat-card mcp-stat-card--success">
          <Statistic title="成功率" value={value.success_rate || 0} precision={2} suffix="%" loading={loading} />
          <span>状态为成功</span>
        </div>
        <div className="mcp-stat-card mcp-stat-card--error">
          <Statistic title="错误调用" value={value.error_count || 0} loading={loading} />
          <span>错误与拒绝</span>
        </div>
        <div className="mcp-stat-card mcp-stat-card--latency">
          <Statistic title="平均耗时" value={value.avg_cost_ms || 0} suffix="ms" loading={loading} />
          <span>P95 {formatDuration(value.p95_cost_ms)}</span>
        </div>
      </div>
      <div className="mcp-chart-grid">
        <article className="mcp-chart-panel mcp-chart-panel--trend">
          <header><div><Text strong>调用趋势</Text><small>按小时聚合</small></div></header>
          <TrendPanel items={value.trend || []} />
        </article>
        <article className="mcp-chart-panel">
          <header><div><Text strong>工具排行</Text><small>前 10 个工具事件</small></div></header>
          <MeterList items={value.top_tools || []} />
        </article>
        <article className="mcp-chart-panel">
          <header><div><Text strong>状态分布</Text><small>结果状态构成</small></div></header>
          <StatusDistribution items={value.status_distribution || []} />
        </article>
      </div>
    </section>
  )
}

export default function McpLogs({ filters, onFiltersChange = () => {} }) {
  const fallbackFilters = useMemo(() => parseLogLocation(''), [])
  const activeFilters = filters || fallbackFilters
  const [items, setItems] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [logsLoading, setLogsLoading] = useState(false)
  const [statsLoading, setStatsLoading] = useState(false)
  const [logsError, setLogsError] = useState('')
  const [statsError, setStatsError] = useState('')
  const [stats, setStats] = useState(null)
  const [selectedRowKeys, setSelectedRowKeys] = useState([])
  const [detailRecord, setDetailRecord] = useState(null)
  const [moreFiltersOpen, setMoreFiltersOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsLoading, setSettingsLoading] = useState(false)
  const [settingsSaving, setSettingsSaving] = useState(false)
  const [settingsError, setSettingsError] = useState('')
  const [retentionDays, setRetentionDays] = useState(90)
  const [beforeOpen, setBeforeOpen] = useState(false)
  const [beforeDate, setBeforeDate] = useState(null)
  const [cleanupLoading, setCleanupLoading] = useState(false)
  const [tenantOptions, setTenantOptions] = useState([])
  const [keywordDraft, setKeywordDraft] = useState(normalizeLogKeyword(activeFilters.keyword))
  const [reloadNonce, setReloadNonce] = useState(0)
  const [moreForm] = Form.useForm()
  const [modal, modalContextHolder] = Modal.useModal()
  const [messageApi, messageContextHolder] = message.useMessage()
  const screens = Grid.useBreakpoint()
  const compactTable = !screens.md
  const listRequest = useRef(0)
  const statsRequest = useRef(0)
  const filterSignature = useMemo(() => serializeLogFilters(activeFilters), [activeFilters])

  const rangePresets = useMemo(() => {
    return [
      { label: '最近 1 小时', value: () => [dayjs().subtract(1, 'hour'), dayjs()] },
      { label: '最近 24 小时', value: () => [dayjs().subtract(24, 'hour'), dayjs()] },
      { label: '最近 7 天', value: () => [dayjs().subtract(7, 'day'), dayjs()] },
      { label: '最近 30 天', value: () => [dayjs().subtract(30, 'day'), dayjs()] },
      { label: '最近 90 天', value: () => [dayjs().subtract(90, 'day'), dayjs()] },
    ]
  }, [])

  const reloadData = useCallback(() => setReloadNonce((value) => value + 1), [])

  const updateFilters = useCallback((patch) => {
    const next = { ...activeFilters, ...patch }
    setPage(1)
    setSelectedRowKeys([])
    onFiltersChange(next)
  }, [activeFilters, onFiltersChange])

  useLayoutEffect(() => {
    listRequest.current += 1
    statsRequest.current += 1
    setItems([])
    setTotal(0)
    setStats(null)
    setDetailRecord(null)
    setLogsError('')
    setStatsError('')
    setKeywordDraft(normalizeLogKeyword(activeFilters.keyword))
    setPage(1)
    setSelectedRowKeys([])
  }, [filterSignature])

  useEffect(() => {
    const requestId = ++listRequest.current
    const controller = new AbortController()
    setLogsLoading(true)
    setLogsError('')
    api.get('/admin/mcp-logs', {
      params: buildLogQuery(activeFilters, page, pageSize),
      signal: controller.signal,
    }).then((response) => {
      if (requestId !== listRequest.current) return
      setItems(response.data?.items || [])
      setTotal(Number(response.data?.total) || 0)
    }).catch((error) => {
      if (controller.signal.aborted || requestId !== listRequest.current) return
      setLogsError(errorMessage(error, '日志列表加载失败'))
    }).finally(() => {
      if (requestId === listRequest.current) setLogsLoading(false)
    })
    return () => controller.abort()
  }, [filterSignature, page, pageSize, reloadNonce])

  useEffect(() => {
    if (!activeFilters.tenantId) {
      statsRequest.current += 1
      setStats(null)
      setStatsError('')
      setStatsLoading(false)
      return undefined
    }
    const requestId = ++statsRequest.current
    const controller = new AbortController()
    const query = buildLogQuery(activeFilters, 1, 20)
    delete query.page
    delete query.page_size
    setStatsLoading(true)
    setStatsError('')
    api.get('/admin/mcp-logs/stats', { params: query, signal: controller.signal })
      .then((response) => {
        if (requestId === statsRequest.current) setStats(response.data || {})
      })
      .catch((error) => {
        if (controller.signal.aborted || requestId !== statsRequest.current) return
        setStatsError(errorMessage(error, '统计数据加载失败'))
      })
      .finally(() => {
        if (requestId === statsRequest.current) setStatsLoading(false)
      })
    return () => controller.abort()
  }, [filterSignature, reloadNonce])

  useEffect(() => {
    const controller = new AbortController()
    api.get('/admin/tenants', { signal: controller.signal })
      .then((response) => {
        const options = (response.data?.items || []).map((tenant) => ({
          value: tenant.tenant_id,
          label: tenant.display_name
            ? `${tenant.display_name} · ${tenant.tenant_id}`
            : tenant.tenant_id,
        }))
        setTenantOptions(options)
      })
      .catch(() => {})
    return () => controller.abort()
  }, [])

  const openMoreFilters = () => {
    moreForm.setFieldsValue({
      requestId: activeFilters.requestId || undefined,
      clientIp: activeFilters.clientIp || undefined,
      eventName: activeFilters.eventName || undefined,
      costMin: activeFilters.costMin === '' ? undefined : activeFilters.costMin,
      costMax: activeFilters.costMax === '' ? undefined : activeFilters.costMax,
    })
    setMoreFiltersOpen(true)
  }

  const applyMoreFilters = async () => {
    try {
      const values = await moreForm.validateFields()
      if (
        values.costMin !== undefined
        && values.costMax !== undefined
        && Number(values.costMin) > Number(values.costMax)
      ) {
        messageApi.warning('最小耗时不能大于最大耗时')
        return
      }
      updateFilters({
        requestId: values.requestId?.trim() || '',
        clientIp: values.clientIp?.trim() || '',
        eventName: values.eventName?.trim() || '',
        costMin: values.costMin ?? '',
        costMax: values.costMax ?? '',
      })
      setMoreFiltersOpen(false)
    } catch (error) {
      if (!error?.errorFields) messageApi.error('筛选条件校验失败')
    }
  }

  const clearMoreFilters = () => {
    moreForm.resetFields()
    updateFilters({ requestId: '', clientIp: '', eventName: '', costMin: '', costMax: '' })
    setMoreFiltersOpen(false)
  }

  const loadSettings = async () => {
    setSettingsOpen(true)
    setSettingsLoading(true)
    setSettingsError('')
    try {
      const response = await api.get('/admin/mcp-log-settings')
      setRetentionDays(Number(response.data?.retention_days ?? 90))
    } catch (error) {
      setSettingsError(errorMessage(error, '保留策略加载失败'))
    } finally {
      setSettingsLoading(false)
    }
  }

  const saveSettings = async () => {
    if (!Number.isInteger(retentionDays) || retentionDays < 0 || retentionDays > 3650) {
      messageApi.warning('保留天数必须是 0–3650 的整数')
      return
    }
    setSettingsSaving(true)
    try {
      const response = await api.put('/admin/mcp-log-settings', { retention_days: retentionDays })
      setRetentionDays(Number(response.data?.retention_days ?? retentionDays))
      messageApi.success('日志保留策略已保存')
      setSettingsOpen(false)
    } catch (error) {
      messageApi.error(errorMessage(error, '保留策略保存失败'))
    } finally {
      setSettingsSaving(false)
    }
  }

  const executeDelete = async (spec, preview) => {
    setCleanupLoading(true)
    try {
      const response = await api.delete('/admin/mcp-logs', {
        data: { ...spec, confirm_token: preview.confirm_token },
      })
      setSelectedRowKeys([])
      setPage(1)
      messageApi.success(`已清理 ${Number(response.data?.deleted) || 0} 条日志`)
      reloadData()
    } catch (error) {
      messageApi.error(errorMessage(error, '日志清理失败'))
      throw error
    } finally {
      setCleanupLoading(false)
    }
  }

  const showDeleteConfirmation = (mode, spec, preview) => {
    const matched = Number(preview.matched_count) || 0
    if (!matched) {
      modal.info({ title: '没有可清理的日志', content: '预览结果为 0 条，未执行任何操作。' })
      return
    }
    const labels = {
      ids: '清理所选日志',
      filter: '按当前筛选清理',
      before_date: '按日期阈值清理',
      all: '清空全部日志',
    }
    let instance
    const expiry = preview.expires_at ? formatTimestamp(preview.expires_at) : '5 分钟内'
    instance = modal.confirm({
      title: labels[mode],
      icon: <SafetyCertificateOutlined />,
      width: 520,
      content: (
        <div className="mcp-delete-preview">
          <div><span>预计影响</span><strong>{matched} 条</strong></div>
          <div><span>预览凭证</span><strong>{expiry}有效</strong></div>
          {mode === 'all' && (
            <AllClearField
              onValidityChange={(valid) => instance?.update({
                okButtonProps: { danger: true, disabled: !valid },
              })}
            />
          )}
        </div>
      ),
      okText: labels[mode],
      cancelText: '取消',
      okButtonProps: { danger: true, disabled: mode === 'all' },
      onOk: () => executeDelete(spec, preview),
    })
  }

  const requestDelete = async (mode, threshold = null) => {
    let spec
    try {
      spec = buildDeleteSpec(mode, activeFilters, selectedRowKeys, threshold)
    } catch (error) {
      messageApi.warning(error.message || '清理条件无效')
      return
    }
    setCleanupLoading(true)
    try {
      const response = await api.post('/admin/mcp-logs/delete-preview', spec)
      showDeleteConfirmation(mode, spec, response.data || {})
    } catch (error) {
      messageApi.error(errorMessage(error, '清理预览失败'))
    } finally {
      setCleanupLoading(false)
    }
  }

  const moreFilterCount = [
    activeFilters.requestId,
    activeFilters.clientIp,
    activeFilters.eventName,
    activeFilters.costMin,
    activeFilters.costMax,
  ].filter((value) => value !== '' && value !== null && value !== undefined).length

  const activeTenantLabel = tenantOptions.find((option) => option.value === activeFilters.tenantId)?.label
    || activeFilters.tenantId
    || '全部租户'

  const summaryTags = [
    activeFilters.tenantId && `租户：${activeTenantLabel}`,
    activeFilters.category && `类别：${CATEGORY_LABELS[activeFilters.category]}`,
    activeFilters.status && `状态：${statusMeta(activeFilters.status).label}`,
    activeFilters.keyword && `关键词：${activeFilters.keyword}`,
    activeFilters.requestId && `请求 ID：${activeFilters.requestId}`,
    activeFilters.clientIp && `IP：${activeFilters.clientIp}`,
    activeFilters.eventName && `事件：${activeFilters.eventName}`,
    activeFilters.costMin !== '' && `耗时 ≥ ${activeFilters.costMin} ms`,
    activeFilters.costMax !== '' && `耗时 ≤ ${activeFilters.costMax} ms`,
  ].filter(Boolean)

  const columns = [
    {
      title: '时间', dataIndex: 'created_at', key: 'created_at', width: 154,
      render: (value) => <Text className="mcp-time">{formatTimestamp(value)}</Text>,
    },
    {
      title: '租户', dataIndex: 'tenant_id', key: 'tenant_id', width: 150, responsive: ['md'],
      render: (value) => <Text ellipsis={{ tooltip: value }}>{value || '未识别租户'}</Text>,
    },
    {
      title: '类别', dataIndex: 'category', key: 'category', width: 100, responsive: ['sm'],
      render: categoryTag,
    },
    {
      title: '事件', dataIndex: 'event_name', key: 'event_name', width: 210, rowScope: 'row',
      render: (value, record) => (
        <div className="mcp-event-cell">
          <Text strong ellipsis={{ tooltip: value }}>{value || '未命名事件'}</Text>
          {!compactTable && <Text type="secondary">#{record.id}</Text>}
        </div>
      ),
    },
    {
      title: '状态', dataIndex: 'result_status', key: 'result_status', width: 92,
      render: statusTag,
    },
    {
      title: '耗时', dataIndex: 'cost_ms', key: 'cost_ms', width: 100, responsive: ['sm'],
      render: (value) => <Text className="mcp-duration">{formatDuration(value)}</Text>,
    },
    {
      title: '客户端 IP', dataIndex: 'client_ip', key: 'client_ip', width: 145, responsive: ['lg'],
      render: (value) => <Text code>{value || '—'}</Text>,
    },
    {
      title: '摘要', key: 'summary', width: 260, responsive: ['xl'],
      render: (_, record) => (
        <Text ellipsis={{ tooltip: record.target || record.params_summary || record.error_summary }}>
          {record.target || record.params_summary || record.error_summary || '—'}
        </Text>
      ),
    },
  ]

  const dangerMenu = {
    items: [
      { key: 'filter', label: '按当前筛选清理', icon: <FilterOutlined />, danger: true },
      { key: 'before_date', label: '清理指定日期之前', icon: <ClockCircleOutlined />, danger: true },
      { type: 'divider' },
      { key: 'all', label: '清空全部日志', icon: <DeleteOutlined />, danger: true },
    ],
    onClick: ({ key }) => {
      if (key === 'filter') requestDelete('filter')
      if (key === 'before_date') {
        setBeforeDate(dayjs().subtract(90, 'day'))
        setBeforeOpen(true)
      }
      if (key === 'all') requestDelete('all')
    },
  }

  return (
    <main className={`mcp-log-workbench ${activeFilters.tenantId ? 'mcp-log-workbench--tenant' : ''}`}>
      {modalContextHolder}
      {messageContextHolder}

      <header className="mcp-log-heading">
        <div>
          <Text className="mcp-log-eyebrow">MCP TELEMETRY / OPERATIONS</Text>
          <Title level={1}>调用日志</Title>
          <Paragraph>检索业务、协议与鉴权事件，定位异常并执行可预览的安全清理。</Paragraph>
        </div>
        <Space wrap className="mcp-log-heading__actions">
          <Button icon={<SettingOutlined />} onClick={loadSettings}>日志设置</Button>
          <Button icon={<ReloadOutlined />} loading={logsLoading} onClick={reloadData}>刷新</Button>
          <Dropdown menu={dangerMenu} trigger={['click']}>
            <Button danger loading={cleanupLoading}>危险操作 <DownOutlined /></Button>
          </Dropdown>
        </Space>
      </header>

      <section className="mcp-scope-rail" aria-label="当前日志视角">
        <div className="mcp-scope-rail__signal"><span /><span /><span /></div>
        <div>
          <Text>{activeFilters.tenantId ? '租户视角' : '全局视角'}</Text>
          <strong>{activeTenantLabel}</strong>
        </div>
        <div className="mcp-scope-rail__range">
          <Text>时间窗</Text>
          <strong>{formatTimestamp(activeFilters.from)} — {formatTimestamp(activeFilters.to)}</strong>
        </div>
      </section>

      {activeFilters.tenantId && (
        <StatsDashboard stats={stats} loading={statsLoading} error={statsError} onRetry={reloadData} />
      )}

      <section className="mcp-log-panel">
        <div className="mcp-filter-bar">
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            aria-label="按租户筛选"
            placeholder="全部租户"
            value={activeFilters.tenantId || undefined}
            options={tenantOptions}
            onChange={(tenantId) => updateFilters({ tenantId: tenantId || '' })}
          />
          <Select
            allowClear
            aria-label="按日志类别筛选"
            placeholder="全部类别"
            value={activeFilters.category || undefined}
            options={Object.entries(CATEGORY_LABELS).map(([value, label]) => ({ value, label }))}
            onChange={(category) => updateFilters({ category: category || '' })}
          />
          <Select
            allowClear
            aria-label="按结果状态筛选"
            placeholder="全部状态"
            value={activeFilters.status || undefined}
            options={['ok', 'partial', 'error', 'denied'].map((value) => ({
              value,
              label: statusMeta(value).label,
            }))}
            onChange={(status) => updateFilters({ status: status || '' })}
          />
          <RangePicker
            allowClear={false}
            showTime
            presets={rangePresets}
            aria-label="日志时间范围"
            value={[dateValue(activeFilters.from), dateValue(activeFilters.to)]}
            onChange={(range) => {
              if (range?.[0] && range?.[1]) {
                updateFilters({ from: range[0].toISOString(), to: range[1].toISOString() })
              }
            }}
          />
          <Input.Search
            allowClear
            enterButton={<SearchOutlined />}
            maxLength={100}
            aria-label="日志关键词"
            placeholder="搜索事件、目标或安全摘要"
            value={keywordDraft}
            onChange={(event) => setKeywordDraft(event.target.value)}
            onSearch={(keyword) => updateFilters({ keyword: normalizeLogKeyword(keyword) })}
          />
          <Button icon={<FilterOutlined />} onClick={openMoreFilters}>
            更多筛选{moreFilterCount ? ` · ${moreFilterCount}` : ''}
          </Button>
        </div>

        <div className="mcp-filter-summary" aria-label="当前筛选摘要">
          <Text type="secondary">筛选摘要</Text>
          <Tag icon={<ClockCircleOutlined />}>
            {formatTimestamp(activeFilters.from)} — {formatTimestamp(activeFilters.to)}
          </Tag>
          {summaryTags.map((label) => <Tag key={label}>{label}</Tag>)}
        </div>

        {selectedRowKeys.length > 0 && (
          <div className="mcp-selection-bar">
            <div><strong>已选择 {selectedRowKeys.length} 条</strong><span>仅会清理这些日志 ID</span></div>
            <Space>
              <Button size="small" onClick={() => setSelectedRowKeys([])}>取消选择</Button>
              <Button
                size="small"
                danger
                type="primary"
                icon={<DeleteOutlined />}
                loading={cleanupLoading}
                onClick={() => requestDelete('ids')}
              >清理所选</Button>
            </Space>
          </div>
        )}

        {logsError && (
          <Alert
            showIcon
            type="error"
            message="日志列表加载失败"
            description={logsError}
            action={<Button size="small" onClick={reloadData}>重试</Button>}
          />
        )}

        <Table
          rowKey="id"
          size="middle"
          columns={columns}
          dataSource={items}
          loading={logsLoading}
          rowSelection={{
            selectedRowKeys,
            preserveSelectedRowKeys: true,
            onChange: setSelectedRowKeys,
          }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: [20, 50, 100],
            responsive: true,
            showTotal: (count, range) => `${range[0]}–${range[1]} / 共 ${count} 条`,
            onChange: (nextPage, nextPageSize) => {
              setPageSize(Math.min(100, nextPageSize))
              setPage(nextPageSize === pageSize ? nextPage : 1)
            },
          }}
          scroll={{ x: compactTable ? 520 : 1120 }}
          onRow={(record) => ({
            tabIndex: 0,
            'aria-label': `查看日志 #${record.id} ${record.event_name || ''}`,
            className: 'mcp-log-row',
            onClick: (event) => {
              if (event.target.closest('button, a, input, .ant-checkbox-wrapper')) return
              setDetailRecord(record)
            },
            onKeyDown: (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                setDetailRecord(record)
              }
            },
          })}
          locale={{
            emptyText: logsLoading ? null : (
              <Empty description={logsError ? '暂时无法显示日志' : '当前筛选范围内暂无日志'}>
                {!logsError && <Button icon={<ReloadOutlined />} onClick={reloadData}>重新加载</Button>}
              </Empty>
            ),
          }}
        />
      </section>

      <Drawer
        rootClassName="mcp-log-drawer"
        title={detailRecord ? `日志详情 · #${detailRecord.id}` : '日志详情'}
        open={Boolean(detailRecord)}
        onClose={() => setDetailRecord(null)}
        width={screens.sm ? 560 : 'calc(100vw - 12px)'}
        destroyOnHidden={false}
        extra={detailRecord && statusTag(detailRecord.result_status)}
      >
        {detailRecord && (
          <Descriptions bordered column={1} size="small">
            {DETAIL_FIELDS.map(([field, label]) => {
              let content = detailRecord[field]
              if (field === 'created_at') content = formatTimestamp(content)
              if (field === 'category') content = categoryTag(content)
              if (field === 'result_status') content = statusTag(content)
              if (field === 'cost_ms') content = formatDuration(content)
              if (content === '' || content === null || content === undefined) content = '—'
              return <Descriptions.Item key={field} label={label}>{content}</Descriptions.Item>
            })}
          </Descriptions>
        )}
      </Drawer>

      <Drawer
        rootClassName="mcp-log-drawer"
        title="更多筛选"
        open={moreFiltersOpen}
        onClose={() => setMoreFiltersOpen(false)}
        width={screens.sm ? 460 : 'calc(100vw - 12px)'}
        destroyOnHidden={false}
        footer={(
          <div className="mcp-drawer-footer">
            <Button onClick={clearMoreFilters}>清空更多筛选</Button>
            <Button type="primary" onClick={applyMoreFilters}>应用筛选</Button>
          </div>
        )}
      >
        <Paragraph type="secondary">所有条件均为结构化字段，不支持表达式或 SQL。</Paragraph>
        <Form form={moreForm} layout="vertical">
          <Form.Item name="requestId" label="请求 ID" rules={[{ max: 64, message: '最多 64 个字符' }]}>
            <Input placeholder="内部请求或安全会话标识" />
          </Form.Item>
          <Form.Item name="clientIp" label="客户端 IP" rules={[{ max: 64, message: '最多 64 个字符' }]}>
            <Input placeholder="如 203.0.113.8" />
          </Form.Item>
          <Form.Item name="eventName" label="工具或事件" rules={[{ max: 96, message: '最多 96 个字符' }]}>
            <Input placeholder="如 wecom_list_reports" />
          </Form.Item>
          <div className="mcp-cost-grid">
            <Form.Item name="costMin" label="最小耗时（ms）">
              <InputNumber min={0} precision={0} placeholder="0" />
            </Form.Item>
            <Form.Item name="costMax" label="最大耗时（ms）">
              <InputNumber min={0} precision={0} placeholder="不限" />
            </Form.Item>
          </div>
        </Form>
      </Drawer>

      <Drawer
        rootClassName="mcp-log-drawer"
        title="日志保留设置"
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        width={screens.sm ? 420 : 'calc(100vw - 12px)'}
        destroyOnHidden={false}
        loading={settingsLoading}
        footer={(
          <div className="mcp-drawer-footer">
            <Button onClick={() => setSettingsOpen(false)}>取消</Button>
            <Button type="primary" loading={settingsSaving} onClick={saveSettings}>保存设置</Button>
          </div>
        )}
      >
        {settingsError && <Alert showIcon type="error" message={settingsError} />}
        <div className="mcp-retention-setting">
          <Text strong>自动保留天数</Text>
          <InputNumber
            min={0}
            max={3650}
            precision={0}
            value={retentionDays}
            aria-label="自动保留天数"
            onChange={(value) => setRetentionDays(value)}
          />
          <Paragraph type="secondary">
            支持 0–3650 天。设置为 <Text code>0</Text> 时关闭自动清理；手动清理仍然可用。
          </Paragraph>
        </div>
      </Drawer>

      <Modal
        title="清理日期之前的日志"
        open={beforeOpen}
        okText="预览影响范围"
        cancelText="取消"
        onCancel={() => setBeforeOpen(false)}
        onOk={() => {
          if (!beforeDate) {
            messageApi.warning('请选择日期阈值')
            return
          }
          setBeforeOpen(false)
          requestDelete('before_date', beforeDate.toISOString())
        }}
      >
        <Paragraph type="secondary">先选择阈值，下一步会展示预计影响条数并要求再次确认。</Paragraph>
        <DatePicker
          showTime
          value={beforeDate}
          aria-label="清理日期阈值"
          onChange={setBeforeDate}
          style={{ width: '100%' }}
        />
      </Modal>
    </main>
  )
}
