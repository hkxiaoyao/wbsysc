# OpenAPI Multi-Operation Tool Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow one declarative MCP tool to execute 1–N OpenAPI operations sequentially with bounded input references, prior-step references, explicit result mapping, and fail-fast behavior.

**Architecture:** Keep OpenAPI operations as an internal API library and add immutable `DeclarativeTool` and `DeclarativeStep` models to each revision. A closed `ValueRef` parser replaces arbitrary expression evaluation; the connector executes steps through the existing safe HTTP boundary and exposes only declarative tools through `ConnectorSpec`.

**Tech Stack:** Python 3.11+, dataclasses, Pydantic validation boundaries, SafeHttpClient/httpx, PyYAML safe loader, pytest.

**Depends on:** All tasks in `2026-07-17-mcp-service-runtime.md`.
**Blocks:** Declarative builder and final rollout.
**Hot-file ownership:** This plan is the sole writer of `app/connectors/declarative/**`, `app/admin_connections.py`, and `app/mcp_log_models.py` until its commits are merged.

## Global Constraints

- Only `$input.<field>` and `$steps.<previous_step_id>.<declared_output_field>` references are accepted.
- No arbitrary JSONPath, interpolation, templates, scripts, functions, conditions, loops, or parallel steps.
- Steps execute sequentially and fail fast; final output always uses an explicit `result_map`.
- A tool may contain only read steps or at most one explicitly enabled write step.
- Every step retains the existing HTTPS, allowlist, DNS/IP, redirect, request-size, response-size, and timeout controls.
- Published revisions remain immutable; orchestration changes create a new revision.
- Existing OpenAPI documents without composite-tool declarations retain one operation per MCP tool.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `app/connectors/declarative/models.py` | Value references, steps, tools, validation, storage round-trip. |
| `app/connectors/declarative/validator.py` | Compile `x-mcp-tools` and backward-compatible single-step tools. |
| `app/connectors/declarative/connector.py` | Sequential step executor and safe result mapper. |
| `app/connectors/declarative/provider.py` | Expose composite tools through the existing provider. |
| `tests/test_declarative_orchestration_*.py` | Model, import, execution, security, and persistence tests. |

### Task 1: Closed Value References and Immutable Tool Models

**Files:**
- Modify: `app/connectors/declarative/models.py`
- Test: `tests/test_declarative_orchestration_models.py`

**Interfaces:**
- Produces `ValueRef.parse`, `DeclarativeStep`, `DeclarativeTool`, and `DeclarativeRevision.tool_for`.
- Extends `DeclarativeRevision` with `tools: tuple[DeclarativeTool, ...]`.

- [ ] **Step 1: Write failing reference-language and dependency tests.**

```python
@pytest.mark.parametrize("value", [
    "$input.mobile", "$steps.find_user.user_id",
])
def test_value_ref_accepts_only_closed_forms(value):
    assert ValueRef.parse(value).raw == value

@pytest.mark.parametrize("value", [
    "${input.mobile}", "$steps.current.value", "$steps.a.value + 1",
    "$input", "$steps.a", "$.arbitrary.jsonpath",
])
def test_value_ref_rejects_templates_and_expressions(value):
    with pytest.raises(SpecValidationError):
        ValueRef.parse(value)

def test_tool_rejects_forward_step_reference():
    with pytest.raises(SpecValidationError, match="previous step"):
        declarative_tool(steps=[step("first", {"id": "$steps.second.id"}), step("second", {})])
```

- [ ] **Step 2: Run model tests and verify missing types.**

Run: `python -m pytest tests/test_declarative_orchestration_models.py -q`
Expected: FAIL importing `ValueRef` and `DeclarativeTool`.

- [ ] **Step 3: Implement the closed parser and immutable models.**

