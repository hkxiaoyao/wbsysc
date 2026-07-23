const IDENTIFIER = /^[A-Za-z][A-Za-z0-9_.-]{0,127}$/
const STEP_IDENTIFIER = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/
const INPUT_REFERENCE = /^\$input\.([A-Za-z][A-Za-z0-9_.-]{0,127})$/
const STEP_REFERENCE = /^\$steps\.([A-Za-z][A-Za-z0-9_-]{0,63})\.([A-Za-z][A-Za-z0-9_.-]{0,127})$/
const REFERENCE_MARKER = /\$input\.|\$steps\.|\$\{/
const SCHEMA_TYPES = new Set(['string', 'integer', 'number', 'boolean', 'array', 'object', 'null'])
const MAX_TOOLS = 64
const MAX_STEPS = 16
const MAX_INPUTS = 64
const MAX_OUTPUTS = 32

function isObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function ownEntries(value) {
  return isObject(value) ? Object.entries(value) : []
}

function hasReferenceMarker(value) {
  if (typeof value === 'string') return REFERENCE_MARKER.test(value)
  if (Array.isArray(value)) return value.some(hasReferenceMarker)
  return isObject(value) && Object.entries(value).some(([key, child]) => (
    REFERENCE_MARKER.test(key) || hasReferenceMarker(child)
  ))
}

function isSafeJsonValue(value, depth = 0) {
  if (depth > 24 || value === undefined || ['function', 'symbol', 'bigint'].includes(typeof value)) return false
  if (typeof value === 'number') return Number.isFinite(value)
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true
  if (Array.isArray(value)) return value.every((child) => isSafeJsonValue(child, depth + 1))
  return isObject(value) && Object.values(value).every((child) => isSafeJsonValue(child, depth + 1))
}

function validPropertySchema(schema, depth = 0) {
  if (!isObject(schema) || depth > 8 || hasReferenceMarker(schema) || !isSafeJsonValue(schema)) return false
  if (schema.type !== undefined && !SCHEMA_TYPES.has(schema.type)) return false
  if ('items' in schema && !validPropertySchema(schema.items, depth + 1)) return false
  if ('properties' in schema) {
    if (!isObject(schema.properties)) return false
    if (!Object.entries(schema.properties).every(([name, child]) => (
      IDENTIFIER.test(name) && validPropertySchema(child, depth + 1)
    ))) return false
    if ('required' in schema && (!Array.isArray(schema.required)
      || new Set(schema.required).size !== schema.required.length
      || schema.required.some((name) => typeof name !== 'string' || !(name in schema.properties)))) return false
  }
  return true
}

function validClosedSchema(schema, requireProperties = false) {
  if (!isObject(schema)
    || schema.type !== 'object'
    || schema.additionalProperties !== false
    || !isObject(schema.properties)
    || Object.keys(schema.properties).length > MAX_INPUTS
    || (requireProperties && Object.keys(schema.properties).length === 0)
    || !Array.isArray(schema.required)
    || new Set(schema.required).size !== schema.required.length) return false
  const fields = Object.keys(schema.properties)
  return fields.every((name) => IDENTIFIER.test(name) && validPropertySchema(schema.properties[name]))
    && schema.required.every((name) => fields.includes(name))
    && !hasReferenceMarker(schema)
}

function safeCatalogOperation(operation) {
  if (!isObject(operation)) return null
  const requiredKeys = [
    'operation_key', 'mcp_name', 'description', 'operation_kind', 'input_schema', 'output_names',
  ]
  if (!requiredKeys.every((key) => Object.hasOwn(operation, key))
    || !IDENTIFIER.test(operation.operation_key)
    || !IDENTIFIER.test(operation.mcp_name)
    || typeof operation.description !== 'string'
    || operation.description.length > 512
    || hasReferenceMarker(operation.description)
    || !['read', 'write'].includes(operation.operation_kind)
    || !validClosedSchema(operation.input_schema)
    || !Array.isArray(operation.output_names)
    || operation.output_names.length < 1
    || operation.output_names.length > MAX_OUTPUTS
    || new Set(operation.output_names).size !== operation.output_names.length
    || operation.output_names.some((name) => !IDENTIFIER.test(name))) return null
  return {
    operation_key: operation.operation_key,
    mcp_name: operation.mcp_name,
    description: operation.description,
    operation_kind: operation.operation_kind,
    input_schema: operation.input_schema,
    output_names: [...operation.output_names],
  }
}

export function safeOperationCatalog(operationCatalog) {
  if (!Array.isArray(operationCatalog) || operationCatalog.length < 1 || operationCatalog.length > MAX_TOOLS) {
    throw new Error('后端未返回有界安全操作元数据')
  }
  const safe = operationCatalog.map(safeCatalogOperation)
  if (safe.some((operation) => !operation)) {
    throw new Error('后端安全操作元数据不完整或重复')
  }
  const namespace = new Map()
  safe.forEach((operation, index) => {
    for (const name of new Set([operation.operation_key, operation.mcp_name])) {
      if (namespace.has(name) && namespace.get(name) !== index) {
        throw new Error('后端操作 Key 与 MCP 名称命名空间冲突或重复')
      }
      namespace.set(name, index)
    }
  })
  if (namespace.size < safe.length) {
    throw new Error('后端操作 Key 与 MCP 名称命名空间冲突或重复')
  }
  return safe
}

function exactReference(reference, allowed) {
  return typeof reference === 'string'
    && (INPUT_REFERENCE.test(reference) || STEP_REFERENCE.test(reference))
    && allowed.has(reference)
}

export function availableReferences(tool, steps, currentIndex) {
  const inputRefs = Object.keys(tool?.input_schema?.properties || {})
    .filter((name) => IDENTIFIER.test(name))
    .map((name) => `$input.${name}`)
  const stepRefs = (Array.isArray(steps) ? steps : []).slice(0, Math.max(0, currentIndex)).flatMap((step) => (
    STEP_IDENTIFIER.test(step?.step_id || '')
      ? Object.keys(step?.output_mappings || {})
        .filter((name) => IDENTIFIER.test(name))
        .map((name) => `$steps.${step.step_id}.${name}`)
      : []
  ))
  return [...inputRefs, ...stepRefs]
}

export function validateToolDraft(tool, operationCatalog = []) {
  const errors = []
  if (!isObject(tool)) return ['组合工具必须是对象']
  if (!IDENTIFIER.test(tool.tool_key || '')) errors.push('工具 Key 必须是有效标识符')
  if (!IDENTIFIER.test(tool.mcp_name || '')) errors.push('MCP 名称必须是有效标识符')
  if (typeof tool.description !== 'string' || tool.description.length > 512 || hasReferenceMarker(tool.description)) {
    errors.push('安全描述不能超过 512 字符或包含引用表达式')
  }
  if (!validClosedSchema(tool.input_schema)) errors.push('输入 schema 必须是关闭且有效的对象 schema')
  if (!validClosedSchema(tool.output_schema, true)) errors.push('输出 schema 必须声明至少一个关闭字段')

  const steps = Array.isArray(tool.steps) ? tool.steps : []
  if (steps.length < 1 || steps.length > MAX_STEPS) errors.push('组合工具必须包含 1 至 16 个步骤')
  const stepIds = steps.map((step) => step?.step_id)
  if (stepIds.some((id) => !STEP_IDENTIFIER.test(id || '')) || new Set(stepIds).size !== stepIds.length) {
    errors.push('步骤 ID 必须有效且不能重复')
  }

  let approvedOperations = []
  try {
    approvedOperations = safeOperationCatalog(operationCatalog)
  } catch (catalogError) {
    errors.push(catalogError.message)
  }
  const catalog = new Map(approvedOperations.map((operation) => [operation.operation_key, operation]))
  let writeCount = 0
  steps.forEach((step, index) => {
    const label = step?.step_id || String(index + 1)
    const operation = catalog.get(step?.operation_key)
    if (!operation) {
      errors.push(`步骤 ${label} 缺少服务器批准的安全元数据`)
      return
    }
    if (operation.operation_kind === 'write') writeCount += 1
    if (!isObject(step.input_map) || Object.keys(step.input_map).length > MAX_INPUTS) {
      errors.push(`步骤 ${label} 输入映射无效`)
    } else {
      const operationInputs = Object.keys(operation.input_schema.properties)
      const allowed = new Set(availableReferences(tool, steps, index))
      for (const required of operation.input_schema.required) {
        if (!Object.hasOwn(step.input_map, required) || !step.input_map[required]) {
          errors.push(`步骤 ${label} 缺少必填输入 ${required}`)
        }
      }
      for (const [name, reference] of Object.entries(step.input_map)) {
        if (!operationInputs.includes(name)) errors.push(`步骤 ${label} 包含未知操作输入 ${name}`)
        if (!exactReference(reference, allowed)) errors.push(`步骤 ${label} 的引用必须来自工具输入或前序步骤`)
      }
    }
    if (!isObject(step.output_mappings)
      || Object.keys(step.output_mappings).length < 1
      || Object.keys(step.output_mappings).length > MAX_OUTPUTS) {
      errors.push(`步骤 ${label} 必须声明 1 至 32 个输出映射`)
    } else {
      for (const [name, operationOutput] of Object.entries(step.output_mappings)) {
        if (!IDENTIFIER.test(name)) errors.push(`步骤 ${label} 输出名称无效`)
        if (!operation.output_names.includes(operationOutput)) {
          errors.push(`步骤 ${label} 选择了未知输出 ${operationOutput}`)
        }
      }
    }
  })
  if (writeCount > 1) errors.push('组合工具最多包含一个写步骤')

  const resultMap = isObject(tool.result_map) ? tool.result_map : {}
  const outputFields = Object.keys(tool?.output_schema?.properties || {})
  const resultFields = Object.keys(resultMap)
  const finalAllowed = new Set(availableReferences(tool, steps, steps.length))
  if (outputFields.length === 0
    || resultFields.length !== outputFields.length
    || resultFields.some((name) => !outputFields.includes(name))
    || outputFields.some((name) => !Object.hasOwn(resultMap, name))
    || Object.values(resultMap).some((reference) => !exactReference(reference, finalAllowed))) {
    errors.push('最终输出映射必须完整引用已声明值')
  }
  return [...new Set(errors)]
}

function cleanSchema(schema) {
  return JSON.parse(JSON.stringify(schema))
}

export function buildMcpToolsExtension(tools, operationCatalog = undefined) {
  if (!Array.isArray(tools) || tools.length > MAX_TOOLS) throw new Error('组合工具数量必须在 0 至 64 之间')
  const namespace = new Map()
  return tools.map((tool, index) => {
    const catalog = operationCatalog ?? tool.operationCatalog ?? []
    const errors = validateToolDraft(tool, catalog)
    for (const name of new Set([tool.tool_key, tool.mcp_name])) {
      if (namespace.has(name) && namespace.get(name) !== index) {
        errors.unshift('工具 Key 与 MCP 名称的统一命名空间不能跨工具冲突')
      }
      namespace.set(name, index)
    }
    if (errors.length) throw new Error(errors.join('；'))
    return {
      tool_key: tool.tool_key,
      mcp_name: tool.mcp_name,
      description: tool.description,
      input_schema: cleanSchema(tool.input_schema),
      output_schema: cleanSchema(tool.output_schema),
      steps: tool.steps.map((step) => ({
        step_id: step.step_id,
        operation_key: step.operation_key,
        input_map: { ...step.input_map },
        output_mappings: { ...step.output_mappings },
      })),
      result_map: { ...tool.result_map },
    }
  })
}

function parseUniqueJson(text) {
  if (typeof text !== 'string') throw new Error('JSON 文档必须是文本')
  let index = 0
  const whitespace = () => { while (/\s/.test(text[index] || '')) index += 1 }
  const string = () => {
    const start = index
    if (text[index] !== '"') throw new Error('JSON 文档无效')
    index += 1
    while (index < text.length) {
      if (text[index] === '\\') { index += 2; continue }
      if (text[index] === '"') {
        index += 1
        try { return JSON.parse(text.slice(start, index)) } catch { throw new Error('JSON 文档无效') }
      }
      if (text.charCodeAt(index) < 0x20) throw new Error('JSON 文档无效')
      index += 1
    }
    throw new Error('JSON 文档无效')
  }
  const value = () => {
    whitespace()
    if (text[index] === '"') return string()
    if (text[index] === '{') {
      const object = {}
      const keys = new Set()
      index += 1
      whitespace()
      if (text[index] === '}') { index += 1; return object }
      while (index < text.length) {
        whitespace()
        const key = string()
        if (keys.has(key)) throw new Error(`JSON 文档包含重复 Key：${key}`)
        keys.add(key)
        whitespace()
        if (text[index] !== ':') throw new Error('JSON 文档无效')
        index += 1
        object[key] = value()
        whitespace()
        if (text[index] === '}') { index += 1; return object }
        if (text[index] !== ',') throw new Error('JSON 文档无效')
        index += 1
      }
      throw new Error('JSON 文档无效')
    }
    if (text[index] === '[') {
      const array = []
      index += 1
      whitespace()
      if (text[index] === ']') { index += 1; return array }
      while (index < text.length) {
        array.push(value())
        whitespace()
        if (text[index] === ']') { index += 1; return array }
        if (text[index] !== ',') throw new Error('JSON 文档无效')
        index += 1
      }
      throw new Error('JSON 文档无效')
    }
    for (const [literal, result] of [['true', true], ['false', false], ['null', null]]) {
      if (text.startsWith(literal, index)) { index += literal.length; return result }
    }
    const number = text.slice(index).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/)
    if (!number) throw new Error('JSON 文档无效')
    index += number[0].length
    const parsed = Number(number[0])
    if (!Number.isFinite(parsed)) throw new Error('JSON 数字必须是有限值')
    return parsed
  }
  const parsed = value()
  whitespace()
  if (index !== text.length) throw new Error('JSON 文档无效')
  return parsed
}

