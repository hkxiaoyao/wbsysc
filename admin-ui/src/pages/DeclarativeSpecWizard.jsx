import { useEffect, useMemo, useState } from 'react'
import { Alert, Button, Checkbox, Form, Input, InputNumber, Space, Steps, Tag, Typography, Upload, message } from 'antd'
import { InboxOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import api from '../api.js'
import { canEnableWriteTool, createWizardState, hasExplicitPolicies, safeServerError } from './connectionView.js'

const { Paragraph, Text } = Typography

export default function DeclarativeSpecWizard({ connection, onChanged = () => {} }) {
  const [state, setState] = useState(() => createWizardState(connection))
  const [tools, setTools] = useState([])
  const [policies, setPolicies] = useState([])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [messageApi, contextHolder] = message.useMessage()

  useEffect(() => {
    setState(createWizardState(connection))
    setTools([])
    setPolicies([])
    setError('')
  }, [connection?.connection_id, connection?.status])

  const base = `/admin/connections/${encodeURIComponent(connection.connection_id)}`
  const policyComplete = useMemo(() => hasExplicitPolicies(tools, policies), [tools, policies])
  const patchState = (patch) => setState((current) => ({ ...current, ...patch }))

  const run = async (key, operation, successText) => {
    setBusy(key)
    setError('')
    try {
      const result = await operation()
      if (successText) messageApi.success(successText)
      return result
    } catch (requestError) {
      setError(safeServerError(requestError))
      return null
    } finally { setBusy('') }
  }

  const disableActive = async () => {
    const result = await run('disable', () => api.post(`${base}/disable`), '连接已停用，可以安全准备待发布修订')
    if (result) {
      patchState({ mustDisable: false })
      onChanged(result.data?.connection)
    }
  }

  const importSpec = async () => {
    if (!state.sourceText.trim() || !state.specId.trim()) {
      setError('请填写 Spec ID 并粘贴或导入 JSON/YAML 文档')
      return
    }
    const result = await run('import', () => api.post(`${base}/specs/import`, {
      document: state.sourceText,
      spec_id: state.specId.trim(),
      revision: Number(state.revision),
    }), '规范已导入，等待后端验证')
    if (result) patchState({ imported: true, step: 2 })
  }

  const validate = async () => {
    const result = await run('validate', () => api.post(
      `${base}/specs/${encodeURIComponent(state.specId.trim())}/revisions/${state.revision}/validate`,
    ), '后端验证通过')
    if (!result) return
    const toolResponse = await run('tools', () => api.get(`${base}/tools`))
    if (!toolResponse) return
    const nextTools = toolResponse.data?.items || []
    setTools(nextTools)
    setPolicies([])
    patchState({ validated: true, step: 3 })
  }

  const setPolicy = (tool, values) => {
    setPolicies((current) => {
      const next = current.filter((item) => item.tool_key !== tool.tool_key)
      return [...next, { tool_key: tool.tool_key, ...values }]
    })
  }

  const savePolicies = async () => {
    if (!policyComplete) {
      setError('必须为每个待发布工具明确选择启用或停用策略')
      return
    }
    const result = await run('policies', () => api.put(`${base}/tools`, { policies }), '工具策略已保存')
    if (result) patchState({ step: 4 })
  }

  const testConnection = async () => {
    const result = await run('test', () => api.post(`${base}/test`), '安全只读连接测试通过')
    if (result) patchState({ tested: true, step: 5 })
  }

  const publish = async () => {
    const result = await run('publish', () => api.post(
      `${base}/specs/${encodeURIComponent(state.specId.trim())}/revisions/${state.revision}/publish`,
    ), '修订已发布')
    if (result) patchState({ published: true, step: 6 })
  }

  const activate = async () => {
    const result = await run('activate', () => api.post(
      `${base}/specs/${encodeURIComponent(state.specId.trim())}/revisions/${state.revision}/activate`,
    ), '连接已激活')
    if (result) {
      patchState({ activated: true, step: 7 })
      onChanged(result.data?.connection)
    }
  }

  const policyFor = (tool) => policies.find((item) => item.tool_key === tool.tool_key)

  return (
    <section className="declarative-wizard" aria-label="声明式连接向导">
      {contextHolder}
      <Steps
        current={Math.min(state.step, 6)}
        size="small"
        responsive
        items={[
          { title: '选择类型' }, { title: '导入规范' }, { title: '后端验证' },
          { title: '工具策略' }, { title: '安全测试' }, { title: '发布' }, { title: '激活' },
        ]}
      />

      {error && <Alert showIcon type="error" message="无法继续" description={error} />}
      {state.mustDisable && (
        <Alert
          showIcon
          type="warning"
          message="先停用当前连接"
          description="活动连接不能导入或发布待定修订。停用不会展示或还原任何凭据。"
          action={<Button danger loading={busy === 'disable'} onClick={disableActive}>停用后继续</Button>}
        />
      )}

      <div className="declarative-stage">
        <header><Text strong>HTTP 声明式连接</Text><Tag color="cyan">http_declarative</Tag></header>
        <Paragraph type="secondary">只接受 JSON/YAML OpenAPI 子集。解析、校验和出站安全检查全部由后端完成；不支持脚本、模板或自定义代码。</Paragraph>
        <Form layout="vertical">
          <div className="declarative-id-grid">
            <Form.Item label="Spec ID" required>
              <Input value={state.specId} maxLength={64} placeholder="例如 crm-api" onChange={(event) => patchState({ specId: event.target.value })} />
            </Form.Item>
            <Form.Item label="修订号" required>
              <InputNumber min={1} precision={0} value={state.revision} onChange={(revision) => patchState({ revision: revision || 1 })} />
            </Form.Item>
          </div>
          <Form.Item label="规范文档" required>
            <Upload.Dragger
              accept=".json,.yaml,.yml,application/json,text/yaml"
              maxCount={1}
              beforeUpload={(file) => {
                const reader = new FileReader()
                reader.onload = () => patchState({
                  sourceText: String(reader.result || ''),
                  sourceFormat: /\.ya?ml$/i.test(file.name) ? 'yaml' : 'json',
                  imported: false,
                })
                reader.readAsText(file)
                return false
              }}
              onRemove={() => { patchState({ sourceText: '', imported: false }); return true }}
            >
              <p className="ant-upload-drag-icon"><InboxOutlined /></p>
              <p>拖入 JSON/YAML，或点击选择文件</p>
            </Upload.Dragger>
            <Input.TextArea
              className="declarative-source"
              rows={8}
              maxLength={1_000_000}
              value={state.sourceText}
              placeholder="也可以在这里粘贴 OpenAPI JSON 或 YAML"
              onChange={(event) => patchState({ sourceText: event.target.value, imported: false })}
            />
          </Form.Item>
        </Form>
        <Space wrap>
          <Button type="primary" disabled={state.mustDisable} loading={busy === 'import'} onClick={importSpec}>导入规范</Button>
          <Button disabled={!state.imported} loading={busy === 'validate' || busy === 'tools'} onClick={validate}>后端验证</Button>
        </Space>
      </div>

      {state.validated && (
        <div className="declarative-stage">
          <header><Text strong>操作与批准映射</Text><Tag color="green">验证通过</Tag></header>
          <Paragraph type="secondary">操作和输入/输出映射来自后端批准的规范。你只能明确启用或停用，并为写操作另行授权。</Paragraph>
          <div className="declarative-tool-list">
            {tools.map((tool) => {
              const current = policyFor(tool)
              return (
                <div key={tool.tool_key} className="declarative-tool">
                  <div><Text code>{tool.mcp_name || tool.tool_key}</Text><Tag color={tool.operation_kind === 'write' ? 'orange' : 'blue'}>{tool.operation_kind === 'write' ? '写操作' : '只读'}</Tag></div>
                  <Space wrap>
                    <Checkbox
                      checked={current?.enabled === true}
                      onChange={(event) => setPolicy(tool, {
                        enabled: event.target.checked,
                        allow_write: tool.operation_kind === 'write' ? Boolean(current?.allow_write) : false,
                      })}
                    >明确启用</Checkbox>
                    {tool.operation_kind === 'write' && (
                      <Checkbox
                        checked={current?.allow_write === true}
                        disabled={!current?.enabled}
                        onChange={(event) => setPolicy(tool, {
                          enabled: Boolean(current?.enabled),
                          allow_write: event.target.checked,
                        })}
                      >我同意写入上游系统</Checkbox>
                    )}
                    <Button size="small" onClick={() => setPolicy(tool, { enabled: false, allow_write: false })}>明确停用</Button>
                  </Space>
                  {current?.enabled && !canEnableWriteTool(tool, { explicitEnable: true, explicitWrite: current.allow_write }) && <Text type="danger">写操作还需要明确同意</Text>}
                </div>
              )
            })}
          </div>
          <Button type="primary" disabled={!policyComplete} loading={busy === 'policies'} onClick={savePolicies}>保存全部工具策略</Button>
        </div>
      )}

      {state.step >= 4 && (
        <div className="declarative-stage declarative-stage--actions">
          <Button icon={<SafetyCertificateOutlined />} loading={busy === 'test'} onClick={testConnection}>运行安全只读测试</Button>
          <Button disabled={!state.tested} loading={busy === 'publish'} onClick={publish}>发布修订</Button>
          <Button type="primary" disabled={!state.published || !policyComplete} loading={busy === 'activate'} onClick={activate}>激活连接</Button>
          {state.activated && <Tag color="success">已激活</Tag>}
        </div>
      )}
    </section>
  )
}