```python
@dataclass(frozen=True)
class ValueRef:
    raw: str
    source: Literal["input", "step"]
    field: str
    step_id: str = ""

    @classmethod
    def parse(cls, raw: str) -> "ValueRef":
        input_match = re.fullmatch(r"\$input\.([A-Za-z][A-Za-z0-9_.-]{0,127})", raw)
        if input_match:
            return cls(raw=raw, source="input", field=input_match.group(1))
        step_match = re.fullmatch(
            r"\$steps\.([A-Za-z][A-Za-z0-9_.-]{0,63})\.([A-Za-z][A-Za-z0-9_.-]{0,127})", raw
        )
        if step_match:
            return cls(raw=raw, source="step", step_id=step_match.group(1), field=step_match.group(2))
        raise SpecValidationError("invalid value reference")

@dataclass(frozen=True)
class DeclarativeStep:
    step_id: str
    operation_key: str
    input_map: Mapping[str, ValueRef]
    output_mappings: Mapping[str, str]
    timeout_ms: int | None = None

@dataclass(frozen=True)
class DeclarativeTool:
    tool_key: str
    mcp_name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    steps: tuple[DeclarativeStep, ...]
    result_map: Mapping[str, ValueRef]
```

`output_mappings` maps a step-local output name to an output field already declared by the selected operation; it cannot expose arbitrary response fields or pointers. Validate at most 16 steps, unique step IDs, references only to declared input fields or earlier step `output_mappings` names, explicit final output fields, a 60-second total timeout cap, and at most one write operation.

- [ ] **Step 4: Make `connector_spec()` enumerate tools rather than operations.**

```python
def tool_for(self, tool_key: str) -> DeclarativeTool:
    for tool in self.tools:
        if tool_key in {tool.tool_key, tool.mcp_name}:
            return tool
    raise UnknownToolError("unknown declarative tool")
```

Run: `python -m pytest tests/test_declarative_orchestration_models.py tests/test_declarative_provider.py -q`
Expected: PASS.

- [ ] **Step 5: Commit the orchestration model.**

```bash
git add app/connectors/declarative/models.py tests/test_declarative_orchestration_models.py tests/test_declarative_provider.py
git commit -m "feat: model declarative multi-operation tools"
```

### Task 2: OpenAPI Import and Storage Round-Trip

**Files:**
- Modify: `app/connectors/declarative/validator.py`
- Modify: `app/connectors/declarative/models.py`
- Test: `tests/test_declarative_orchestration_import.py`
- Test: `tests/test_declarative_provider.py`

**Interfaces:**
- Accepts a root `x-mcp-tools` array in imported OpenAPI JSON/YAML.
- Persists `tools` beside `operations` in the immutable revision document.

- [ ] **Step 1: Write failing import and persistence tests.**

```python
def test_import_compiles_multi_operation_tool():
    revision = import_openapi_revision(openapi_with_tools([
        {
            "tool_key": "employee.profile",
            "mcp_name": "employee_profile",
            "description": "查询员工完整档案",
            "input_schema": {"type": "object", "properties": {"mobile": {"type": "string"}}, "required": ["mobile"]},
            "steps": [
                {"step_id": "find", "operation_key": "users.find",
                 "input_map": {"mobile": "$input.mobile"},
                 "output_mappings": {"user_id": "user_id"}},
                {"step_id": "profile", "operation_key": "users.get",
                 "input_map": {"user_id": "$steps.find.user_id"},
                 "output_mappings": {"name": "name"}},
            ],
            "result_map": {"name": "$steps.profile.name"},
        }
    ]), tenant_id="tenant-a", connection_id="conn-a")
    assert revision.tools[0].steps[1].operation_key == "users.get"
    restored = restore(revision.storage_document())
    assert restored == revision
```

- [ ] **Step 2: Run import tests and verify `x-mcp-tools` is not compiled.**

Run: `python -m pytest tests/test_declarative_orchestration_import.py -q`
Expected: FAIL because the imported revision has no composite tools.

- [ ] **Step 3: Compile the extension with exact keys and fail-closed validation.**

