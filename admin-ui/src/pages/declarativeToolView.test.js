import test from 'node:test'
import assert from 'node:assert/strict'
import {
  availableReferences,
  buildMcpToolsExtension,
  createRevisionAllocator,
  createSourceGeneration,
  mergeJsonMcpToolsExtension,
  mergeMcpToolsExtension,
  mergeYamlMcpToolsExtension,
  operationKindSummary,
  safeOperationCatalog,
  validateToolDraft,
} from './declarativeToolView.js'

const operations = [
  {
    operation_key: 'people.lookup',
    mcp_name: 'people_lookup',
    description: 'Look up a person',
    operation_kind: 'read',
    input_schema: {
      type: 'object',
      properties: { email: { type: 'string' } },
      required: ['email'],
      additionalProperties: false,
    },
    output_names: ['entity_id'],
  },
  {
    operation_key: 'people.update',
    mcp_name: 'people_update',
    description: 'Update a person',
    operation_kind: 'write',
    input_schema: {
      type: 'object',
      properties: { id: { type: 'string' }, name: { type: 'string' } },
      required: ['id', 'name'],
      additionalProperties: false,
    },
    output_names: ['display_name'],
  },
]

const employeeProfileDraft = {
  tool_key: 'people.profile',
  mcp_name: 'people_profile',
  description: 'Find and update a public profile',
  input_schema: {
    type: 'object',
    properties: { email: { type: 'string' }, name: { type: 'string' } },
    required: ['email', 'name'],
    additionalProperties: false,
  },
  output_schema: {
    type: 'object',
    properties: { name: { type: 'string' } },
    required: ['name'],
    additionalProperties: false,
  },
  steps: [
    {
      step_id: 'find',
      operation_key: 'people.lookup',
      input_map: { email: '$input.email' },
      output_mappings: { user_id: 'entity_id' },
    },
    {
      step_id: 'profile',
      operation_key: 'people.update',
      input_map: { id: '$steps.find.user_id', name: '$input.name' },
      output_mappings: { public_name: 'display_name' },
    },
  ],
  result_map: { name: '$steps.profile.public_name' },
  operationCatalog: operations,
  uiExpanded: true,
}

test('builder serializes only the backend exact seven-key tool and four-key step shapes', () => {
  const extension = buildMcpToolsExtension([employeeProfileDraft])
  assert.deepEqual(Object.keys(extension[0]), [
    'tool_key', 'mcp_name', 'description', 'input_schema', 'output_schema', 'steps', 'result_map',
  ])
  assert.deepEqual(Object.keys(extension[0].steps[0]), [
    'step_id', 'operation_key', 'input_map', 'output_mappings',
  ])
  assert.equal(extension[0].steps[1].input_map.id, '$steps.find.user_id')
  assert.equal(JSON.stringify(extension).includes('${'), false)
  assert.equal(JSON.stringify(extension).includes('uiExpanded'), false)
  assert.equal(JSON.stringify(extension).includes('operationCatalog'), false)
})

test('available references expose declared inputs and mapped outputs from prior steps only', () => {
  assert.deepEqual(availableReferences(employeeProfileDraft, employeeProfileDraft.steps, 1), [
    '$input.email', '$input.name', '$steps.find.user_id',
  ])
  assert.deepEqual(availableReferences(employeeProfileDraft, employeeProfileDraft.steps, 0), [
    '$input.email', '$input.name',
  ])
})

test('builder rejects forward, unknown, embedded, and expression references before submit', () => {
  for (const reference of [
    '$steps.profile.public_name',
    '$steps.missing.user_id',
    'prefix $input.email',
    '${input.email}',
  ]) {
    const draft = structuredClone(employeeProfileDraft)
    draft.steps[0].input_map.email = reference
    assert.ok(validateToolDraft(draft, operations).some((error) => error.includes('引用')))
  }
})

test('builder rejects duplicate identifiers across tools and inside ordered steps', () => {
  const duplicateStep = structuredClone(employeeProfileDraft)
  duplicateStep.steps[1].step_id = 'find'
  assert.ok(validateToolDraft(duplicateStep, operations).some((error) => error.includes('步骤 ID')))
  assert.throws(
    () => buildMcpToolsExtension([employeeProfileDraft, { ...employeeProfileDraft }]),
    /工具 Key|MCP 名称/,
  )
})

