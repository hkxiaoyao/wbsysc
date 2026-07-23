import { Alert, Button, Checkbox, Empty, Form, Input, Select, Space, Tag, Typography } from 'antd'
import {
  availableReferences,
  operationKindSummary,
  validateToolDraft,
} from './declarativeToolView.js'

const { Text } = Typography
const FIELD_TYPES = ['string', 'integer', 'number', 'boolean', 'array', 'object']

function blankSchema() {
  return { type: 'object', properties: {}, required: [], additionalProperties: false }
}

function blankTool(index) {
  return {
    tool_key: `composite.tool_${index + 1}`,
    mcp_name: `composite_tool_${index + 1}`,
    description: '',
    input_schema: blankSchema(),
    output_schema: blankSchema(),
    steps: [],
    result_map: {},
  }
}

function schemaFields(schema) {
  const required = new Set(schema?.required || [])
  return Object.entries(schema?.properties || {}).map(([name, value]) => ({
    name,
    type: value?.type || 'string',
    required: required.has(name),
  }))
}

function fieldsSchema(fields) {
  return {
    type: 'object',
    properties: Object.fromEntries(fields.filter((field) => field.name).map((field) => [
      field.name,
      field.type === 'array' ? { type: 'array', items: { type: 'string' } }
        : field.type === 'object' ? { type: 'object', properties: {}, additionalProperties: false }
          : { type: field.type },
    ])),
    required: fields.filter((field) => field.name && field.required).map((field) => field.name),
    additionalProperties: false,
  }
}

function operationFor(catalog, key) {
  return catalog.find((operation) => operation.operation_key === key)
}

function newStep(operation, index) {
  return {
    step_id: `step_${index + 1}`,
    operation_key: operation?.operation_key || '',
    input_map: {},
    output_mappings: Object.fromEntries((operation?.output_names || []).map((name) => [name, name])),
  }
}

function SchemaEditor({ label, schema, onChange, output = false, disabled = false }) {
  const fields = schemaFields(schema)
  const update = (next) => onChange(fieldsSchema(next.slice(0, 64)))
  return (
    <fieldset disabled={disabled}>
      <legend>{label}</legend>
      {!fields.length && <Text type="secondary">尚未声明字段</Text>}
      {fields.map((field, index) => (
        <Space key={`${field.name}-${index}`} wrap align="baseline">
          <Input
            aria-label={`${label}字段 ${index + 1} 名称`}
            value={field.name}
            maxLength={128}
            placeholder="字段名称"
            onChange={(event) => update(fields.map((item, itemIndex) => (
              itemIndex === index ? { ...item, name: event.target.value } : item
            )))}
          />
          <Select
            aria-label={`${label}字段 ${index + 1} 类型`}
            value={field.type}
            options={FIELD_TYPES.map((type) => ({ value: type, label: type }))}
            onChange={(type) => update(fields.map((item, itemIndex) => (
              itemIndex === index ? { ...item, type } : item
            )))}
          />
          <Checkbox
            checked={field.required}
            onChange={(event) => update(fields.map((item, itemIndex) => (
              itemIndex === index ? { ...item, required: event.target.checked } : item
            )))}
          >必填</Checkbox>
          <Button danger size="small" onClick={() => update(fields.filter((_, itemIndex) => itemIndex !== index))}>移除字段</Button>
        </Space>
      ))}
      <Button
        size="small"
        disabled={disabled || fields.length >= 64}
        onClick={() => update([...fields, { name: output ? `output_${fields.length + 1}` : `input_${fields.length + 1}`, type: 'string', required: true }])}
      >添加{output ? '输出' : '输入'}字段</Button>
    </fieldset>
  )
}