```python
def _compile_tools(document: Mapping[str, Any], operations: tuple[DeclarativeOperation, ...]) -> tuple[DeclarativeTool, ...]:
    raw_tools = document.get("x-mcp-tools")
    if raw_tools is None:
        return tuple(_single_step_tool(operation) for operation in operations)
    if not isinstance(raw_tools, list) or not raw_tools or len(raw_tools) > MAX_OPERATION_COUNT:
        raise SpecValidationError("invalid x-mcp-tools")
    return tuple(_compile_tool(item, operations) for item in raw_tools)
```

Reject unknown keys at every extension level. Each step accepts exactly `step_id`, `operation_key`, `input_map`, `output_mappings`, and optional `timeout_ms`. Resolve every `operation_key` against the same revision. When the extension is absent, generate an explicit single-step tool whose input map, output mappings, and result map preserve current one-operation behavior.

- [ ] **Step 4: Extend storage and restore with mandatory compiled `tools`.**

Persist only the compiled, credential-free structure. Evolve the exact stored root keys from `{base_url, allowed_hosts, auth_scheme, sync_spec, operations}` to the same set plus `tools`; accept the old exact set only for backward restoration, and reject every other unknown root or nested key. For old stored revisions without `tools`, reconstruct deterministic single-step tools during restore; a subsequent write emits the new format.

Parse ValueRef fields into typed objects before serializing them. Continue applying `assert_safe_declaration_value()` to every non-reference string; `$input.` and `$steps.` are allowed only at the exact `input_map` and `result_map` value positions, never in descriptions, URLs, schemas, headers, or other free text.

Run: `python -m pytest tests/test_declarative_orchestration_import.py tests/test_declarative_provider.py tests/test_admin_connections.py -q`
Expected: PASS.

- [ ] **Step 5: Commit import and compatibility behavior.**

```bash
git add app/connectors/declarative/models.py app/connectors/declarative/validator.py tests/test_declarative_orchestration_import.py tests/test_declarative_provider.py tests/test_admin_connections.py
git commit -m "feat: import declarative composite tools"
```

### Task 3: Sequential Fail-Fast Executor

**Files:**
- Modify: `app/connectors/declarative/connector.py`
- Test: `tests/test_declarative_orchestration_execution.py`
- Test: `tests/test_declarative_http_safety.py`

**Interfaces:**
- Adds `_execute_operation(context, operation, args) -> ExecutionResult`.
- Adds `_resolve_ref(ref, tool_args, step_outputs) -> Any`.
- `execute()` now resolves a `DeclarativeTool` and executes its ordered steps.

- [ ] **Step 1: Write failing success, fail-fast, and output-minimization tests.**

```python
@pytest.mark.asyncio
async def test_composite_tool_feeds_previous_output_and_maps_final_result():
    result = await connector.execute(context, "employee.profile", {"mobile": "13800000000"})
    assert result.status == "ok"
    assert result.data == {"name": "张三", "department": "研发"}
    assert fake_client.requests[1].url.endswith("/users/u-123")

@pytest.mark.asyncio
async def test_step_failure_stops_later_requests_and_returns_generic_error():
    fake_client.responses[0] = response(500, {"secret": "must-not-leak"})
    result = await connector.execute(context, "employee.profile", {"mobile": "13800000000"})
    assert result == ExecutionResult(data={"error": "declarative operation failed"}, status="error")
    assert len(fake_client.requests) == 1
```

- [ ] **Step 2: Run executor tests and verify current one-operation behavior fails them.**

Run: `python -m pytest tests/test_declarative_orchestration_execution.py -q`
Expected: FAIL because `execute()` looks up an operation directly.

- [ ] **Step 3: Refactor one safe operation call and add the sequential loop.**

