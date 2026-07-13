# Tenant Management Page Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing tenant management screen into a focused operations workbench with fast scanning, local filtering, compact actions, and a safe right-side editor drawer without changing backend behavior.

**Architecture:** Keep all existing API calls and auxiliary MCP/domain dialogs in `Tenants.jsx`, extract only deterministic list derivation into a small ESM helper module, and cover those helpers with Node's built-in test runner. Add one page-scoped stylesheet for the status rail, data-mode row signature, drawer sections, and responsive behavior.

**Tech Stack:** React 18.3, Ant Design 5.29.3 (resolved lockfile version), Vite 5.4, native CSS, Node `node:test`.

## Global Constraints

- Only the tenant management page may change; do not modify the login page, global header, backend API, database schema, synchronization semantics, or authentication.
- Preserve the existing request paths, payload fields, validation rules, MCP configuration dialog, trusted-domain dialog, delete confirmation, force-sync confirmation, and diagnostic result dialog.
- Use `#17323D` for primary ink, `#167D95` for stored mode, `#7356A3` for direct mode, `#EEF3F5` for the page canvas, and `#2C8C68` for running state.
- The signature element is a slim left data-mode rail on every table row; avoid generic floating statistic cards and decorative gradients.
- Desktop uses a table with a fixed action column; narrow screens keep the table with secondary columns hidden and horizontal scrolling enabled.
- Direct-mode tenants must keep sync, full rollback, and diagnosis unavailable, with the reason visible to the user.
- Do not add runtime dependencies.
- Run deterministic tests, Vite production build, Ant Design lint, desktop visual review, narrow-screen visual review, and interaction smoke tests before completion.

---

## File Map

| File | Responsibility |
| --- | --- |
| `admin-ui/src/pages/tenantsView.js` | Pure tenant statistics, filtering, and direct-mode action rules |
| `admin-ui/src/pages/tenantsView.test.js` | Deterministic coverage for the view helpers using `node:test` |
| `admin-ui/src/pages/Tenants.jsx` | Existing API workflows plus the workbench, table, menus, and editor drawer |
| `admin-ui/src/pages/Tenants.css` | Page-scoped visual system, mode rail, drawer sections, and breakpoints |

## Pre-implementation Gate

- [x] Run the required Gemini and Claude analyses in parallel from the active worktree, asking both reviewers to inspect the approved specification, this plan, and the current `Tenants.jsx` for correctness, accessibility, regression risks, and Ant Design 5.29.3 compatibility. Gemini was attempted but unavailable because `GEMINI_API_KEY` is not configured; Claude completed the analysis.
- [x] Merge non-conflicting findings into the implementation notes; reject suggestions that change backend contracts or the approved page scope.
- [x] Update `.ccg/tasks/admin-tenant-page-polish/task.json` to `currentPhase: "implementation"` and `nextAction: "实现租户管理工作台"`.

---

### Task 1: Deterministic tenant view model

**Files:**
- Create: `admin-ui/src/pages/tenantsView.test.js`
- Create: `admin-ui/src/pages/tenantsView.js`

**Interfaces:**
- Consumes: Tenant rows returned by `GET /admin/tenants` with `tenant_id`, `display_name`, `corpid`, `data_mode`, `enabled`, and `has_secret`.
- Produces: `EMPTY_FILTERS`, `getTenantStats(items)`, `filterTenants(items, filters)`, and `getDirectModeReason(row)` for `Tenants.jsx`.

- [x] **Step 1: Write the failing helper tests**

Create `admin-ui/src/pages/tenantsView.test.js` with this exact test coverage:

```js
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  EMPTY_FILTERS,
  filterTenants,
  getDirectModeReason,
  getTenantStats,
} from './tenantsView.js'

const tenants = [
  {
    tenant_id: 'alpha',
    display_name: '北区门店',
    corpid: 'wwAlpha',
    data_mode: 'stored',
    enabled: true,
    has_secret: true,
  },
  {
    tenant_id: 'beta',
    display_name: '华南直连',
    corpid: 'wwBeta',
    data_mode: 'direct',
    enabled: true,
    has_secret: false,
  },
  {
    tenant_id: 'gamma',
    display_name: '停用租户',
    corpid: 'wwGamma',
    data_mode: 'stored',
    enabled: false,
    has_secret: true,
  },
]

test('getTenantStats derives stable full-list counts', () => {
  assert.deepEqual(getTenantStats(tenants), {
    total: 3,
    running: 2,
    direct: 1,
    attention: 2,
  })
})

test('filterTenants searches name, tenant id, and CorpID case-insensitively', () => {
  assert.deepEqual(
    filterTenants(tenants, { ...EMPTY_FILTERS, query: 'WWBETA' }).map((row) => row.tenant_id),
    ['beta'],
  )
  assert.deepEqual(
    filterTenants(tenants, { ...EMPTY_FILTERS, query: '北区' }).map((row) => row.tenant_id),
    ['alpha'],
  )
})

test('filterTenants combines mode and enabled filters', () => {
  assert.deepEqual(
    filterTenants(tenants, { query: '', dataMode: 'stored', enabled: 'disabled' })
      .map((row) => row.tenant_id),
    ['gamma'],
  )
})

test('getDirectModeReason explains unavailable synchronization actions', () => {
  assert.equal(getDirectModeReason(tenants[1]), '直连模式实时调用企微 API，无需同步')
  assert.equal(getDirectModeReason(tenants[0]), '')
})
```

- [x] **Step 2: Run the test and verify the missing module failure**

Run:

```powershell
cd D:\app\wbsysc\admin-ui
node --test src/pages/tenantsView.test.js
```

Expected: exit code `1` with `ERR_MODULE_NOT_FOUND` for `tenantsView.js`.

- [x] **Step 3: Implement the pure view helpers**

Create `admin-ui/src/pages/tenantsView.js`:

```js
export const EMPTY_FILTERS = Object.freeze({
  query: '',
  dataMode: 'all',
  enabled: 'all',
})

const normalized = (value) => String(value ?? '').trim().toLocaleLowerCase()

export function getTenantStats(items = []) {
  return items.reduce((stats, row) => {
    stats.total += 1
    if (row.enabled) stats.running += 1
    if (row.data_mode === 'direct') stats.direct += 1
    if (!row.enabled || !row.has_secret) stats.attention += 1
    return stats
  }, { total: 0, running: 0, direct: 0, attention: 0 })
}

export function filterTenants(items = [], filters = EMPTY_FILTERS) {
  const query = normalized(filters.query)
  return items.filter((row) => {
    const matchesQuery = !query || [row.display_name, row.tenant_id, row.corpid]
      .some((value) => normalized(value).includes(query))
    const matchesMode = filters.dataMode === 'all' || row.data_mode === filters.dataMode
    const matchesEnabled = filters.enabled === 'all'
      || (filters.enabled === 'enabled' ? Boolean(row.enabled) : !row.enabled)
    return matchesQuery && matchesMode && matchesEnabled
  })
}

export function getDirectModeReason(row) {
  return row?.data_mode === 'direct' ? '直连模式实时调用企微 API，无需同步' : ''
}
```

- [x] **Step 4: Run the helper tests**

Run:

```powershell
node --test src/pages/tenantsView.test.js
```

Expected: `4` tests pass, `0` fail.

- [x] **Step 5: Commit the helper boundary**

```powershell
git add admin-ui/src/pages/tenantsView.js admin-ui/src/pages/tenantsView.test.js
git commit -m "test: cover tenant workbench view model"
```

---

### Task 2: Tenant workbench and compact action table

**Files:**
- Modify: `admin-ui/src/pages/Tenants.jsx`

**Interfaces:**
- Consumes: All exports from `./tenantsView.js`; existing `api` client and unchanged tenant endpoints.
- Produces: Full-list status statistics, filtered table rows, compact row actions, distinct initial/filter/error states, and per-tenant action loading feedback.

- [x] **Step 1: Add imports and workbench state**

Update React and Ant Design imports to include `useMemo`, `Alert`, `Badge`, `Dropdown`, `Empty`, and `Tooltip`. Add `DownOutlined` and `SearchOutlined` to the icon import. Import the helper interface; the page stylesheet is added with Task 4:

```js
import { useEffect, useMemo, useState } from 'react'
import {
  Alert, Badge, Button, Drawer, Dropdown, Empty, Form, Input, InputNumber,
  Modal, Select, Space, Switch, Table, Tag, Tooltip, Typography, Upload,
} from 'antd'
import {
  CopyOutlined, DeleteOutlined, DownOutlined, EditOutlined, GlobalOutlined,
  MoreOutlined, PlusOutlined, ReloadOutlined, SearchOutlined, ThunderboltOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import api from '../api.js'
import { EMPTY_FILTERS, filterTenants, getDirectModeReason, getTenantStats } from './tenantsView.js'
```

Inside `Tenants`, add these state and derived values:

```js
const [loadError, setLoadError] = useState('')
const [filters, setFilters] = useState({ ...EMPTY_FILTERS })
const [rowActions, setRowActions] = useState(() => new Set())

const stats = useMemo(() => getTenantStats(data), [data])
const visibleTenants = useMemo(() => filterTenants(data, filters), [data, filters])
const hasFilters = filters.query || filters.dataMode !== 'all' || filters.enabled !== 'all'
```

- [x] **Step 2: Make list loading recoverable without discarding safe data**

Replace `load` with:

```js
const load = async () => {
  setLoading(true)
  setLoadError('')
  try {
    const response = await api.get('/admin/tenants')
    setData(response.data.items || [])
  } catch (error) {
    const detail = error.response?.data?.detail || error.message
    setLoadError(`租户列表加载失败：${detail}`)
  } finally {
    setLoading(false)
  }
}
```

Do not clear `data` in the error path, so a failed refresh keeps the last safe list visible.

- [x] **Step 3: Isolate row-level operation state**

Wrap the existing sync and diagnosis request bodies with these helpers:

```js
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
```

In `syncNow`, save `const action = opts.reset_cursor ? 'force-sync' : 'sync'`, call `beginRowAction(row, action)` before the request, and `endRowAction(row, action)` in `finally`. In `diagnoseSync`, use action key `diagnose` with the same row/action arguments. Keep the existing URLs, parameters, messages, and result content unchanged.

- [x] **Step 4: Replace the wide operation group with configuration plus a dropdown**

Add a menu factory next to the column definition:

```jsx
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
```

Replace `columns` with six purposeful columns:

```jsx
const columns = [
  {
    title: '租户', key: 'tenant', width: 220, rowScope: 'row',
    render: (_, row) => (
      <div className="tenant-identity">
        <Text strong>{row.display_name || row.tenant_id}</Text>
        <Tooltip title={row.tenant_id}><Text className="tenant-code" copyable>{row.tenant_id}</Text></Tooltip>
      </div>
    ),
  },
  {
    title: '企业信息', key: 'company', width: 220, responsive: ['md'],
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
    render: (mode) => <Tag className={`mode-tag mode-tag--${mode}`}>{mode === 'direct' ? '企微直连' : 'MySQL 存储'}</Tag>,
  },
  {
    title: '同步策略', key: 'policy', width: 250, responsive: ['lg'],
    render: (_, row) => (
      <div className="tenant-policy">
        <Text>{row.data_mode === 'direct' ? '实时调用企微 API' : `每 ${row.sync_interval_min || 30} 分钟同步`}</Text>
        <Text type="secondary">{(row.enabled_modules || '').split(',').filter(Boolean).join(' · ') || '未启用模块'}</Text>
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
            icon={<MoreOutlined />}
            loading={isRowBusy(row) || mcpLoadingTenant === row.tenant_id}
          >更多 <DownOutlined /></Button>
        </Dropdown>
      </Space>
    ),
  },
]
```

- [x] **Step 5: Build the status rail, filters, and explicit table states**

Replace the current top `Space` and `Table` with this structure:

