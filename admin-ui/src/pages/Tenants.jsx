import { useEffect, useState } from 'react'
import { Table, Button, Modal, Form, Input, InputNumber, Select, Switch, Space, Tag, message, Typography } from 'antd'
import { PlusOutlined, ReloadOutlined, ThunderboltOutlined, EditOutlined, DeleteOutlined, CopyOutlined } from '@ant-design/icons'
import api from '../api.js'

const { Text, Paragraph } = Typography
const MODULES = ['report', 'approval', 'checkin']

function buildMcpConfig(row) {
  // 用当前站点 origin 拼 MCP 地址，适配直连/反代
  const origin = window.location.origin
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
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [mcpModal, setMcpModal] = useState({ open: false, title: '', text: '' })
  const [form] = Form.useForm()

  const load = async () => {
    setLoading(true)
    try {
      const r = await api.get('/admin/tenants')
      setData(r.data.items)
    } catch (e) {
      message.error('加载失败: ' + (e.response?.data?.detail || e.message))
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ enabled_modules: MODULES, sync_interval_min: 30, enabled: true })
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
    })
    setModalOpen(true)
  }

  const submit = async () => {
    const v = await form.validateFields()
    const payload = {
      ...v,
      enabled_modules: (v.enabled_modules || []).join(','),
      checkin_userids: v.checkin_userids || '',
    }
    try {
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
      message.error('保存失败: ' + (e.response?.data?.detail || e.message))
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

  const syncNow = async (row) => {
    try {
      await api.post(`/admin/tenants/${row.tenant_id}/sync`)
      message.success(`${row.tenant_id} 同步已触发(后台执行)`)
    } catch (e) {
      message.error('触发失败: ' + (e.response?.data?.detail || e.message))
    }
  }

  const openMcpConfig = (row) => {
    if (!row.mcp_token) {
      message.warning('该租户未配置 MCP Token')
      return
    }
    const text = JSON.stringify(buildMcpConfig(row), null, 2)
    setMcpModal({
      open: true,
      title: `MCP 配置 · ${row.display_name || row.tenant_id}`,
      text,
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

  const columns = [
    { title: '租户ID', dataIndex: 'tenant_id', key: 'tenant_id' },
    { title: '名称', dataIndex: 'display_name', key: 'display_name' },
    { title: 'CorpID', dataIndex: 'corpid', key: 'corpid' },
    {
      title: '模块', dataIndex: 'enabled_modules', key: 'modules',
      render: (v) => (v || '').split(',').filter(Boolean).map(m => <Tag key={m} color="blue">{m}</Tag>)
    },
    { title: '间隔(分)', dataIndex: 'sync_interval_min', key: 'interval', width: 80 },
    {
      title: '凭证', key: 'cred',
      render: (_, r) => (
        <Space size={4}>
          <Tag color={r.has_secret ? 'green' : 'red'}>应用{r.has_secret ? '✓' : '✗'}</Tag>
          <Tag color={r.has_contact_secret ? 'green' : 'default'}>通讯录{r.has_contact_secret ? '✓' : '—'}</Tag>
        </Space>
      )
    },
    {
      title: '状态', dataIndex: 'enabled', key: 'enabled', width: 80,
      render: (v) => <Tag color={v ? 'green' : 'default'}>{v ? '启用' : '禁用'}</Tag>
    },
    {
      title: '操作', key: 'op', width: 300,
      render: (_, r) => (
        <Space size={4} wrap>
          <Button size="small" icon={<CopyOutlined />} onClick={() => openMcpConfig(r)}>MCP配置</Button>
          <Button size="small" icon={<ThunderboltOutlined />} onClick={() => syncNow(r)}>同步</Button>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>编辑</Button>
          <Button size="small" danger icon={<DeleteOutlined />} onClick={() => remove(r)} />
        </Space>
      )
    },
  ]

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增租户</Button>
        <Button icon={<ReloadOutlined />} onClick={load} loading={loading}>刷新</Button>
        <Text type="secondary">点「MCP配置」可一键复制 WorkBuddy 连接 JSON</Text>
      </Space>
      <Table rowKey="tenant_id" columns={columns} dataSource={data} loading={loading} size="middle"
        pagination={false} scroll={{ x: 1100 }} />

      <Modal title={editing ? '编辑租户' : '新增租户'} open={modalOpen} onOk={submit}
        onCancel={() => setModalOpen(false)} width={620} okText="保存" cancelText="取消">
        <Form form={form} layout="vertical">
          <Form.Item name="tenant_id" label="租户ID" rules={[{ required: true }]}>
            <Input disabled={!!editing} placeholder="如 customerA" />
          </Form.Item>
          <Form.Item name="display_name" label="显示名称"><Input /></Form.Item>
          <Form.Item name="corpid" label="企业CorpID" rules={[{ required: true }]}>
            <Input placeholder="wwXXXXXXXX" />
          </Form.Item>
          <Form.Item name="mcp_token" label="MCP连接Token(workbuddy用)" rules={[{ required: true }]}
            extra="给客户配在 workbuddy 的 MCP Server headers；也可用列表「MCP配置」一键复制">
            <Input placeholder="长随机串" />
          </Form.Item>
          <Form.Item name="secret" label="自建应用Secret"
            extra={editing ? '留空=不修改' : '必填'}>
            <Input.Password placeholder={editing ? '****（不改留空）' : ''} />
          </Form.Item>
          <Form.Item name="contact_secret" label="通讯录同步Secret（可选）"
            extra="配置后自动拉全企业userid喂打卡；留空=不改">
            <Input.Password placeholder={editing ? '****（不改留空）' : ''} />
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
          粘贴到 WorkBuddy / CodeBuddy 的 MCP 配置中。URL 基于当前访问域名自动生成。
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
    </div>
  )
}