function StepEditor({ tool, step, index, operationCatalog, onChange, onRemove, onMove, disabled }) {
  const operation = operationFor(operationCatalog, step.operation_key)
  const references = availableReferences(tool, tool.steps, index)
  const setOperation = (operationKey) => {
    const nextOperation = operationFor(operationCatalog, operationKey)
    onChange({
      ...step,
      operation_key: operationKey,
      input_map: {},
      output_mappings: Object.fromEntries((nextOperation?.output_names || []).map((name) => [name, name])),
    })
  }
  const setInput = (name, reference) => {
    const next = { ...step.input_map }
    if (reference) next[name] = reference
    else delete next[name]
    onChange({ ...step, input_map: next })
  }
  const outputEntries = Object.entries(step.output_mappings || {})
  const setOutput = (outputIndex, name, operationOutput) => {
    const next = outputEntries.map(([currentName, currentOutput], entryIndex) => (
      entryIndex === outputIndex ? [name, operationOutput] : [currentName, currentOutput]
    )).filter(([alias]) => alias)
    onChange({ ...step, output_mappings: Object.fromEntries(next) })
  }
  return (
    <div className="declarative-tool">
      <Space wrap>
        <Text strong>步骤 {index + 1}</Text>
        <Button size="small" disabled={disabled || index === 0} onClick={() => onMove(-1)}>上移</Button>
        <Button size="small" disabled={disabled || index === tool.steps.length - 1} onClick={() => onMove(1)}>下移</Button>
        <Button danger size="small" disabled={disabled} onClick={onRemove}>移除步骤</Button>
      </Space>
      <Form.Item label={`步骤 ${index + 1} ID`} required>
        <Input aria-label={`步骤 ${index + 1} ID`} value={step.step_id} maxLength={64} disabled={disabled} onChange={(event) => onChange({ ...step, step_id: event.target.value })} />
      </Form.Item>
      <Form.Item label="服务器批准的操作" required>
        <Select
          aria-label={`步骤 ${index + 1} 操作`}
          value={step.operation_key || undefined}
          disabled={disabled}
          placeholder="选择已验证操作"
          options={operationCatalog.map((candidate) => ({
            value: candidate.operation_key,
            label: `${candidate.mcp_name} · ${candidate.operation_kind === 'write' ? '写' : '只读'}`,
          }))}
          onChange={setOperation}
        />
      </Form.Item>
      {operation && (
        <>
          <Text type="secondary">{operation.description || '后端未提供描述'}</Text>
          {Object.entries(operation.input_schema?.properties || {}).map(([name]) => (
            <Form.Item key={name} label={`操作输入 ${name}${operation.input_schema.required?.includes(name) ? '（必填）' : ''}`} required={operation.input_schema.required?.includes(name)}>
              <Select
                aria-label={`步骤 ${index + 1} 输入 ${name} 引用`}
                allowClear
                value={step.input_map?.[name]}
                disabled={disabled}
                placeholder="仅可选择声明值"
                options={references.map((reference) => ({ value: reference, label: reference }))}
                onChange={(reference) => setInput(name, reference)}
              />
            </Form.Item>
          ))}
          <fieldset disabled={disabled}>
            <legend>显式步骤输出映射</legend>
            {outputEntries.map(([name, operationOutput], outputIndex) => (
              <Space key={`${name}-${outputIndex}`} wrap>
                <Input
                  aria-label={`步骤 ${index + 1} 输出 ${outputIndex + 1} 名称`}
                  maxLength={128}
                  value={name}
                  placeholder="步骤输出名称"
                  onChange={(event) => setOutput(outputIndex, event.target.value, operationOutput)}
                />
                <Select
                  aria-label={`步骤 ${index + 1} 输出 ${outputIndex + 1} 操作值`}
                  value={operationOutput}
                  options={(operation.output_names || []).map((outputName) => ({ value: outputName, label: outputName }))}
                  onChange={(value) => setOutput(outputIndex, name, value)}
                />
                <Button danger size="small" onClick={() => onChange({
                  ...step,
                  output_mappings: Object.fromEntries(outputEntries.filter((_, entryIndex) => entryIndex !== outputIndex)),
                })}>移除输出</Button>
              </Space>
            ))}
            <Button
              size="small"
              disabled={outputEntries.length >= 32 || !(operation.output_names || []).length}
              onClick={() => onChange({
                ...step,
                output_mappings: {
                  ...step.output_mappings,
                  [`mapped_${outputEntries.length + 1}`]: operation.output_names[0],
                },
              })}
            >添加输出映射</Button>
          </fieldset>
        </>
      )}
    </div>
  )
}

