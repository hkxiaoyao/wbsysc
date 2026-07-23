import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Checkbox, Form, Input, InputNumber, Space, Steps, Tag, Typography, Upload, message } from 'antd'
import { InboxOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import api from '../api.js'
import DeclarativeToolBuilder from './DeclarativeToolBuilder.jsx'
import {
  buildMcpToolsExtension,
  createRevisionAllocator,
  createSourceGeneration,
  mergeMcpToolsExtension,
  safeOperationCatalog,
} from './declarativeToolView.js'
import {
  canEnableWriteTool,
  createWizardState,
  hasExplicitPolicies,
  invalidateWizardState,
  requiredCredentialKeys,
  safeServerError,
  schemaMetadataSummary,
  setExplicitToolPolicy,
  wizardRevisionIdentity,
} from './connectionView.js'

const { Paragraph, Text } = Typography

function jsonObject(text) {
  try {
    const value = JSON.parse(text || '{}')
    if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error()
    return value
  } catch { throw new Error('凭据必须是 JSON 对象') }
}

function schemaSummary(value) {
  if (!value || typeof value !== 'object') return '后端暂未提供此映射元数据'
  return JSON.stringify(value, null, 2)
}

export default function DeclarativeSpecWizard({ connection, active = true, onChanged = () => {} }) {
  const [state, setState] = useState(() => createWizardState(connection))
  const [tools, setTools] = useState([])
  const [policies, setPolicies] = useState([])
  const [credentialSchema, setCredentialSchema] = useState(null)
  const [credentialsText, setCredentialsText] = useState('{}')
  const [originalSource, setOriginalSource] = useState(null)
  const [operationCatalog, setOperationCatalog] = useState([])
  const [compositeTools, setCompositeTools] = useState([])
  const [compositeImported, setCompositeImported] = useState(false)
  const [compositeValidated, setCompositeValidated] = useState(false)
  const [validationPreview, setValidationPreview] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [messageApi, contextHolder] = message.useMessage()
  const stateRef = useRef(state)
  const identityVersion = useRef(0)
  const sourceGeneration = useRef(createSourceGeneration())
  const revisionAllocator = useRef(createRevisionAllocator())

  useEffect(() => { stateRef.current = state }, [state])
  useEffect(() => {
    identityVersion.current += 1
    const next = createWizardState(connection)
    stateRef.current = next
    setState(next)
    setTools([])
    setPolicies([])
    setCredentialSchema(null)
    setCredentialsText('{}')
    setOriginalSource(null)
    setOperationCatalog([])
    setCompositeTools([])
    setCompositeImported(false)
    setCompositeValidated(false)
    setValidationPreview(null)
    sourceGeneration.current.invalidate()
    revisionAllocator.current.reset()
    setError('')
  }, [connection?.connection_id, connection?.status])
  useEffect(() => {
    if (!active) setCredentialsText('{}')
  }, [active])

  const base = `/admin/connections/${encodeURIComponent(connection.connection_id)}`
  const policyComplete = useMemo(() => hasExplicitPolicies(tools, policies), [tools, policies])
  const requiredCredentials = useMemo(() => requiredCredentialKeys(credentialSchema), [credentialSchema])
  const patchState = (patch) => setState((current) => ({ ...current, ...patch }))
  const identityIsCurrent = (identity) => {
    const current = wizardRevisionIdentity(stateRef.current)
    return identity.version === identityVersion.current
      && current.specId === identity.specId
      && current.revision === identity.revision
  }
  const captureIdentity = () => ({
    ...wizardRevisionIdentity(stateRef.current),
    version: identityVersion.current,
  })

  const run = async (key, operation, successText, completionIsCurrent = () => true) => {
    setBusy(key)
    setError('')
    try {
      const result = await operation()
      if (!completionIsCurrent()) return null
      if (successText) messageApi.success(successText)
      return result
    } catch (requestError) {
      if (completionIsCurrent()) setError(safeServerError(requestError))
      return null
    } finally { if (completionIsCurrent()) setBusy('') }
  }

  const invalidateSource = (patch) => {
    identityVersion.current += 1
    sourceGeneration.current.invalidate()
    setState((current) => ({ ...invalidateWizardState(current, 'source'), ...patch, step: 1 }))
    setTools([])
    setPolicies([])
    setCredentialSchema(null)
    setCredentialsText('{}')
    setOriginalSource(null)
    setOperationCatalog([])
    setCompositeTools([])
    setCompositeImported(false)
    setCompositeValidated(false)
    setValidationPreview(null)
    revisionAllocator.current.reset()
    setBusy('')
    setError('')
  }

  const disableActive = async () => {
    const result = await run('disable', () => api.post(`${base}/disable`), '连接已停用，可以安全准备待激活修订')
    if (result) {
      patchState({ mustDisable: false })
      onChanged(result.data?.connection)
    }
  }

  const importSpec = async () => {
    identityVersion.current += 1
    const identity = { ...wizardRevisionIdentity(state), version: identityVersion.current }
    if (!state.sourceText.trim() || !identity.specId) {
      setError('请填写 Spec ID 并粘贴或导入 JSON/YAML 文档')
      return
    }
    const sourceText = state.sourceText
    const sourceFormat = sourceText.trimStart().startsWith('{') ? 'json' : 'yaml'
    const ticket = sourceGeneration.current.begin({ ...identity, phase: 'source-import' })
    setState((current) => invalidateWizardState(current, 'source'))
    setTools([])
    setPolicies([])
    setCredentialSchema(null)
    setCredentialsText('{}')
    setOriginalSource(null)
    setOperationCatalog([])
    setCompositeTools([])
    setCompositeImported(false)
    setCompositeValidated(false)
    setValidationPreview(null)
    const result = await run('import', () => api.post(`${base}/specs/import`, {
      document: sourceText,
      spec_id: identity.specId,
      revision: identity.revision,
    }), '原始规范已导入', () => sourceGeneration.current.isCurrent(ticket) && identityIsCurrent(identity))
    if (result && sourceGeneration.current.isCurrent(ticket) && identityIsCurrent(identity)) {
      setOriginalSource({
        text: sourceText,
        format: sourceFormat,
        specId: identity.specId,
        revision: identity.revision,
      })
      revisionAllocator.current.reset(identity.revision)
      patchState({ imported: true, step: 2, sourceFormat })
    }
  }

  const validate = async () => {
    const identity = captureIdentity()
    const ticket = sourceGeneration.current.begin({ ...identity, phase: 'source-validate' })
    const result = await run('validate', () => api.post(
      `${base}/specs/${encodeURIComponent(identity.specId)}/revisions/${identity.revision}/validate`,
    ), '原始规范后端验证通过', () => sourceGeneration.current.isCurrent(ticket) && identityIsCurrent(identity))
    if (!result || !sourceGeneration.current.isCurrent(ticket) || !identityIsCurrent(identity)) return
    try {
      const catalog = safeOperationCatalog(result.data?.preview?.operations)
      setOperationCatalog(catalog)
      setValidationPreview(result.data?.preview || null)
      patchState({ validated: true, step: 3 })
    } catch (metadataError) {
      setOperationCatalog([])
      setValidationPreview(null)
      setError(metadataError.message)
    }
  }

  const changeCompositeTools = (nextTools) => {
    identityVersion.current += 1
    sourceGeneration.current.invalidate()
    setCompositeTools(nextTools)
    setCompositeImported(false)
    setCompositeValidated(false)
    setValidationPreview(null)
    setTools([])
    setPolicies([])
    setCredentialSchema(null)
    setCredentialsText('{}')
    setState((current) => ({
      ...current,
      ...(nextTools.length === 0 && originalSource ? {
        specId: originalSource.specId,
        revision: originalSource.revision,
        imported: true,
        validated: true,
        step: 3,
      } : {}),
      published: false,
      mappingReviewed: false,
      credentialsSaved: false,
      policiesSaved: false,
      tested: false,
      activated: false,
    }))
    setBusy('')
    setError('')
  }

  const importCompositeRevision = async () => {
    if (!originalSource || !operationCatalog.length || !compositeTools.length) return
    let mergedDocument
    try {
      buildMcpToolsExtension(compositeTools, operationCatalog)
      mergedDocument = mergeMcpToolsExtension(
        originalSource.text,
        originalSource.format,
        compositeTools,
        operationCatalog,
      )
    } catch (builderError) {
      setError(builderError.message)
      return
    }
    identityVersion.current += 1
    const nextIdentity = {
      specId: originalSource.specId,
      revision: revisionAllocator.current.reserve(),
      version: identityVersion.current,
    }
    const ticket = sourceGeneration.current.begin({ ...nextIdentity, phase: 'merged-import' })
    const result = await run('composite-import', () => api.post(`${base}/specs/import`, {
      document: mergedDocument,
      spec_id: nextIdentity.specId,
      revision: nextIdentity.revision,
    }), '组合工具已导入为新的不可变草稿修订', () => sourceGeneration.current.isCurrent(ticket))
    if (!result || !sourceGeneration.current.isCurrent(ticket)) return
    setState((current) => ({
      ...invalidateWizardState(current, 'source'),
      specId: nextIdentity.specId,
      revision: nextIdentity.revision,
      sourceText: originalSource.text,
      imported: true,
      step: 2,
    }))
    setCompositeImported(true)
    setCompositeValidated(false)
    setValidationPreview(null)
  }

  const validateCompositeRevision = async () => {
    if (!compositeImported) return
    const identity = captureIdentity()
    const ticket = sourceGeneration.current.begin({ ...identity, phase: 'merged-validate' })
    const result = await run('composite-validate', () => api.post(
      `${base}/specs/${encodeURIComponent(identity.specId)}/revisions/${identity.revision}/validate`,
    ), '组合工具草稿后端验证通过', () => sourceGeneration.current.isCurrent(ticket) && identityIsCurrent(identity))
    if (!result || !sourceGeneration.current.isCurrent(ticket) || !identityIsCurrent(identity)) return
    try {
      safeOperationCatalog(result.data?.preview?.operations)
      if (!Array.isArray(result.data?.preview?.tools) || result.data.preview.tools.length < 1) {
        throw new Error('后端未返回组合工具安全预览')
      }
      setValidationPreview(result.data.preview)
      setCompositeValidated(true)
      patchState({ validated: true, step: 3 })
    } catch (previewError) {
      setCompositeValidated(false)
      setValidationPreview(null)
      setError(previewError.message)
    }
  }

  const publish = async () => {
    const identity = captureIdentity()
    const ticket = sourceGeneration.current.begin({ ...identity, phase: 'publish' })
    const result = await run('publish', () => api.post(
      `${base}/specs/${encodeURIComponent(identity.specId)}/revisions/${identity.revision}/publish`,
    ), '待激活修订已发布', () => sourceGeneration.current.isCurrent(ticket) && identityIsCurrent(identity))
    if (!result || !sourceGeneration.current.isCurrent(ticket) || !identityIsCurrent(identity)) return
    const toolResponse = await run('tools', () => api.get(`${base}/tools`), '', () => (
      sourceGeneration.current.isCurrent(ticket) && identityIsCurrent(identity)
    ))
    if (!toolResponse || !sourceGeneration.current.isCurrent(ticket) || !identityIsCurrent(identity)) return
    setTools(toolResponse.data?.items || [])
    setCredentialSchema(toolResponse.data?.credential_schema || null)
    setPolicies([])
    patchState({ published: true, step: 4 })
  }

  const reviewMappings = (checked) => {
    setState((current) => ({
      ...invalidateWizardState(current, 'mapping'),
      mappingReviewed: checked,
    }))
  }

  const changeCredentials = (value) => {
    setCredentialsText(value)
    setState((current) => invalidateWizardState(current, 'credentials'))
  }

  const saveCredentials = async () => {
    const identity = captureIdentity()
    let credentials
    try { credentials = jsonObject(credentialsText) } catch (parseError) { setError(parseError.message); return }
    const result = await run('credentials', () => api.put(`${base}/credentials`, { credentials }), '待激活修订凭据已替换')
    if (result && identityIsCurrent(identity)) {
      setCredentialsText('{}')
      patchState({ credentialsSaved: true, step: 5 })
    }
  }

  const setPolicy = (tool, values) => {
    setPolicies((current) => {
      const next = current.filter((item) => item.tool_key !== tool.tool_key)
      return [...next, { tool_key: tool.tool_key, ...values }]
    })
    setState((current) => invalidateWizardState(current, 'policy'))
  }

  const togglePolicy = (tool, enabled) => {
    const current = policies.find((item) => item.tool_key === tool.tool_key)
    setPolicy(tool, setExplicitToolPolicy(tool, current, enabled))
  }

  const savePolicies = async () => {
    if (!policyComplete || !state.mappingReviewed || !state.credentialsSaved) {
      setError('先审核批准映射、替换待激活凭据，并为每个工具选择完整策略')
      return
    }
    const identity = captureIdentity()
    const submittedPolicies = policies.map((policy) => ({ ...policy }))
    const result = await run('policies', () => api.put(`${base}/tools`, { policies: submittedPolicies }), '工具策略已保存')
    if (result && identityIsCurrent(identity)) patchState({ policiesSaved: true, step: 6 })
  }

  const testConnection = async () => {
    if (!state.published || !state.mappingReviewed || !state.credentialsSaved || !state.policiesSaved) return
    const identity = captureIdentity()
    const result = await run('test', () => api.post(`${base}/test`), '真实安全只读连接测试通过')
    if (result && identityIsCurrent(identity)) patchState({ tested: true, step: 7 })
  }

  const activate = async () => {
    const identity = captureIdentity()
    const result = await run('activate', () => api.post(
      `${base}/specs/${encodeURIComponent(identity.specId)}/revisions/${identity.revision}/activate`,
    ), '连接已激活')
    if (result && identityIsCurrent(identity)) {
      patchState({ activated: true, step: 8 })
      onChanged(result.data?.connection)
    }
  }

  const policyFor = (tool) => policies.find((item) => item.tool_key === tool.tool_key)

  return (
    <section className="declarative-wizard" aria-label="声明式连接向导">
      {contextHolder}
      <Steps
        current={Math.min(state.step, 7)}
        size="small"
        responsive
        items={[
          { title: '选择类型' }, { title: '导入规范' }, { title: '后端验证' }, { title: '发布待激活修订' },
          { title: '映射与凭据' }, { title: '工具策略' }, { title: '真实测试' }, { title: '激活' },
        ]}
      />

      {error && <Alert showIcon type="error" message="无法继续" description={error} />}
      {state.mustDisable && (
        <Alert showIcon type="warning" message="先停用当前连接" description="活动连接不能导入或发布待激活修订。停用不会展示或还原任何凭据。" action={<Button danger loading={busy === 'disable'} onClick={disableActive}>停用后继续</Button>} />
      )}

      <div className="declarative-stage">
        <header><Text strong>HTTP 声明式连接</Text><Tag color="cyan">http_declarative</Tag></header>
        <Paragraph type="secondary">只接受 JSON/YAML OpenAPI 子集。解析、校验和出站安全检查由后端完成；不支持脚本、模板或自定义代码。</Paragraph>
        <Form layout="vertical">
          <div className="declarative-id-grid">
            <Form.Item label="Spec ID" required><Input aria-label="声明式规范 ID" value={state.specId} maxLength={64} placeholder="例如 crm-api" onChange={(event) => invalidateSource({ specId: event.target.value })} /></Form.Item>
            <Form.Item label="修订号" required><InputNumber aria-label="声明式规范修订号" min={1} precision={0} value={state.revision} onChange={(revision) => invalidateSource({ revision: revision || 1 })} /></Form.Item>
          </div>
          <Form.Item label="规范文档" required>
            <Upload.Dragger
              accept=".json,.yaml,.yml,application/json,text/yaml"
              maxCount={1}
              beforeUpload={(file) => {
                const reader = new FileReader()
                reader.onload = () => invalidateSource({ sourceText: String(reader.result || ''), sourceFormat: /\.ya?ml$/i.test(file.name) ? 'yaml' : 'json' })
                reader.readAsText(file)
                return false
              }}
              onRemove={() => { invalidateSource({ sourceText: '' }); return true }}
            ><p className="ant-upload-drag-icon"><InboxOutlined /></p><p>拖入 JSON/YAML，或点击选择文件</p></Upload.Dragger>
            <Input.TextArea aria-label="声明式 JSON 或 YAML 规范" className="declarative-source" rows={8} maxLength={1_000_000} value={state.sourceText} placeholder="也可以在这里粘贴 OpenAPI JSON 或 YAML" onChange={(event) => invalidateSource({ sourceText: event.target.value })} />
          </Form.Item>
        </Form>
        <Space wrap>
          <Button type="primary" disabled={state.mustDisable || state.published || Boolean(originalSource)} loading={busy === 'import'} onClick={importSpec}>导入原始规范</Button>
          <Button disabled={!state.imported || state.published || Boolean(operationCatalog.length) || compositeImported} loading={busy === 'validate'} onClick={validate}>验证原始修订并读取安全操作</Button>
          <Button disabled={!state.validated || (compositeTools.length > 0 && !compositeValidated)} loading={busy === 'publish' || busy === 'tools'} onClick={publish}>发布待激活修订</Button>
        </Space>
      </div>

      {operationCatalog.length > 0 && !state.published && (
        <div className="declarative-stage">
          <header><Text strong>构建多操作工具</Text><Tag color="cyan">仅使用后端批准元数据</Tag></header>
          <Paragraph type="secondary">原始修订保持不变。引用只能从下拉框选择工具输入或前序步骤的显式输出；如果不添加组合工具，将继续既有单操作工具流程。</Paragraph>
          <DeclarativeToolBuilder
            operationCatalog={operationCatalog}
            tools={compositeTools}
            disabled={busy !== ''}
            onChange={changeCompositeTools}
          />
          {compositeTools.length > 0 && (
            <Space wrap>
              <Button type="primary" disabled={compositeImported} loading={busy === 'composite-import'} onClick={importCompositeRevision}>导入为新草稿修订</Button>
              <Button disabled={!compositeImported || compositeValidated} loading={busy === 'composite-validate'} onClick={validateCompositeRevision}>再次后端验证</Button>
              {compositeValidated && <Tag color="green">新草稿修订已验证，可发布</Tag>}
            </Space>
          )}
        </div>
      )}

      {compositeValidated && validationPreview && !state.published && (
        <div className="declarative-stage">
          <header><Text strong>后端安全组合工具预览</Text><Tag color="green">验证通过</Tag></header>
          <Paragraph type="secondary">这里只展示后端 allowlist 中的工具、schema 与步骤摘要；不会展示传输、认证、路径或 secret 元数据。</Paragraph>
          <div className="declarative-tool-list">
            {(validationPreview.tools || []).map((tool) => (
              <div className="declarative-tool" key={tool.tool_key}>
                <Space wrap>
                  <Text code>{tool.mcp_name || tool.tool_key}</Text>
                  <Tag color={tool.operation_kind === 'write' ? 'orange' : 'blue'}>{tool.operation_kind === 'write' ? '写操作' : '只读'}</Tag>
                </Space>
                {tool.description && <Text type="secondary">{tool.description}</Text>}
                <details><summary>后端批准的输入 schema</summary><pre>{schemaSummary(schemaMetadataSummary(tool.input_schema))}</pre></details>
                <details><summary>后端批准的输出 schema</summary><pre>{schemaSummary(schemaMetadataSummary(tool.output_schema))}</pre></details>
                <div>
                  <Text type="secondary">有序步骤：</Text>
                  <Space wrap>{(tool.steps || []).map((step) => (
                    <Tag key={`${step.step_id}-${step.operation_key}`}>{step.step_id} → {step.operation_key} · {step.operation_kind === 'write' ? '写' : '只读'}</Tag>
                  ))}</Space>
                </div>
              </div>
            ))}
          </div>
          <Alert showIcon type="info" message="发布仍是独立操作" description="确认此后端预览后，使用上方“发布待激活修订”；系统不会覆盖或发布原始已验证修订。" />
        </div>
      )}

      {state.published && (
        <div className="declarative-stage">
          <header><Text strong>批准映射与待激活凭据</Text><Tag color="gold">尚未激活</Tag></header>
          <Paragraph type="secondary">以下元数据来自后端批准的不可变修订。只能审核操作和映射，不可输入脚本、模板或表达式。</Paragraph>
          <div className="declarative-tool-list">
            {tools.map((tool) => (
              <div key={tool.tool_key} className="declarative-tool">
                <div><Text code>{tool.mcp_name || tool.tool_key}</Text><Tag color={tool.operation_kind === 'write' ? 'orange' : 'blue'}>{tool.operation_kind === 'write' ? '写操作' : '只读'}</Tag></div>
                {tool.description && <Text type="secondary">{tool.description}</Text>}
                <details><summary>后端批准的输入 properties / mapping</summary><pre>{schemaSummary(tool.input_mapping || schemaMetadataSummary(tool.input_schema))}</pre></details>
                <details><summary>后端批准的输出 properties / mapping</summary><pre>{schemaSummary(tool.output_mapping || schemaMetadataSummary(tool.output_schema))}</pre></details>
              </div>
            ))}
          </div>
          <Checkbox checked={state.mappingReviewed} onChange={(event) => reviewMappings(event.target.checked)}>我已审核每个选定操作的后端批准输入/输出映射</Checkbox>
          <Alert type="warning" showIcon message="现有凭据永不读取" description="仅提交一组完整的新凭据。保存成功、修订变化或向导关闭后输入立即清除。" />
          <div><Text type="secondary">必填凭据 Key（仅名称）：</Text><Space size={4} wrap>{requiredCredentials.length ? requiredCredentials.map((key) => <Tag key={key}>{key}</Tag>) : <Tag>后端未声明必填 Key</Tag>}</Space></div>
          <details><summary>后端凭据 schema</summary><pre>{schemaSummary(schemaMetadataSummary(credentialSchema))}</pre></details>
          <Input.TextArea aria-label="待激活修订新凭据 JSON" rows={6} value={credentialsText} onChange={(event) => changeCredentials(event.target.value)} placeholder='{"api_key":"new value"}' />
          <Button type="primary" disabled={!state.mappingReviewed} loading={busy === 'credentials'} onClick={saveCredentials}>替换待激活凭据并清除输入</Button>
        </div>
      )}

      {state.published && (
        <div className="declarative-stage">
          <header><Text strong>操作选择与明确策略</Text><Tag color={policyComplete ? 'green' : 'default'}>{policyComplete ? '策略完整' : '等待逐项选择'}</Tag></header>
          <Paragraph type="secondary">每个操作必须明确启用或停用。写操作每次重新启用都必须重新同意写入。</Paragraph>
          <div className="declarative-tool-list">
            {tools.map((tool) => {
              const current = policyFor(tool)
              return <div key={tool.tool_key} className="declarative-tool"><div><Text code>{tool.mcp_name || tool.tool_key}</Text><Tag color={tool.operation_kind === 'write' ? 'orange' : 'blue'}>{tool.operation_kind}</Tag></div><Space wrap><Checkbox checked={current?.enabled === true} onChange={(event) => togglePolicy(tool, event.target.checked)}>明确启用</Checkbox>{tool.operation_kind === 'write' && <Checkbox checked={current?.allow_write === true} disabled={!current?.enabled} onChange={(event) => setPolicy(tool, { ...current, enabled: true, allow_write: event.target.checked })}>我同意写入上游系统</Checkbox>}<Button size="small" onClick={() => togglePolicy(tool, false)}>明确停用</Button></Space>{current?.enabled && !canEnableWriteTool(tool, { explicitEnable: Boolean(current.enabled), explicitWrite: current.allow_write }) && <Text type="danger">重新启用写操作后，需要新的明确同意</Text>}</div>
            })}
          </div>
          <Button type="primary" disabled={!policyComplete || !state.mappingReviewed || !state.credentialsSaved} loading={busy === 'policies'} onClick={savePolicies}>保存全部工具策略</Button>
        </div>
      )}

      {state.policiesSaved && (
        <div className="declarative-stage declarative-stage--actions">
          <Button icon={<SafetyCertificateOutlined />} loading={busy === 'test'} onClick={testConnection}>运行真实安全只读测试</Button>
          <Button type="primary" disabled={!state.tested} loading={busy === 'activate'} onClick={activate}>激活已测试修订</Button>
          {state.activated && <Tag color="success">已激活</Tag>}
        </div>
      )}
    </section>
  )
}