export function mergeJsonMcpToolsExtension(sourceText, tools, operationCatalog = undefined) {
  const document = parseUniqueJson(sourceText)
  if (!isObject(document)) throw new Error('JSON 文档根节点必须是对象')
  document['x-mcp-tools'] = buildMcpToolsExtension(tools, operationCatalog)
  return `${JSON.stringify(document, null, 2)}\n`
}

function yamlExtensionLine(line) {
  return /^(?:x-mcp-tools|['"]x-mcp-tools['"])\s*:/.test(line)
}

function yamlFlowEndLine(lines, startLine) {
  const header = /^(?:x-mcp-tools|['"]x-mcp-tools['"])\s*:/.exec(lines[startLine])
  if (!header) throw new Error('YAML 根 x-mcp-tools 无效')
  let column = header[0].length
  while (/\s/.test(lines[startLine][column] || '')) column += 1
  if (!['[', '{'].includes(lines[startLine][column])) return null

  const stack = []
  let quote = ''
  let escaped = false
  let atNodeStart = true
  let nodeKind = ''

  const mappingSeparator = (line, index) => {
    if (nodeKind === 'quoted' || nodeKind === 'collection') return true
    const next = line[index + 1] || ''
    return !next || /\s/.test(next) || ',[]{}#\'"'.includes(next)
  }

  const assertSafeTrailing = (line, closeIndex) => {
    for (let index = closeIndex + 1; index < line.length; index += 1) {
      const character = line[index]
      if (/\s/.test(character)) continue
      if (character === '#' && index > 0 && /\s/.test(line[index - 1])) return
      throw new Error('YAML x-mcp-tools flow 外层闭合后包含尾随内容')
    }
  }

  for (let lineIndex = startLine; lineIndex < lines.length; lineIndex += 1) {
    const line = lines[lineIndex]
    let inComment = false
    for (let index = lineIndex === startLine ? column : 0; index < line.length; index += 1) {
      const character = line[index]
      if (inComment) break
      if (quote === '"') {
        if (escaped) {
          escaped = false
        } else if (character === '\\') {
          escaped = true
        } else if (character === '"') {
          quote = ''
          nodeKind = 'quoted'
        }
        continue
      }
      if (quote === "'") {
        if (character === "'" && line[index + 1] === "'") {
          index += 1
        } else if (character === "'") {
          quote = ''
          nodeKind = 'quoted'
        }
        continue
      }
      if (character === '#' && (index === 0 || /\s/.test(line[index - 1]))) {
        inComment = true
      } else if ((character === '"' || character === "'") && atNodeStart) {
        quote = character
        atNodeStart = false
        nodeKind = 'quoted'
      } else if (character === '[' || character === '{') {
        stack.push(character)
        atNodeStart = true
        nodeKind = ''
      } else if (character === ']' || character === '}') {
        const expected = character === ']' ? '[' : '{'
        if (stack.pop() !== expected) throw new Error('YAML x-mcp-tools flow 集合括号不匹配')
        if (stack.length === 0) {
          assertSafeTrailing(line, index)
          return lineIndex + 1
        }
        atNodeStart = false
        nodeKind = 'collection'
      } else if (character === ',') {
        atNodeStart = true
        nodeKind = ''
      } else if (character === ':' && mappingSeparator(line, index)) {
        atNodeStart = true
        nodeKind = ''
      } else if (!/\s/.test(character) && atNodeStart) {
        atNodeStart = false
        nodeKind = 'plain'
      }
    }
    if (quote === '"' && escaped) escaped = false
  }
  throw new Error('YAML x-mcp-tools flow 集合未闭合')
}

export function mergeYamlMcpToolsExtension(sourceText, tools, operationCatalog = undefined) {
  if (typeof sourceText !== 'string' || !sourceText.trim()) throw new Error('YAML 文档无效')
  const lines = sourceText.split(/\r?\n/)
  if (lines.some((line) => /^\s*(?:---|\.\.\.)\s*(?:#.*)?$/.test(line))) {
    throw new Error('YAML 多文档不支持安全合并')
  }
  const occurrences = []
  lines.forEach((line, index) => {
    if (/^\s*#/.test(line)) return
    if (/x-mcp-tools\s*:/.test(line) || /['"]x-mcp-tools['"]\s*:/.test(line)) {
      if (!yamlExtensionLine(line)) throw new Error('YAML 仅允许唯一根 x-mcp-tools')
      occurrences.push(index)
    }
  })
  if (occurrences.length > 1) throw new Error('YAML 包含重复根 x-mcp-tools')
  const extension = `x-mcp-tools: ${JSON.stringify(buildMcpToolsExtension(tools, operationCatalog))}`
  if (occurrences.length === 0) {
    const separator = sourceText.endsWith('\n') || sourceText.endsWith('\r') ? '' : '\n'
    return `${sourceText}${separator}${extension}\n`
  }
  const start = occurrences[0]
  const flowEnd = yamlFlowEndLine(lines, start)
  let end = flowEnd || start + 1
  const headerValue = lines[start].replace(/^(?:x-mcp-tools|['"]x-mcp-tools['"])\s*:/, '').trim()
  if (!flowEnd && (!headerValue || headerValue.startsWith('#'))) {
    while (end < lines.length) {
      const line = lines[end]
      if (line.trim() && !/^\s/.test(line)) break
      end += 1
    }
  }
  lines.splice(start, end - start, extension)
  return lines.join('\n')
}

export function mergeMcpToolsExtension(sourceText, sourceFormat, tools, operationCatalog = undefined) {
  if (!Array.isArray(tools) || tools.length === 0) return sourceText
  if (sourceFormat === 'json') return mergeJsonMcpToolsExtension(sourceText, tools, operationCatalog)
  if (sourceFormat === 'yaml') return mergeYamlMcpToolsExtension(sourceText, tools, operationCatalog)
  throw new Error('仅支持 JSON 或 YAML 文档')
}

export function createSourceGeneration() {
  let current = 0
  return {
    begin(identity = {}) {
      current += 1
      return Object.freeze({
        generation: current,
        specId: String(identity.specId || '').trim(),
        revision: Number(identity.revision),
        phase: String(identity.phase || ''),
      })
    },
    isCurrent(ticket) { return Boolean(ticket) && ticket.generation === current },
    invalidate() { current += 1 },
    capture() { return current },
  }
}

export function createRevisionAllocator(initialRevision = 0) {
  let current = Number.isSafeInteger(Number(initialRevision)) ? Number(initialRevision) : 0
  return {
    reserve() {
      current += 1
      return current
    },
    capture() { return current },
    reset(revision = 0) {
      current = Number.isSafeInteger(Number(revision)) ? Number(revision) : 0
    },
  }
}

export function operationKindSummary(tool, operationCatalog = []) {
  const kinds = new Map((Array.isArray(operationCatalog) ? operationCatalog : [])
    .map(safeCatalogOperation)
    .filter(Boolean)
    .map((operation) => [operation.operation_key, operation.operation_kind]))
  let readCount = 0
  let writeCount = 0
  for (const step of Array.isArray(tool?.steps) ? tool.steps : []) {
    if (kinds.get(step.operation_key) === 'write') writeCount += 1
    else if (kinds.get(step.operation_key) === 'read') readCount += 1
  }
  return {
    readCount,
    writeCount,
    operationKind: writeCount ? 'write' : 'read',
    label: `${readCount} 个只读步骤，${writeCount} 个写步骤`,
    warning: writeCount ? '此组合工具会写入上游系统，启用时需要明确同意。' : '',
  }
}