```python
async def execute(self, context: ConnectionContext, tool_key: str,
                  args: dict[str, Any]) -> ExecutionResult:
    revision = self._revision_for_context(context)
    tool = revision.tool_for(tool_key)
    outputs: dict[str, dict[str, Any]] = {}
    try:
        for step in tool.steps:
            operation = revision.operation_for(step.operation_key)
            step_args = {
                target: self._resolve_ref(ref, args, outputs)
                for target, ref in step.input_map.items()
            }
            result = await self._execute_operation(context, operation, step_args,
                                                   timeout_ms=step.timeout_ms)
            if result.status != "ok":
                return self._error()
            outputs[step.step_id] = {
                name: result.data[source_field]
                for name, source_field in step.output_mappings.items()
            }
        return ExecutionResult.ok({
            name: self._resolve_ref(ref, args, outputs)
            for name, ref in tool.result_map.items()
        })
    except UnknownToolError:
        raise
    except Exception:
        return self._error()
```

Before executing, validate that every `source_field` is declared by the operation and every later `$steps` reference targets a step-local mapped name. Wrap the whole tool in a 60-second global timeout cap in addition to the existing per-step timeout. Do not keep raw HTTP responses after each safe output extraction.

- [ ] **Step 4: Run execution and HTTP security regressions.**

Run: `python -m pytest tests/test_declarative_orchestration_execution.py tests/test_declarative_connector.py tests/test_declarative_http_safety.py -q`
Expected: PASS.

- [ ] **Step 5: Commit the executor.**

```bash
git add app/connectors/declarative/connector.py tests/test_declarative_orchestration_execution.py tests/test_declarative_connector.py tests/test_declarative_http_safety.py
git commit -m "feat: execute declarative tools sequentially"
```

### Task 4: Step-Level Safe Audit and Admin Validation

**Files:**
- Modify: `app/connectors/declarative/connector.py`
- Modify: `app/admin_connections.py`
- Modify: `app/mcp_log_models.py`
- Test: `tests/test_declarative_orchestration_audit.py`
- Test: `tests/test_admin_connections.py`

**Interfaces:**
- Adds an injected `StepAuditSink` receiving identifiers, status, error code, and elapsed milliseconds only.
- Import/validate responses include a safe tool-and-step preview.

- [ ] **Step 1: Write failing audit-redaction and preview tests.**

```python
@pytest.mark.asyncio
async def test_step_audit_contains_identifiers_but_no_payloads():
    await connector.execute(context, "employee.profile", {"mobile": "13800000000"})
    assert audit.events[0].step_id == "find"
    assert audit.events[0].operation_key == "users.find"
    assert "13800000000" not in repr(audit.events)
    assert "secret" not in repr(audit.events).lower()

def test_validate_preview_lists_tool_steps_without_transport_secrets(client):
    body = client.post(validate_url, json=document).json()
    assert body["tools"][0]["steps"] == ["find", "profile"]
    assert "Authorization" not in repr(body)
```

- [ ] **Step 2: Run focused tests and verify audit/preview absence.**

Run: `python -m pytest tests/test_declarative_orchestration_audit.py tests/test_admin_connections.py -q`
Expected: FAIL.

- [ ] **Step 3: Emit bounded step events and safe management metadata.**

```python
@dataclass(frozen=True)
class StepAuditEvent:
    connection_id: str
    tool_key: str
    step_id: str
    operation_key: str
    status: Literal["ok", "error"]
    error_code: str
    cost_ms: int
```

Construct events from server-side revision identities only. The admin preview returns tool names, descriptions, input/output schemas, step IDs, operation keys, and read/write types; it never returns credentials or runtime response samples.

- [ ] **Step 4: Run the complete declarative suite.**

Run: `python -m pytest tests/test_declarative_connector.py tests/test_declarative_http_safety.py tests/test_declarative_provider.py tests/test_declarative_orchestration_models.py tests/test_declarative_orchestration_import.py tests/test_declarative_orchestration_execution.py tests/test_declarative_orchestration_audit.py tests/test_admin_connections.py -q`
Expected: PASS.

- [ ] **Step 5: Commit audit and preview support.**

```bash
git add app/connectors/declarative app/admin_connections.py app/mcp_log_models.py tests/test_declarative_orchestration_audit.py tests/test_admin_connections.py
git commit -m "feat: audit declarative tool steps"
```