test('tool key and MCP name share one namespace across tools but may match within one tool', () => {
  const sameWithinTool = { ...employeeProfileDraft, tool_key: 'people.profile', mcp_name: 'people.profile' }
  assert.equal(buildMcpToolsExtension([sameWithinTool])[0].mcp_name, 'people.profile')

  const second = structuredClone(employeeProfileDraft)
  second.tool_key = employeeProfileDraft.mcp_name
  second.mcp_name = 'people_profile_second'
  assert.throws(() => buildMcpToolsExtension([employeeProfileDraft, second]), /命名空间/)
})

test('builder fails closed on incomplete operation inputs and unknown operation outputs', () => {
  const missing = structuredClone(employeeProfileDraft)
  delete missing.steps[1].input_map.name
  assert.ok(validateToolDraft(missing, operations).some((error) => error.includes('必填输入 name')))
  const unknownOutput = structuredClone(employeeProfileDraft)
  unknownOutput.steps[0].output_mappings.user_id = 'private_id'
  assert.ok(validateToolDraft(unknownOutput, operations).some((error) => error.includes('未知输出')))
})

test('builder rejects invalid closed schemas and incomplete final result mappings', () => {
  const open = structuredClone(employeeProfileDraft)
  open.input_schema.additionalProperties = true
  assert.ok(validateToolDraft(open, operations).some((error) => error.includes('输入 schema')))
  const incomplete = structuredClone(employeeProfileDraft)
  incomplete.result_map = {}
  assert.ok(validateToolDraft(incomplete, operations).some((error) => error.includes('最终输出')))
  const undeclared = structuredClone(employeeProfileDraft)
  undeclared.result_map.name = '$steps.find.missing'
  assert.ok(validateToolDraft(undeclared, operations).some((error) => error.includes('最终输出')))
  const undefinedSchemaValue = structuredClone(employeeProfileDraft)
  undefinedSchemaValue.input_schema.properties.email.default = undefined
  assert.ok(validateToolDraft(undefinedSchemaValue, operations).some((error) => error.includes('输入 schema')))
})

test('metadata missing an exact safe field fails closed for a selected operation', () => {
  const unsafeCatalog = operations.map((operation) => ({ ...operation }))
  delete unsafeCatalog[0].output_names
  assert.ok(validateToolDraft(employeeProfileDraft, unsafeCatalog).some((error) => error.includes('安全元数据')))
})

test('safe operation catalog keeps only the six allowlisted fields and rejects duplicates', () => {
  const safe = safeOperationCatalog([{ ...operations[0], path: '/private', authorization: 'secret' }])
  assert.deepEqual(Object.keys(safe[0]), [
    'operation_key', 'mcp_name', 'description', 'operation_kind', 'input_schema', 'output_names',
  ])
  assert.equal(JSON.stringify(safe).includes('/private'), false)
  assert.throws(() => safeOperationCatalog([operations[0], operations[0]]), /重复/)
})

test('operation key and MCP name share one catalog namespace and duplicate MCP names fail', () => {
  const sameWithinOperation = { ...operations[0], mcp_name: operations[0].operation_key }
  assert.equal(safeOperationCatalog([sameWithinOperation])[0].mcp_name, operations[0].operation_key)

  const crossConflict = [
    { ...operations[0], mcp_name: 'shared.operation' },
    { ...operations[1], operation_key: 'shared.operation' },
  ]
  assert.throws(() => safeOperationCatalog(crossConflict), /命名空间/)
  assert.throws(() => safeOperationCatalog([
    operations[0],
    { ...operations[1], mcp_name: operations[0].mcp_name },
  ]), /命名空间/)
})

test('zero composites preserve the original source byte-for-byte', () => {
  const source = '{\n  "openapi": "3.0.3",\n  "paths": {}\n}\n'
  assert.equal(mergeMcpToolsExtension(source, 'json', []), source)
  assert.equal(mergeMcpToolsExtension(source, 'yaml', []), source)
})