```jsx
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
        <span>{label}</span><strong>{value}</strong>
      </div>
    ))}
  </section>

  <section className="tenant-panel">
    <div className="tenant-toolbar">
      <Input
        allowClear aria-label="搜索租户" prefix={<SearchOutlined />} value={filters.query}
        placeholder="搜索名称、租户 ID 或 CorpID"
        onChange={(event) => setFilters((current) => ({ ...current, query: event.target.value }))}
      />
      <Select
        value={filters.dataMode} aria-label="按数据模式筛选"
        onChange={(dataMode) => setFilters((current) => ({ ...current, dataMode }))}
        options={[{ value: 'all', label: '全部模式' }, { value: 'stored', label: 'MySQL 存储' }, { value: 'direct', label: '企微直连' }]}
      />
      <Select
        value={filters.enabled} aria-label="按启用状态筛选"
        onChange={(enabled) => setFilters((current) => ({ ...current, enabled }))}
        options={[{ value: 'all', label: '全部状态' }, { value: 'enabled', label: '已启用' }, { value: 'disabled', label: '已禁用' }]}
      />
      <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button>
    </div>

    {loadError && <Alert type="error" showIcon message={loadError} action={<Button size="small" onClick={load}>重试</Button>} />}

    <Table
      rowKey="tenant_id"
      columns={columns}
      dataSource={visibleTenants}
      loading={loading}
      pagination={false}
      size="middle"
      rowClassName={(row) => `tenant-table-row tenant-table-row--${row.data_mode === 'direct' ? 'direct' : 'stored'}`}
      scroll={{ x: 960 }}
      locale={{
        emptyText: loading || loadError ? null : !data.length ? (
          <Empty description="还没有租户，先添加第一个企业接入">
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增租户</Button>
          </Empty>
        ) : (
          <Empty description="没有匹配的租户">
            {hasFilters && <Button onClick={() => setFilters({ ...EMPTY_FILTERS })}>清空筛选</Button>}
          </Empty>
        ),
      }}
    />
  </section>
</main>
```

Keep the MCP, domain, force-sync, delete-confirmation, and diagnosis dialogs as siblings after `</main>` in the returned fragment.

- [x] **Step 6: Run deterministic tests and production build**

```powershell
cd D:\app\wbsysc\admin-ui
node --test src/pages/tenantsView.test.js
pnpm run build
```

Expected: all helper tests pass; Vite exits `0` and emits `dist/` assets without JSX or import errors.

- [x] **Step 7: Commit the workbench structure**

```powershell
git add admin-ui/src/pages/Tenants.jsx
git commit -m "feat: reshape tenant management workbench"
```

---

### Task 3: Safe right-side tenant editor

**Files:**
- Modify: `admin-ui/src/pages/Tenants.jsx`

**Interfaces:**
- Consumes: Existing Ant Design `Form` instance, existing tenant payload assembly, existing create and update endpoints.
- Produces: Right-side create/edit drawer with dirty-close confirmation, fixed footer, grouped form sections, and unchanged submission semantics.

- [x] **Step 1: Rename editor state and track dirty input**

Replace `modalOpen` state with:

```js
const [editorOpen, setEditorOpen] = useState(false)
const [editorDirty, setEditorDirty] = useState(false)
```

At the end of both `openCreate` and `openEdit`, call `setEditorDirty(false)` and `setEditorOpen(true)`. On successful save, call `setEditorDirty(false)` and `setEditorOpen(false)` before `load()`.

- [x] **Step 2: Add one guarded close path**

Add:

```js
const closeEditor = () => {
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
  Modal.confirm({
    title: '放弃未保存的修改？',
    content: '当前租户配置已发生变化，关闭后这些修改不会保留。',
    okText: '放弃修改',
    cancelText: '继续编辑',
    okButtonProps: { danger: true },
    onOk: closeEditor,
  })
}
```

Use `requestCloseEditor` for the drawer close button, mask click, Escape key, and footer cancellation through the Drawer `onClose` and cancel button.

- [x] **Step 3: Replace the editor Modal with a grouped Drawer**

Use the verified Ant Design 5.29.3 Drawer API:

