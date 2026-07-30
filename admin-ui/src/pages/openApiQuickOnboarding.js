import { apiClientEndpoint, connectionResourceEndpoint } from './connectionView.js'

export const MAX_OPENAPI_DOCUMENT_BYTES = 1_000_000

function cleanText(value) {
  return String(value ?? '').trim()
}

function plainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function utf8Size(value) {
  if (typeof TextEncoder === 'function') return new TextEncoder().encode(value).length
  return unescape(encodeURIComponent(value)).length
}

export function openApiQuickEndpoint(apiClient, scope, connectionId, action) {
  if (!['analyze', 'activate'].includes(action)) throw new Error('未知的 OpenAPI 快速接入操作')
  return apiClientEndpoint(
    apiClient,
    connectionResourceEndpoint(scope, connectionId, `openapi/${action}`),
  )
}

export function parseOpenApiDocument(source, fileName = '') {
  const text = String(source ?? '').replace(/^\uFEFF/, '')
  const trimmed = text.trim()
  if (!trimmed) throw new Error('请上传或粘贴 OpenAPI JSON/YAML 文档')
  if (utf8Size(text) > MAX_OPENAPI_DOCUMENT_BYTES) throw new Error('OpenAPI 文档不能超过 1 MB')
  if (/^https?:\/\/\S+$/i.test(trimmed)) throw new Error('暂不支持 URL 导入，请上传或粘贴 JSON/YAML')

  const jsonFile = /\.json$/i.test(cleanText(fileName))
  const jsonInput = jsonFile || /^[{[]/.test(trimmed)
  if (jsonInput) {
    let document
    try { document = JSON.parse(trimmed) } catch {
      if (jsonFile) throw new Error('OpenAPI JSON 格式无效')
    }
    if (document !== undefined) {
      if (!plainObject(document)) throw new Error('OpenAPI 文档必须是对象')
      return { format: 'json', document }
    }
  }

  return { format: 'yaml', document: text }
}

export function normalizeOpenApiAnalysis(payload) {
  const source = plainObject(payload?.analysis) ? payload.analysis : payload
  if (!plainObject(source)) throw new Error('后端未返回有效的 OpenAPI 分析结果')

  const specId = cleanText(source.spec_id ?? source.specId)
  const revision = Number(source.revision)
  const expectedConfigVersion = Number(
    source.expected_config_version ?? source.config_version ?? source.expectedConfigVersion,
  )
  const analysisDigest = cleanText(source.analysis_digest ?? source.analysisDigest ?? source.digest)
  const rawTools = source.items ?? source.tools ?? source.preview?.tools
  if (!specId || !Number.isInteger(revision) || revision < 1
    || !Number.isInteger(expectedConfigVersion) || expectedConfigVersion < 1
    || !analysisDigest || !Array.isArray(rawTools)) {
    throw new Error('后端返回的 OpenAPI 分析标识不完整')
  }

  const seen = new Set()
  const tools = rawTools.map((item) => {
    if (!plainObject(item)) throw new Error('后端返回的工具摘要无效')
    const toolKey = cleanText(item.tool_key ?? item.toolKey ?? item.key)
    const operationKind = cleanText(item.operation_kind ?? item.operationKind).toLowerCase()
    if (!toolKey || seen.has(toolKey) || !['read', 'write'].includes(operationKind)) {
      throw new Error('后端返回的工具分类无效')
    }
    seen.add(toolKey)
    return {
      tool_key: toolKey,
      mcp_name: cleanText(item.mcp_name ?? item.mcpName ?? item.name) || toolKey,
      description: cleanText(item.description),
      operation_kind: operationKind,
    }
  })

  const credentialSchema = source.credential_schema
    ?? source.credentialSchema
    ?? source.credentials_schema
    ?? { type: 'object', properties: {} }
  if (!plainObject(credentialSchema)) throw new Error('后端返回的凭据 schema 无效')

  return {
    spec_id: specId,
    revision,
    expected_config_version: expectedConfigVersion,
    analysis_digest: analysisDigest,
    credential_schema: credentialSchema,
    tools,
  }
}

export function classifyOpenApiTools(tools = []) {
  const readTools = []
  const writeTools = []
  for (const tool of tools) {
    if (tool?.operation_kind === 'read') readTools.push(tool)
    else if (tool?.operation_kind === 'write') writeTools.push(tool)
    else throw new Error('工具必须明确标记为只读或写操作')
  }
  return { readTools, writeTools }
}

function isSecretField(name, schema) {
  return schema?.writeOnly === true
    || schema?.['x-secret'] === true
    || ['password', 'secret'].includes(cleanText(schema?.format).toLowerCase())
    || /(password|passwd|secret|token|api[_-]?key|authorization)/i.test(name)
}

export function credentialFields(schema = {}) {
  if (!plainObject(schema)) return []
  const properties = plainObject(schema.properties) ? schema.properties : {}
  const required = new Set(Array.isArray(schema.required) ? schema.required : [])
  return Object.entries(properties).map(([name, value]) => {
    const property = plainObject(value) ? value : {}
    const enumValues = Array.isArray(property.enum)
      ? property.enum.filter((item) => ['string', 'number', 'boolean'].includes(typeof item))
      : []
    return {
      name,
      label: cleanText(property.title) || name,
      description: cleanText(property.description),
      required: required.has(name),
      secret: isSecretField(name, property),
      type: cleanText(property.type).toLowerCase() || 'string',
      enumValues,
    }
  })
}

export function emptyCredentialValues(schema = {}) {
  return Object.fromEntries(credentialFields(schema).map((field) => [
    field.name,
    field.type === 'boolean' ? false : field.type === 'number' || field.type === 'integer' ? null : '',
  ]))
}

export function requiredCredentialsPresent(schema = {}, values = {}) {
  return credentialFields(schema).filter((field) => field.required).every((field) => {
    const value = values[field.name]
    if (field.type === 'boolean') return typeof value === 'boolean'
    if (field.type === 'number' || field.type === 'integer') return typeof value === 'number' && Number.isFinite(value)
    return cleanText(value) !== ''
  })
}

export function buildCredentialPayload(schema = {}, values = {}) {
  const credentials = {}
  for (const field of credentialFields(schema)) {
    const value = values[field.name]
    if (value === undefined || value === null || (!field.required && typeof value === 'string' && value === '')) continue
    credentials[field.name] = value
  }
  return credentials
}

export function buildOpenApiActivationRequest(analysis, credentials, enabledWriteTools = []) {
  if (!plainObject(analysis)) throw new Error('请先分析 OpenAPI 文档')
  if (!plainObject(credentials)) throw new Error('凭据必须是对象')
  const allowedWriteTools = new Set(classifyOpenApiTools(analysis.tools || []).writeTools.map((tool) => tool.tool_key))
  const enabled = [...new Set(enabledWriteTools.map(cleanText).filter(Boolean))]
  if (enabled.some((toolKey) => !allowedWriteTools.has(toolKey))) {
    throw new Error('只能显式启用分析结果中的写工具')
  }
  return {
    spec_id: analysis.spec_id,
    revision: analysis.revision,
    expected_config_version: analysis.expected_config_version,
    analysis_digest: analysis.analysis_digest,
    credentials: { ...credentials },
    enabled_write_tools: enabled,
  }
}