test('JSON merge preserves the document semantics and replaces one root extension', () => {
  const source = JSON.stringify({
    openapi: '3.0.3',
    info: { title: 'People', 'x-note': 'kept' },
    'x-mcp-tools': [{ old: true }],
    paths: {},
  }, null, 2)
  const merged = JSON.parse(mergeJsonMcpToolsExtension(source, [employeeProfileDraft]))
  assert.equal(merged.info['x-note'], 'kept')
  assert.equal(merged['x-mcp-tools'].length, 1)
  assert.equal(merged['x-mcp-tools'][0].tool_key, 'people.profile')
})

test('JSON merge rejects invalid, non-object, duplicate-key, and non-finite documents', () => {
  assert.throws(() => mergeJsonMcpToolsExtension('{', [employeeProfileDraft]), /JSON/)
  assert.throws(() => mergeJsonMcpToolsExtension('[]', [employeeProfileDraft]), /对象/)
  assert.throws(() => mergeJsonMcpToolsExtension('{"openapi":"3","openapi":"4"}', [employeeProfileDraft]), /重复/)
  assert.throws(() => mergeJsonMcpToolsExtension('{"n":1e999}', [employeeProfileDraft]), /有限/)
})

test('YAML merge appends or replaces exactly one unique root extension and preserves unrelated text', () => {
  const source = 'openapi: 3.0.3\ninfo:\n  title: People\npaths: {}\n'
  const appended = mergeYamlMcpToolsExtension(source, [employeeProfileDraft])
  assert.ok(appended.startsWith(source))
  assert.equal((appended.match(/^x-mcp-tools:/gm) || []).length, 1)
  assert.ok(appended.includes('"tool_key":"people.profile"'))

  const replaced = mergeYamlMcpToolsExtension(
    'openapi: 3.0.3\nx-mcp-tools: [{"old":true}]\npaths: {}\n',
    [employeeProfileDraft],
  )
  assert.ok(replaced.includes('paths: {}'))
  assert.equal(replaced.includes('"old":true'), false)
  assert.equal((replaced.match(/^x-mcp-tools:/gm) || []).length, 1)
})

test('YAML replacement preserves an immediately following root comment for flow and block extensions', () => {
  const flow = mergeYamlMcpToolsExtension(
    'openapi: 3.0.3\nx-mcp-tools: [{"old":true}]\n# keep flow comment\npaths: {}\n',
    [employeeProfileDraft],
  )
  assert.ok(flow.includes('\n# keep flow comment\npaths: {}'))

  const block = mergeYamlMcpToolsExtension(
    'openapi: 3.0.3\nx-mcp-tools:\n  - tool_key: old\n# keep block comment\npaths: {}\n',
    [employeeProfileDraft],
  )
  assert.ok(block.includes('\n# keep block comment\npaths: {}'))
})

test('YAML replacement consumes a complete multiline flow extension and preserves following roots', () => {
  const merged = mergeYamlMcpToolsExtension(
    'openapi: 3.0.3\nx-mcp-tools: [\n  {"old": true}\n]\n# keep multiline flow comment\npaths: {}\n',
    [employeeProfileDraft],
  )
  assert.equal(merged.includes('{"old": true}'), false)
  assert.equal((merged.match(/^x-mcp-tools:/gm) || []).length, 1)
  assert.ok(merged.includes('\n# keep multiline flow comment\npaths: {}'))
})

test('YAML multiline flow boundary handles nesting, quoted brackets, escapes, doubled quotes, and comments', () => {
  const source = [
    'openapi: 3.0.3',
    'x-mcp-tools: [',
    '  {"nested": [1, {"double": "] } \\\"still quoted"}]}, # ] ignored in comment',
    "  {'single': 'it''s still ] and } quoted', plain: value#not-a-comment}",
    ']',
    '# keep complex flow comment',
    'paths: {}',
    '',
  ].join('\n')
  const merged = mergeYamlMcpToolsExtension(source, [employeeProfileDraft])
  assert.equal(merged.includes('still quoted'), false)
  assert.ok(merged.includes('\n# keep complex flow comment\npaths: {}'))
})