```jsx
<Drawer
  rootClassName="tenant-editor"
  title={editing ? '配置租户' : '新增租户'}
  open={editorOpen}
  onClose={requestCloseEditor}
  width={680}
  destroyOnHidden={false}
  maskClosable={!saving}
  extra={editing && <Tag className={`mode-tag mode-tag--${editing.data_mode}`}>{editing.data_mode === 'direct' ? '企微直连' : 'MySQL 存储'}</Tag>}
  footer={(
    <div className="tenant-editor-footer">
      <Button onClick={requestCloseEditor} disabled={saving}>取消</Button>
      <Button type="primary" onClick={submit} loading={saving}>保存配置</Button>
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
      <div className="tenant-form-section__heading"><div><h2>基本信息</h2><p>用于识别租户和控制启用状态。</p></div></div>
      <div className="tenant-form-grid">
        <Form.Item name="tenant_id" label="租户 ID" rules={[{ required: true, message: '请输入租户 ID' }]}>
          <Input disabled={Boolean(editing)} placeholder="如 customerA" />
        </Form.Item>
        <Form.Item name="display_name" label="显示名称"><Input placeholder="企业或项目名称" /></Form.Item>
      </div>
      <Form.Item name="enabled" label="启用租户" valuePropName="checked"><Switch /></Form.Item>
    </section>

    <section className="tenant-form-section">
      <div className="tenant-form-section__heading"><div><h2>连接凭据</h2><p>用于调用企微接口和连接 MCP 服务。</p></div></div>
      <Form.Item name="corpid" label="企业 CorpID" rules={[{ required: true, message: '请输入企业 CorpID' }]}>
        <Input placeholder="wwXXXXXXXX" />
      </Form.Item>
      <Form.Item name="mcp_token" label="MCP 连接 Token" rules={[{ required: !editing, message: '请输入 MCP 连接 Token' }]} extra={editing ? '留空将保留现有 Token' : '供 WorkBuddy / CodeBuddy 的 MCP Server headers 使用'}>
        <Input.Password placeholder={editing ? '留空表示不修改' : '输入长随机串'} />
      </Form.Item>
      <Form.Item name="secret" label="自建应用 Secret" extra={editing ? '留空表示不修改' : '新租户需要配置应用 Secret'}>
        <Input.Password placeholder={editing ? '留空表示不修改' : '输入应用 Secret'} />
      </Form.Item>
      <Form.Item name="contact_secret" label="通讯录同步 Secret（可选）" extra="配置后自动获取企业成员 userid；编辑时留空表示不修改">
        <Input.Password placeholder="可选" />
      </Form.Item>
    </section>

    <section className="tenant-form-section">
      <div className="tenant-form-section__heading"><div><h2>数据与同步策略</h2><p>选择 MySQL 存储或企微实时直连。</p></div></div>
      <Form.Item name="data_mode" label="数据模式" rules={[{ required: true, message: '请选择数据模式' }]} extra="MySQL 存储会定时写入业务数据；企微直连每次实时请求且不保存业务数据">
        <Select options={[{ value: 'stored', label: 'MySQL 存储' }, { value: 'direct', label: '企微直连（不缓存）' }]} />
      </Form.Item>
      <Form.Item name="enabled_modules" label="启用模块" rules={[{ required: true, message: '请选择至少一个模块' }]}>
        <Select mode="multiple" options={MODULES.map((module) => ({ value: module, label: module }))} />
      </Form.Item>
      <div className="tenant-form-grid">
        <Form.Item name="sync_interval_min" label="同步间隔（分钟）"><InputNumber min={1} max={1440} /></Form.Item>
        <Form.Item name="checkin_userids" label="打卡 userid（可选）" extra="多个 userid 使用英文逗号分隔">
          <Input.TextArea autoSize={{ minRows: 1, maxRows: 3 }} placeholder="userA,userB" />
        </Form.Item>
      </div>
    </section>

    <section className="tenant-form-section">
      <div className="tenant-form-section__heading"><div><h2>可信域名</h2><p>配置 MCP 服务对外访问的域名。</p></div></div>
      <Form.Item name="trusted_domain" label="可信域名（可选）" extra="不要包含 https://，校验文件仍可从租户列表的“更多”菜单上传">
        <Input placeholder="mcp.example.com" />
      </Form.Item>
    </section>
  </Form>
</Drawer>
```