export default function DeclarativeToolBuilder({ operationCatalog = [], tools = [], onChange, disabled = false }) {
  const replaceTool = (index, tool) => onChange(tools.map((item, itemIndex) => (itemIndex === index ? tool : item)))
  if (!operationCatalog.length) {
    return <Alert showIcon type="info" message="先验证原始规范" description="后端返回安全操作元数据后，才可创建多操作工具。" />
  }
  return (
    <div className="declarative-tool-list" aria-label="多操作工具构建器">
      {!tools.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未定义组合工具；将保持原始规范和单操作工具流程" />}
      {tools.map((tool, toolIndex) => {
        const errors = validateToolDraft(tool, operationCatalog)
        const summary = operationKindSummary(tool, operationCatalog)
        return (
          <div className="declarative-tool" key={`tool-${toolIndex}`}>
            <Space wrap>
              <Text strong>组合工具 {toolIndex + 1}</Text>
              <Tag color={summary.writeCount ? 'orange' : 'blue'}>{summary.label}</Tag>
              <Button danger size="small" disabled={disabled} onClick={() => onChange(tools.filter((_, index) => index !== toolIndex))}>移除组合工具</Button>
            </Space>
            {summary.warning && <Alert showIcon type="warning" message="包含写操作" description={summary.warning} />}
            <Form layout="vertical">
              <Form.Item label="工具 Key" required><Input aria-label={`组合工具 ${toolIndex + 1} Key`} value={tool.tool_key} maxLength={128} disabled={disabled} onChange={(event) => replaceTool(toolIndex, { ...tool, tool_key: event.target.value })} /></Form.Item>
              <Form.Item label="MCP 名称" required><Input aria-label={`组合工具 ${toolIndex + 1} MCP 名称`} value={tool.mcp_name} maxLength={128} disabled={disabled} onChange={(event) => replaceTool(toolIndex, { ...tool, mcp_name: event.target.value })} /></Form.Item>
              <Form.Item label="安全描述"><Input.TextArea aria-label={`组合工具 ${toolIndex + 1} 安全描述`} value={tool.description} maxLength={512} disabled={disabled} onChange={(event) => replaceTool(toolIndex, { ...tool, description: event.target.value })} /></Form.Item>
              <SchemaEditor label="工具输入 schema" schema={tool.input_schema} disabled={disabled} onChange={(inputSchema) => replaceTool(toolIndex, { ...tool, input_schema: inputSchema })} />
              {tool.steps.map((step, stepIndex) => (
                <StepEditor
                  key={`${step.step_id}-${stepIndex}`}
                  tool={tool}
                  step={step}
                  index={stepIndex}
                  operationCatalog={operationCatalog}
                  disabled={disabled}
                  onChange={(nextStep) => replaceTool(toolIndex, {
                    ...tool,
                    steps: tool.steps.map((item, index) => (index === stepIndex ? nextStep : item)),
                  })}
                  onRemove={() => replaceTool(toolIndex, { ...tool, steps: tool.steps.filter((_, index) => index !== stepIndex) })}
                  onMove={(offset) => {
                    const steps = [...tool.steps]
                    const [selected] = steps.splice(stepIndex, 1)
                    steps.splice(stepIndex + offset, 0, selected)
                    replaceTool(toolIndex, { ...tool, steps })
                  }}
                />
              ))}
              <Button
                disabled={disabled || tool.steps.length >= 16}
                onClick={() => replaceTool(toolIndex, {
                  ...tool,
                  steps: [...tool.steps, newStep(operationCatalog[0], tool.steps.length)],
                })}
              >添加有序步骤</Button>
              <SchemaEditor label="工具输出 schema" output schema={tool.output_schema} disabled={disabled} onChange={(outputSchema) => {
                const fields = Object.keys(outputSchema.properties)
                replaceTool(toolIndex, {
                  ...tool,
                  output_schema: outputSchema,
                  result_map: Object.fromEntries(fields.map((name) => [name, tool.result_map?.[name] || ''])),
                })
              }} />
              {Object.keys(tool.output_schema?.properties || {}).map((name) => (
                <Form.Item key={name} label={`最终输出 ${name}`} required>
                  <Select
                    aria-label={`组合工具 ${toolIndex + 1} 最终输出 ${name} 引用`}
                    value={tool.result_map?.[name] || undefined}
                    disabled={disabled}
                    placeholder="仅可选择已声明步骤输出"
                    options={availableReferences(tool, tool.steps, tool.steps.length)
                      .filter((reference) => reference.startsWith('$steps.'))
                      .map((reference) => ({ value: reference, label: reference }))}
                    onChange={(reference) => replaceTool(toolIndex, {
                      ...tool,
                      result_map: { ...tool.result_map, [name]: reference },
                    })}
                  />
                </Form.Item>
              ))}
            </Form>
            {errors.length > 0 && <Alert showIcon type="error" message="组合工具尚未完成" description={<ul>{errors.map((error) => <li key={error}>{error}</li>)}</ul>} />}
          </div>
        )
      })}
      <Button
        type="dashed"
        disabled={disabled || tools.length >= 64}
        onClick={() => onChange([...tools, blankTool(tools.length)])}
      >添加组合工具</Button>
    </div>
  )
}