test('YAML multiline flow treats apostrophes and double quotes inside plain scalars as plain text', () => {
  for (const value of ["can't", 'a"b']) {
    const merged = mergeYamlMcpToolsExtension(
      `openapi: 3.0.3\nx-mcp-tools: [\n  {description: ${value}, old: true}\n]\n# keep plain scalar comment\npaths: {}\n`,
      [employeeProfileDraft],
    )
    assert.equal(merged.includes(`description: ${value}`), false)
    assert.ok(merged.includes('\n# keep plain scalar comment\npaths: {}'))
  }
})

test('YAML replacement rejects non-comment trailing tokens after the outer flow close', () => {
  for (const trailing of [']', '}']) {
    assert.throws(() => mergeYamlMcpToolsExtension(
      `openapi: 3.0.3\nx-mcp-tools: [{old: true}]${trailing}\npaths: {}\n`,
      [employeeProfileDraft],
    ), /YAML.*尾随/)
  }
})

test('YAML replacement rejects an unclosed multiline flow extension locally', () => {
  assert.throws(() => mergeYamlMcpToolsExtension(
    'openapi: 3.0.3\nx-mcp-tools: [\n  {"old": true}\n# never closed\npaths: {}\n',
    [employeeProfileDraft],
  ), /YAML.*未闭合/)
})

test('YAML merge fails closed on duplicate, multidocument, and indented extensions', () => {
  for (const source of [
    'openapi: 3.0.3\nx-mcp-tools: []\nx-mcp-tools: []\n',
    'openapi: 3.0.3\n---\nopenapi: 3.0.3\n',
    'openapi: 3.0.3\ninfo:\n  x-mcp-tools: []\n',
  ]) assert.throws(() => mergeYamlMcpToolsExtension(source, [employeeProfileDraft]), /YAML/)
})

test('source generation tickets reject superseded and explicitly invalidated async responses', () => {
  const generation = createSourceGeneration()
  const originalValidation = generation.begin({ specId: 'people', revision: 2, phase: 'source-validate' })
  const mergedImport = generation.begin({ specId: 'people', revision: 3, phase: 'merged-import' })
  assert.equal(generation.isCurrent(originalValidation), false)
  assert.equal(generation.isCurrent(mergedImport), true)
  generation.invalidate()
  assert.equal(generation.isCurrent(mergedImport), false)
})

test('revision allocator reserves before requests so a lost response cannot reuse a revision', () => {
  const revisions = createRevisionAllocator(2)
  const persistedButLost = revisions.reserve()
  const retry = revisions.reserve()
  assert.equal(persistedButLost, 3)
  assert.equal(retry, 4)
  assert.equal(revisions.capture(), 4)
  revisions.reset(10)
  assert.equal(revisions.reserve(), 11)
})

test('draft validation permits zero or one write step and rejects two write steps', () => {
  assert.equal(validateToolDraft(employeeProfileDraft, operations).some((error) => error.includes('写步骤')), false)
  const allRead = operations.map((operation) => ({ ...operation, operation_kind: 'read' }))
  assert.equal(validateToolDraft(employeeProfileDraft, allRead).some((error) => error.includes('写步骤')), false)

  const twoWrites = structuredClone(employeeProfileDraft)
  twoWrites.steps.push({
    step_id: 'profile_again',
    operation_key: 'people.update',
    input_map: { id: '$steps.find.user_id', name: '$input.name' },
    output_mappings: { second_name: 'display_name' },
  })
  assert.ok(validateToolDraft(twoWrites, operations).some((error) => error.includes('最多包含一个写步骤')))
})

test('read/write summary is explicit and warns whenever a selected step writes', () => {
  assert.deepEqual(operationKindSummary(employeeProfileDraft, operations), {
    readCount: 1,
    writeCount: 1,
    operationKind: 'write',
    label: '1 个只读步骤，1 个写步骤',
    warning: '此组合工具会写入上游系统，启用时需要明确同意。',
  })
  assert.equal(operationKindSummary({ ...employeeProfileDraft, steps: employeeProfileDraft.steps.slice(0, 1) }, operations).warning, '')
})