- [x] **Step 4: Verify editor behavior and build**

Run:

```powershell
cd D:\app\wbsysc\admin-ui
node --test src/pages/tenantsView.test.js
pnpm run build
```

Expected: tests and build pass. During smoke testing, an untouched drawer closes immediately; a changed field produces the discard confirmation; a failed save leaves the drawer and input open; a successful save closes and reloads the list.

- [x] **Step 5: Commit the editor interaction**

```powershell
git add admin-ui/src/pages/Tenants.jsx
git commit -m "feat: move tenant configuration into drawer"
```

---

### Task 4: Page-scoped visual system and responsive rules

**Files:**
- Create: `admin-ui/src/pages/Tenants.css`

**Interfaces:**
- Consumes: Class names emitted by `Tenants.jsx`.
- Produces: Approved operations-console palette, continuous status rail, row mode signature, responsive table/tooling, and nearly full-width mobile drawer.

- [ ] **Step 1: Add the complete page stylesheet**

Create `admin-ui/src/pages/Tenants.css`:

```css
.tenant-workbench {
  --tenant-ink: #17323d;
  --tenant-stored: #167d95;
  --tenant-direct: #7356a3;
  --tenant-canvas: #eef3f5;
  --tenant-running: #2c8c68;
  min-height: 100%;
  margin: -24px;
  padding: 24px;
  color: var(--tenant-ink);
  background: var(--tenant-canvas);
}

.tenant-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 20px;
}

.tenant-heading h1 {
  margin: 4px 0 4px;
  color: var(--tenant-ink);
  font-size: clamp(28px, 3vw, 40px);
  line-height: 1.08;
  letter-spacing: -0.035em;
}

.tenant-heading .ant-typography { margin-bottom: 0; }

.tenant-eyebrow {
  color: var(--tenant-stored);
  font: 700 11px/1.2 ui-monospace, SFMono-Regular, Consolas, monospace;
  letter-spacing: 0.14em;
}

.tenant-status-rail {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  overflow: hidden;
  margin-bottom: 16px;
  border: 1px solid #d7e1e5;
  border-radius: 12px;
  background: #fff;
}

.status-rail-item {
  position: relative;
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  min-height: 76px;
  padding: 18px 20px 16px;
  border-right: 1px solid #e3eaed;
}

.status-rail-item:last-child { border-right: 0; }
.status-rail-item::before { content: ''; position: absolute; inset: 0 auto 0 0; width: 3px; background: #9badb4; }
.status-rail-item--running::before { background: var(--tenant-running); }
.status-rail-item--direct::before { background: var(--tenant-direct); }
.status-rail-item--attention::before { background: #c77a2b; }
.status-rail-item span { color: #61757e; font-size: 13px; }
.status-rail-item strong { color: var(--tenant-ink); font: 700 28px/1 ui-monospace, SFMono-Regular, Consolas, monospace; }

.tenant-panel {
  overflow: hidden;
  border: 1px solid #d7e1e5;
  border-radius: 14px;
  background: #fff;
}

.tenant-toolbar {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) 160px 150px auto;
  gap: 10px;
  padding: 14px;
  border-bottom: 1px solid #e3eaed;
  background: #f8fafb;
}

.tenant-panel > .ant-alert { margin: 12px 14px 0; }
.tenant-panel .ant-table-wrapper { padding: 0 14px 14px; }
.tenant-panel .ant-table-thead > tr > th { color: #60727a; font-size: 12px; font-weight: 700; letter-spacing: 0.035em; text-transform: uppercase; }
.tenant-panel .ant-table-tbody > tr > td:first-child { position: relative; padding-left: 20px; }
.tenant-panel .ant-table-tbody > .tenant-table-row > td:first-child::before { content: ''; position: absolute; inset: 8px auto 8px 0; width: 4px; border-radius: 0 3px 3px 0; background: var(--tenant-stored); }
.tenant-panel .ant-table-tbody > .tenant-table-row--direct > td:first-child::before { background: var(--tenant-direct); }

.tenant-identity,
.tenant-company,
.tenant-policy,
.tenant-status { display: flex; flex-direction: column; align-items: flex-start; gap: 5px; min-width: 0; }

.tenant-code { max-width: 100%; overflow: hidden; color: #687b83; font: 12px/1.45 ui-monospace, SFMono-Regular, Consolas, monospace; text-overflow: ellipsis; white-space: nowrap; }
.tenant-company .ant-tag { margin-inline-end: 0; font-size: 11px; }
.tenant-policy .ant-typography:last-child,
.tenant-status .ant-typography { font-size: 12px; }

.mode-tag { margin: 0; border-color: transparent; font-weight: 650; }
.mode-tag--stored { color: #0e6679; background: #e4f3f6; }
.mode-tag--direct { color: #62428d; background: #f0eafa; }
.tenant-menu-label { display: flex; flex-direction: column; gap: 2px; }
.tenant-menu-label small { max-width: 230px; color: #8a969b; font-size: 11px; white-space: normal; }

.tenant-editor .ant-drawer-header { border-bottom-color: #dfe7ea; }
.tenant-editor .ant-drawer-body { background: var(--tenant-canvas, #eef3f5); }
.tenant-editor .ant-drawer-footer { padding: 12px 24px; border-top-color: #dfe7ea; background: #fff; }
.tenant-editor-footer { display: flex; justify-content: flex-end; gap: 10px; }

.tenant-form-section {
  margin-bottom: 14px;
  padding: 20px 20px 4px;
  border: 1px solid #d8e2e6;
  border-radius: 12px;
  background: #fff;
}

.tenant-form-section__heading { margin-bottom: 18px; padding-left: 12px; border-left: 3px solid #167d95; }
.tenant-form-section__heading h2 { margin: 0; color: #17323d; font-size: 16px; line-height: 1.3; }
.tenant-form-section__heading p { margin: 3px 0 0; color: #70828a; font-size: 12px; }
.tenant-form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }
.tenant-form-grid .ant-input-number { width: 100%; }

@media (max-width: 900px) {
  .tenant-status-rail { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .status-rail-item:nth-child(2) { border-right: 0; }
  .status-rail-item:nth-child(-n + 2) { border-bottom: 1px solid #e3eaed; }
  .tenant-toolbar { grid-template-columns: minmax(220px, 1fr) 150px 140px; }
  .tenant-toolbar > .ant-btn { grid-column: 3; }
}

@media (max-width: 640px) {
  .tenant-heading { align-items: stretch; flex-direction: column; }
  .tenant-heading > .ant-btn { width: 100%; }
  .tenant-status-rail { grid-template-columns: 1fr 1fr; }
  .status-rail-item { min-height: 68px; padding: 14px; }
  .status-rail-item strong { font-size: 23px; }
  .tenant-toolbar { grid-template-columns: 1fr 1fr; }
  .tenant-toolbar > .ant-input-affix-wrapper { grid-column: 1 / -1; }
  .tenant-toolbar > .ant-btn { grid-column: 1 / -1; }
  .tenant-panel .ant-table-wrapper { padding-inline: 8px; }
  .tenant-form-grid { grid-template-columns: 1fr; }
  .tenant-form-section { padding-inline: 14px; }
  .tenant-editor .ant-drawer-content-wrapper { width: calc(100vw - 12px) !important; }
}

@media (prefers-reduced-motion: reduce) {
  .tenant-workbench *,
  .tenant-workbench *::before,
  .tenant-workbench *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
}
```

