import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Button, Checkbox, Form, Input, InputNumber, Select, Space, Steps, Switch, Tag, Typography, Upload, message,
} from 'antd'
import { InboxOutlined, RocketOutlined } from '@ant-design/icons'
import defaultApi from '../api.js'
import { safeServerError } from './connectionView.js'
import {
  buildCredentialPayload,
  buildOpenApiActivationRequest,
  classifyOpenApiTools,
  credentialFields,
  emptyCredentialValues,
  normalizeOpenApiAnalysis,
  openApiQuickEndpoint,
  parseOpenApiDocument,
  requiredCredentialsPresent,
} from './openApiQuickOnboarding.js'

const { Paragraph, Text } = Typography

export default function OpenApiQuickOnboarding({
  connection,
  apiClient = defaultApi,
  scope = 'admin',
  active = true,
  onChanged = () => {},
}) {
  const [sourceText, setSourceText] = useState('')
  const [sourceName, setSourceName] = useState('')
  const [analysis, setAnalysis] = useState(null)
  const [credentials, setCredentials] = useState({})
  const [enabledWriteTools, setEnabledWriteTools] = useState([])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [activated, setActivated] = useState(false)
  const [messageApi, contextHolder] = message.useMessage()
  const requestGeneration = useRef(0)
  const requestController = useRef(null)

  const fields = useMemo(
    () => credentialFields(analysis?.credential_schema),
    [analysis?.credential_schema],
  )
  const { readTools, writeTools } = useMemo(
    () => classifyOpenApiTools(analysis?.tools || []),
    [analysis?.tools],
  )
  const credentialsReady = requiredCredentialsPresent(analysis?.credential_schema, credentials)
  const mustDisable = connection?.status === 'active'

  const invalidateRequest = () => {
    requestGeneration.current += 1
    requestController.current?.abort()
  }

  const clearCredentialState = (schema = analysis?.credential_schema) => {
    setCredentials(emptyCredentialValues(schema))
  }

  const resetAnalysis = () => {
    invalidateRequest()
    setAnalysis(null)
    setCredentials({})
    setEnabledWriteTools([])
    setBusy('')
    setError('')
    setActivated(false)
  }

  useEffect(() => {
    setSourceText('')
    setSourceName('')
    resetAnalysis()
    return invalidateRequest
  }, [connection?.connection_id, scope, apiClient])

  useEffect(() => {
    if (!active) clearCredentialState()
  }, [active])

  const changeSource = (text, name = '') => {
    setSourceText(text)
    setSourceName(name)
    resetAnalysis()
  }

  const analyze = async () => {
    let document
    try {
      document = parseOpenApiDocument(sourceText, sourceName).document
    } catch (parseError) {
      setError(parseError.message)
      return
    }

    invalidateRequest()
    const generation = requestGeneration.current
    const controller = new AbortController()
    requestController.current = controller
    setBusy('analyze')
    setError('')
    setActivated(false)
    setAnalysis(null)
    setCredentials({})
    setEnabledWriteTools([])
    try {
      const response = await apiClient.post(
        openApiQuickEndpoint(apiClient, scope, connection.connection_id, 'analyze'),
        { document },
        { signal: controller.signal },
      )
      if (controller.signal.aborted || generation !== requestGeneration.current) return
      const next = normalizeOpenApiAnalysis(response.data)
      setAnalysis(next)
      setCredentials(emptyCredentialValues(next.credential_schema))
      messageApi.success('OpenAPI 分析完成，请填写凭据并确认写权限')
    } catch (requestError) {
      if (!controller.signal.aborted && generation === requestGeneration.current) {
        setError(safeServerError(requestError, 'OpenAPI 分析失败，请检查文档'))
      }
    } finally {
      if (!controller.signal.aborted && generation === requestGeneration.current) setBusy('')
    }
  }

  const activate = async () => {
    if (!analysis) return
    if (!credentialsReady) {
      setError('请填写全部必填凭据')
      return
    }

    let payload
    try {
      payload = buildOpenApiActivationRequest(
        analysis,
        buildCredentialPayload(analysis.credential_schema, credentials),
        enabledWriteTools,
      )
    } catch (requestError) {
      setError(requestError.message)
      return
    }

    invalidateRequest()
    const generation = requestGeneration.current
    const controller = new AbortController()
    requestController.current = controller
    setBusy('activate')
    setError('')
    try {
      const response = await apiClient.post(
        openApiQuickEndpoint(apiClient, scope, connection.connection_id, 'activate'),
        payload,
        { signal: controller.signal },
      )
      if (controller.signal.aborted || generation !== requestGeneration.current) return
      setActivated(true)
      messageApi.success('连接测试通过并已激活')
      onChanged(response.data?.connection)
    } catch (requestError) {
      if (!controller.signal.aborted && generation === requestGeneration.current) {
        setError(safeServerError(requestError, '测试或激活失败，请检查凭据和连接配置'))
      }
    } finally {
      if (generation === requestGeneration.current) {
        clearCredentialState(analysis.credential_schema)
        if (!controller.signal.aborted) setBusy('')
      }
    }
  }

  const fieldControl = (field) => {
    const value = credentials[field.name]
    const update = (next) => setCredentials((current) => ({ ...current, [field.name]: next }))
    if (field.secret) {
      return <Input.Password aria-label={field.label} value={String(value ?? '')} autoComplete="new-password" onChange={(event) => update(event.target.value)} />
    }
    if (field.enumValues.length) {
      return <Select aria-label={field.label} value={value === '' ? undefined : value} options={field.enumValues.map((item) => ({ value: item, label: String(item) }))} onChange={update} />
    }
    if (field.type === 'boolean') {
      return <Switch aria-label={field.label} checked={Boolean(value)} checkedChildren="是" unCheckedChildren="否" onChange={update} />
    }
    if (field.type === 'number' || field.type === 'integer') {
      return <InputNumber aria-label={field.label} value={value} precision={field.type === 'integer' ? 0 : undefined} style={{ width: '100%' }} onChange={update} />
    }
    return <Input aria-label={field.label} value={String(value ?? '')} autoComplete="off" onChange={(event) => update(event.target.value)} />
  }

  return (
    <section className="declarative-wizard" aria-label="OpenAPI 两步快速接入">
      {contextHolder}
      <Steps
        current={analysis ? 1 : 0}
        size="small"
        responsive
        items={[{ title: '导入并分析' }, { title: '凭据、写权限与激活' }]}
      />

      {error && <Alert showIcon type="error" message="无法继续" description={error} />}
      {activated && <Alert showIcon type="success" message="连接已激活" description="凭据输入已从页面内存状态清空。" />}
      {mustDisable && <Alert showIcon type="warning" message="先停用当前连接" description="请先在“测试与同步”中停用连接，再分析新的 OpenAPI 修订。" />}

      <div className="declarative-stage">
        <header><Text strong>第一步：上传或粘贴 OpenAPI</Text><Tag color="cyan">JSON / YAML</Tag></header>
        <Paragraph type="secondary">本期不支持 URL。系统会自动生成规范标识与修订，并展示只读、写操作摘要。</Paragraph>
        <Upload.Dragger
          accept=".json,.yaml,.yml,application/json,text/yaml"
          maxCount={1}
          disabled={busy !== ''}
          beforeUpload={(file) => {
            const reader = new FileReader()
            reader.onload = () => changeSource(String(reader.result || ''), file.name)
            reader.onerror = () => setError('无法读取所选文件')
            reader.readAsText(file)
            return false
          }}
          onRemove={() => { changeSource(''); return true }}
        >
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p>拖入 JSON/YAML，或点击选择文件</p>
        </Upload.Dragger>
        <Input.TextArea
          aria-label="OpenAPI JSON 或 YAML 文档"
          className="declarative-source"
          rows={9}
          maxLength={1_000_000}
          disabled={busy !== ''}
          value={sourceText}
          placeholder="也可以在这里粘贴 OpenAPI JSON 或 YAML"
          onChange={(event) => changeSource(event.target.value)}
        />
        <Button type="primary" loading={busy === 'analyze'} disabled={busy === 'activate' || mustDisable} onClick={analyze}>分析 OpenAPI</Button>
      </div>

      {analysis && (
        <div className="declarative-stage">
          <header><Text strong>第二步：填写新凭据并确认写权限</Text><Tag color="gold">尚未激活</Tag></header>
          <Alert type="warning" showIcon message="现有凭据永不读取" description="这里只收集一组完整的新凭据；激活请求结束后，无论成功或失败都会立即清空输入。" />

          <div className="declarative-tool-list">
            <div className="declarative-tool">
              <Space wrap><Text strong>默认启用的只读工具</Text><Tag color="blue">{readTools.length}</Tag></Space>
              {readTools.length ? readTools.map((tool) => (
                <div key={tool.tool_key}><Text code>{tool.mcp_name}</Text>{tool.description && <Text type="secondary"> · {tool.description}</Text>}</div>
              )) : <Text type="secondary">没有只读工具</Text>}
            </div>
            <div className="declarative-tool">
              <Space wrap><Text strong>默认关闭的写工具</Text><Tag color="orange">{writeTools.length}</Tag></Space>
              {writeTools.length ? writeTools.map((tool) => (
                <div key={tool.tool_key}>
                  <Checkbox
                    checked={enabledWriteTools.includes(tool.tool_key)}
                    onChange={(event) => setEnabledWriteTools((current) => (
                      event.target.checked
                        ? [...new Set([...current, tool.tool_key])]
                        : current.filter((item) => item !== tool.tool_key)
                    ))}
                  >
                    明确启用并同意写入：<Text code>{tool.mcp_name}</Text>
                  </Checkbox>
                  {tool.description && <div><Text type="secondary">{tool.description}</Text></div>}
                </div>
              )) : <Text type="secondary">没有写工具需要确认</Text>}
            </div>
          </div>

          <Form layout="vertical" autoComplete="off">
            {fields.length ? fields.map((field) => (
              <Form.Item key={field.name} label={field.label} required={field.required} extra={field.description || undefined}>
                {fieldControl(field)}
              </Form.Item>
            )) : <Alert type="info" showIcon message="此规范不需要凭据" />}
          </Form>

          <Button type="primary" icon={<RocketOutlined />} loading={busy === 'activate'} disabled={busy === 'analyze' || !credentialsReady} onClick={activate}>
            测试并启用
          </Button>
        </div>
      )}
    </section>
  )
}