- [ ] **Step 2: Run build and Ant Design lint**

```powershell
cd D:\app\wbsysc\admin-ui
pnpm run build
```

Expected: build exits `0` without JSX, import, or deprecated-API warnings. Verify the component source against the queried Ant Design 5.29.3 APIs and complete the keyboard/accessibility checks in Task 5 before committing.

- [ ] **Step 3: Commit the visual system**

```powershell
git add admin-ui/src/pages/Tenants.css admin-ui/src/pages/Tenants.jsx
git commit -m "style: polish tenant operations console"
```

---

### Task 5: Visual, interaction, and cross-model review gate

**Files:**
- Modify when findings require it: `admin-ui/src/pages/Tenants.jsx`
- Modify when findings require it: `admin-ui/src/pages/Tenants.css`
- Modify: `.ccg/tasks/admin-tenant-page-polish/task.json`
- Create: `.ccg/tasks/admin-tenant-page-polish/review.md`

**Interfaces:**
- Consumes: The complete tenant workbench and approved design specification.
- Produces: Evidence-backed final review, corrected implementation, archived CCG task, and a clean worktree except for user-owned changes.

- [ ] **Step 1: Run all deterministic checks from a clean command sequence**

```powershell
cd D:\app\wbsysc\admin-ui
node --test src/pages/tenantsView.test.js
pnpm run build
cd D:\app\wbsysc
git diff --check
git status --short
```

Expected: tests pass, build exits `0`, `git diff --check` is empty, and status lists only the intended task files. The visual-companion scratch directory remains in the primary checkout until final cleanup.

- [ ] **Step 2: Review the page at desktop width**

Run the Vite development server, open the authenticated tenant page at `1440 × 900`, and save a screenshot. Verify:

- Status rail reads as one continuous instrument panel.
- Stored and direct rows use distinct left rails and matching tags.
- The action column contains only “配置” and “更多”.
- Search and both filters fit on one line.
- IDs truncate safely and reveal/copy the complete value.

- [ ] **Step 3: Review the page at narrow width**

Resize the same page to `390 × 844` and save a screenshot. Verify:

- Status rail becomes two columns without overflow.
- Search, filters, and refresh remain usable.
- Secondary columns hide while the table retains horizontal scrolling.
- The editor drawer is `calc(100vw - 12px)` wide and its fixed footer does not cover the final field.

- [ ] **Step 4: Run the main interaction smoke test**

Use a non-destructive existing tenant and verify:

1. Search by display name, tenant ID, and CorpID returns the same row.
2. Mode and enabled filters combine correctly, and “清空筛选” restores all rows.
3. “配置” opens the populated drawer without exposing stored secrets.
4. Closing an untouched drawer is immediate; changing a field opens the discard confirmation; choose “继续编辑” and confirm the drawer remains open.
5. The “更多” menu opens MCP configuration and trusted-domain dialogs through their existing paths.
6. A direct tenant shows sync, full rollback, and diagnosis disabled with the explanatory Tooltip.
7. Do not confirm delete, upload a domain file, save edited credentials, or trigger full synchronization during smoke testing.

- [ ] **Step 5: Run required Gemini and Claude reviews in parallel**

Ask both models to review `git diff` plus the design specification for correctness, regressions, security, accessibility, responsive behavior, and Ant Design 5.29.3 use. Classify the merged findings as Critical, Warning, or Info in `.ccg/tasks/admin-tenant-page-polish/review.md`.

Expected: no unresolved Critical finding. Fix valid Critical and Warning findings, then rerun Steps 1–4 and repeat the dual-model review if code changed.

- [ ] **Step 6: Commit review fixes and task evidence**

```powershell
git add admin-ui/src/pages/Tenants.jsx admin-ui/src/pages/Tenants.css admin-ui/src/pages/tenantsView.js admin-ui/src/pages/tenantsView.test.js .ccg/tasks/admin-tenant-page-polish
git commit -m "fix: address tenant page review findings"
```

Skip this commit only when there are no code fixes and the review evidence is already included in the final archive commit.

- [ ] **Step 7: Archive the completed CCG task and clean temporary visual files**

Set `task.json` to `status: "completed"`, `currentPhase: "completed"`, and `nextAction: "已完成并归档"`. Move the task directory to `.ccg/tasks/archive/2026-07/admin-tenant-page-polish`, stop the visual-companion server, and remove only `D:\app\wbsysc\.superpowers\brainstorm\1006-1783952989` after verifying that exact resolved path is inside the repository scratch directory.

Then commit the archive:

```powershell
git add .ccg/tasks docs/superpowers/plans/2026-07-13-admin-tenant-page-polish.md
git commit -m "chore: archive ccg task admin-tenant-page-polish"
```
