import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from app.connections.models import ConnectionRecord
from app.connectors.contracts import (
    ConnectorSpec,
    ExecutionResult,
    SyncResult,
    ToolSpec,
)
from app.connectors.discovery import (
    ConnectorDiscoveryFailure,
    ConnectorDiscoveryError,
    ConnectorDiscoveryResult,
    ValidatedConnector,
    discover_connector_packages,
    discover_trusted_connectors,
    register_discovered_connectors,
    validate_active_connector_dependencies,
)
from app.connectors.registry import ConnectorRegistry, validate_connector_manifest


TOOL = ToolSpec(
    tool_key="messages.list",
    mcp_name="feishu_list_messages",
    description="List messages.",
    input_schema={"type": "object"},
    output_schema={"type": "object"},
    operation_kind="read",
    default_timeout_ms=1000,
    cache_ttl_seconds=None,
)


class FakeConnector:
    def __init__(self, connector_key="feishu", version="1.0.0", tools=(TOOL,)):
        self._spec = ConnectorSpec(
            connector_key=connector_key,
            version=version,
            tools=tools,
        )

    def spec(self):
        return self._spec

    async def execute(self, context, tool_key, args):
        return ExecutionResult.ok({})

    async def sync(self, context, resource_key):
        return SyncResult.ok(context.connection_id, resource_key)


class FakeEntryPoint:
    group = "wbsysc.connectors"

    def __init__(self, name, connector=None, error=None):
        self.name = name
        self.value = f"reviewed_{name}:connector"
        self._connector = connector or FakeConnector(name.replace("-", "_"))
        self._error = error
        self.loads = 0

    def load(self):
        self.loads += 1
        if self._error is not None:
            raise self._error
        return lambda: self._connector


def configure(monkeypatch, allowlist, entrypoints):
    from app.connectors import discovery

    monkeypatch.setattr(
        discovery,
        "get_settings",
        lambda: SimpleNamespace(connector_allowlist=allowlist),
    )
    monkeypatch.setattr(discovery, "entry_points", lambda *, group: entrypoints)


def test_discovery_loads_only_allowlisted_entry_points(monkeypatch):
    trusted_ep = FakeEntryPoint("feishu")
    untrusted_ep = FakeEntryPoint(
        "untrusted", error=AssertionError("untrusted package executed")
    )
    configure(monkeypatch, "feishu", [trusted_ep, untrusted_ep])

    connectors = discover_trusted_connectors()

    assert [connector.spec().connector_key for connector in connectors] == ["feishu"]
    assert trusted_ep.loads == 1
    assert untrusted_ep.loads == 0


def test_discovery_normalizes_allowlist_names_before_exact_matching(monkeypatch):
    trusted_ep = FakeEntryPoint(
        "acme-connector", connector=FakeConnector("acme_connector")
    )
    prefix_ep = FakeEntryPoint(
        "acme", error=AssertionError("prefix match must not execute")
    )
    configure(monkeypatch, " ACME_connector ", [trusted_ep, prefix_ep])

    assert [c.spec().connector_key for c in discover_trusted_connectors()] == [
        "acme_connector"
    ]
    assert prefix_ep.loads == 0


def test_discovery_never_loads_an_unsafe_entry_point_name(monkeypatch):
    unsafe = FakeEntryPoint(
        "feishu\ncredential=value",
        error=AssertionError("unsafe package metadata executed"),
    )
    configure(monkeypatch, unsafe.name, [unsafe])

    assert discover_trusted_connectors() == []
    assert unsafe.loads == 0


def test_discovery_rejects_missing_manifest_version(monkeypatch):
    configure(
        monkeypatch,
        "feishu",
        [FakeEntryPoint("feishu", connector=FakeConnector(version=""))],
    )

    with pytest.raises(ConnectorDiscoveryError, match="version"):
        discover_trusted_connectors()


def test_discovery_rejects_malformed_manifest_version(monkeypatch):
    configure(
        monkeypatch,
        "feishu",
        [FakeEntryPoint("feishu", connector=FakeConnector(version="latest"))],
    )

    with pytest.raises(ConnectorDiscoveryError, match="version"):
        discover_trusted_connectors()


def test_discovery_rejects_an_incompatible_connector_contract(monkeypatch):
    class ManifestOnlyConnector:
        def spec(self):
            return ConnectorSpec(connector_key="feishu", version="1", tools=())

    configure(
        monkeypatch,
        "feishu",
        [FakeEntryPoint("feishu", connector=ManifestOnlyConnector())],
    )

    with pytest.raises(ConnectorDiscoveryError, match="contract"):
        discover_trusted_connectors()


def test_discovery_rejects_invalid_entry_point_identity(monkeypatch):
    configure(
        monkeypatch,
        "feishu",
        [FakeEntryPoint("feishu", connector=FakeConnector("different"))],
    )

    with pytest.raises(ConnectorDiscoveryError, match="identity"):
        discover_trusted_connectors()


def test_discovery_rejects_duplicate_connector_and_tool_identities(monkeypatch):
    duplicate_tool = SimpleNamespace(
        tool_key="messages.list",
        mcp_name="messages.list",
    )
    malformed_spec = SimpleNamespace(
        connector_key="feishu",
        version="1.0.0",
        tools=(duplicate_tool, duplicate_tool),
    )
    connector = FakeConnector()
    connector._spec = malformed_spec
    configure(monkeypatch, "feishu", [FakeEntryPoint("feishu", connector=connector)])

    with pytest.raises(ConnectorDiscoveryError, match="manifest"):
        discover_trusted_connectors()


def test_discovery_wraps_load_errors_without_leaking_exception_text(monkeypatch):
    marker = "credential=do-not-log"
    configure(
        monkeypatch,
        "feishu",
        [FakeEntryPoint("feishu", error=RuntimeError(marker))],
    )

    with pytest.raises(ConnectorDiscoveryError) as raised:
        discover_trusted_connectors()

    assert marker not in str(raised.value)
    assert marker not in repr(raised.value)


def test_active_dependency_validation_fails_only_for_allowlisted_connectors():
    def active(key):
        return ConnectionRecord(
            connection_id=f"conn-{key}",
            tenant_id="tenant-a",
            connector_key=key,
            display_name=key,
            status="active",
            data_mode="direct",
            public_config={},
            config_version=1,
        )

    disabled = ConnectionRecord(**{**active("feishu").__dict__, "status": "disabled"})

    validate_active_connector_dependencies(
        [active("untrusted"), disabled], [], allowlist="feishu"
    )
    with pytest.raises(ConnectorDiscoveryError, match="active connection"):
        validate_active_connector_dependencies(
            [active("feishu")], [], allowlist="feishu"
        )


def test_discovery_rejects_duplicate_entry_point_names_before_loading(monkeypatch):
    first = FakeEntryPoint("feishu")
    second = FakeEntryPoint("FEISHU")
    configure(monkeypatch, "feishu", [first, second])

    with pytest.raises(ConnectorDiscoveryError, match="duplicate"):
        discover_trusted_connectors()

    assert first.loads == 0
    assert second.loads == 0


def test_registry_rejects_builtin_package_cross_namespace_collision_before_mutation():
    builtin = FakeConnector(
        "wecom",
        tools=(
            ToolSpec(
                **{
                    **TOOL.__dict__,
                    "tool_key": "reports.list",
                    "mcp_name": "wecom_list_reports",
                }
            ),
        ),
    )
    package = FakeConnector(
        "feishu",
        tools=(
            ToolSpec(
                **{
                    **TOOL.__dict__,
                    "tool_key": "feishu.reports",
                    "mcp_name": "wecom_list_reports",
                }
            ),
        ),
    )
    registry = ConnectorRegistry([builtin])

    with pytest.raises(ValueError, match="duplicate tool identifier"):
        registry.register(package)

    assert tuple(registry.registered) == ("wecom",)


def test_separate_discovery_registration_rejects_package_collision():
    first = FakeConnector("feishu")
    colliding = FakeConnector(
        "lark",
        tools=(ToolSpec(**{**TOOL.__dict__, "tool_key": "lark.messages"}),),
    )
    registry = ConnectorRegistry()

    registry.register(first)
    with pytest.raises(ValueError, match="duplicate tool identifier"):
        registry.register(colliding)

    assert tuple(registry.registered) == ("feishu",)


def test_active_dependency_filter_does_not_call_spec_for_irrelevant_rows():
    class ExplosiveConnector:
        def spec(self):
            raise AssertionError("irrelevant connector spec executed")

    validate_active_connector_dependencies(
        [
            ConnectionRecord(
                connection_id="inactive",
                tenant_id="tenant-a",
                connector_key="feishu",
                display_name="inactive",
                status="disabled",
                data_mode="direct",
                public_config={"secret": "must-not-be-read"},
                config_version=1,
            )
        ],
        [ExplosiveConnector()],
        allowlist="feishu",
    )


def test_active_dependency_spec_failure_is_sanitized():
    marker = "credential=dependency-secret"

    class ExplosiveConnector:
        def spec(self):
            raise RuntimeError(marker)

    with pytest.raises(ConnectorDiscoveryError) as raised:
        validate_active_connector_dependencies(
            [
                ConnectionRecord(
                    connection_id="active",
                    tenant_id="tenant-a",
                    connector_key="feishu",
                    display_name="active",
                    status="active",
                    data_mode="direct",
                    public_config={},
                    config_version=1,
                )
            ],
            [ExplosiveConnector()],
            allowlist="feishu",
        )

    assert marker not in str(raised.value)


@pytest.mark.parametrize(
    "tool",
    [
        SimpleNamespace(tool_key="messages.list", mcp_name="feishu_list_messages"),
        ToolSpec(**{**TOOL.__dict__, "tool_key": 7}),
        ToolSpec(**{**TOOL.__dict__, "tool_key": "messages.\nlist"}),
        ToolSpec(**{**TOOL.__dict__, "operation_kind": "admin"}),
        ToolSpec(**{**TOOL.__dict__, "default_timeout_ms": 0}),
        ToolSpec(**{**TOOL.__dict__, "input_schema": []}),
    ],
)
def test_manifest_rejects_malformed_nested_tools_at_tool_boundary(tool):
    spec = ConnectorSpec(
        connector_key="feishu",
        version="1.0.0",
        tools=(tool,),
    )

    with pytest.raises(ValueError, match="tool"):
        validate_connector_manifest(spec)


def test_manifest_rejects_noncanonical_version_whitespace():
    spec = ConnectorSpec(connector_key="feishu", version=" 1.0.0 ", tools=())

    with pytest.raises(ValueError, match="version"):
        validate_connector_manifest(spec)


def test_registry_rejects_connector_key_colliding_with_its_own_tool_namespace():
    connector = FakeConnector(
        "feishu",
        tools=(
            ToolSpec(
                **{
                    **TOOL.__dict__,
                    "tool_key": "feishu",
                    "mcp_name": "feishu_messages",
                }
            ),
        ),
    )
    registry = ConnectorRegistry()

    with pytest.raises(ValueError, match="cross-namespace"):
        registry.register(connector)

    assert tuple(registry.registered) == ()


def test_tolerant_discovery_retains_safe_failure_metadata(monkeypatch):
    marker = "credential=optional-secret"
    configure(
        monkeypatch,
        "feishu,lark",
        [
            FakeEntryPoint("feishu", error=RuntimeError(marker)),
            FakeEntryPoint("lark"),
        ],
    )

    result = discover_connector_packages()

    assert [item.spec.connector_key for item in result.connectors] == ["lark"]
    assert result.failures == (
        ConnectorDiscoveryFailure(connector_key="feishu", reason="load"),
    )
    assert marker not in repr(result)


def test_registration_is_idempotent_for_equivalent_validated_packages():
    registry = ConnectorRegistry()
    first = FakeConnector()
    replacement = FakeConnector()
    first_item = ValidatedConnector(first, first.spec())
    replacement_item = ValidatedConnector(replacement, replacement.spec())

    register_discovered_connectors(
        registry, ConnectorDiscoveryResult((first_item,), ())
    )
    register_discovered_connectors(
        registry, ConnectorDiscoveryResult((replacement_item,), ())
    )

    assert registry.get("feishu") is replacement
    assert tuple(registry.registered) == ("feishu",)


def test_startup_registers_valid_packages_into_gateway_and_sync_registry(monkeypatch):
    from app import main

    connector = FakeConnector()
    result = ConnectorDiscoveryResult(
        (ValidatedConnector(connector, connector.spec()),), ()
    )
    registry = ConnectorRegistry([FakeConnector("wecom", tools=())])
    monkeypatch.setattr(main, "connector_registry", registry)
    monkeypatch.setattr(main.connection_sync_orchestrator, "_registry", registry)
    monkeypatch.setattr(main, "discover_connector_packages", lambda: result)
    monkeypatch.setattr(main, "list_active_connector_dependencies", lambda: ())
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(connector_allowlist="feishu"),
    )

    main.configure_trusted_connectors()

    assert registry.get("feishu") is connector
    assert main.connection_sync_orchestrator._registry is registry


def test_default_gateway_and_sync_orchestrator_share_the_connector_registry():
    from app import main

    assert main.connector_registry is main.mcp_gateway._runtime._registry
    assert main.connector_registry is main.connection_sync_orchestrator._registry


def test_repeated_startup_configuration_is_idempotent(monkeypatch):
    from app import main

    registry = ConnectorRegistry()
    calls = 0

    def discover():
        nonlocal calls
        calls += 1
        connector = FakeConnector()
        return ConnectorDiscoveryResult(
            (ValidatedConnector(connector, connector.spec()),), ()
        )

    monkeypatch.setattr(main, "discover_connector_packages", discover)
    monkeypatch.setattr(main, "list_active_connector_dependencies", lambda: ())
    monkeypatch.setattr(main, "connector_registry", registry)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(connector_allowlist="feishu"),
    )

    main.configure_trusted_connectors()
    main.configure_trusted_connectors()

    assert calls == 2
    assert tuple(registry.registered) == ("feishu",)


def test_next_startup_removes_stale_discovered_connector_but_keeps_builtin(
    monkeypatch,
):
    from app import main

    registry = ConnectorRegistry([FakeConnector("wecom", tools=())])
    connector = FakeConnector()
    results = iter(
        (
            ConnectorDiscoveryResult(
                (ValidatedConnector(connector, connector.spec()),), ()
            ),
            ConnectorDiscoveryResult(
                (), (ConnectorDiscoveryFailure("feishu", "load"),)
            ),
        )
    )
    monkeypatch.setattr(main, "discover_connector_packages", lambda: next(results))
    monkeypatch.setattr(main, "list_active_connector_dependencies", lambda: ())
    monkeypatch.setattr(main, "connector_registry", registry)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(connector_allowlist="feishu"),
    )

    main.configure_trusted_connectors()
    main.configure_trusted_connectors()

    assert tuple(registry.registered) == ("wecom",)


def test_startup_tolerates_optional_broken_package_without_active_dependency(
    monkeypatch,
):
    from app import main

    result = ConnectorDiscoveryResult(
        (), (ConnectorDiscoveryFailure("feishu", "load"),)
    )
    monkeypatch.setattr(main, "discover_connector_packages", lambda: result)
    monkeypatch.setattr(main, "list_active_connector_dependencies", lambda: ())
    monkeypatch.setattr(main, "connector_registry", ConnectorRegistry())
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(connector_allowlist="feishu"),
    )

    main.configure_trusted_connectors()


def test_startup_active_broken_package_fails_without_secret_text(monkeypatch):
    from app import main

    result = ConnectorDiscoveryResult(
        (), (ConnectorDiscoveryFailure("feishu", "load"),)
    )
    monkeypatch.setattr(main, "discover_connector_packages", lambda: result)
    monkeypatch.setattr(
        main,
        "list_active_connector_dependencies",
        lambda: (SimpleNamespace(connector_key="feishu", status="active"),),
    )
    monkeypatch.setattr(main, "connector_registry", ConnectorRegistry())
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(connector_allowlist="feishu"),
    )

    with pytest.raises(ConnectorDiscoveryError) as raised:
        main.configure_trusted_connectors()

    assert "credential" not in str(raised.value)


def test_lifespan_configures_connectors_after_migrations_before_gateway(monkeypatch):
    from app import main

    events = []
    registry = ConnectorRegistry()

    class Gateway:
        _runtime = SimpleNamespace(_registry=registry)

        @contextlib.asynccontextmanager
        async def run(self):
            events.append("gateway")
            yield

    class Scheduler:
        def __init__(self, **kwargs):
            self.running = False

        def add_job(self, *args, **kwargs):
            pass

        def start(self):
            self.running = True

        def shutdown(self, *, wait):
            self.running = False

    def create_task(coroutine):
        coroutine.close()
        return object()

    monkeypatch.setattr(
        main.db, "run_startup_migrations", lambda: events.append("migrations")
    )
    monkeypatch.setattr(
        main,
        "configure_trusted_connectors",
        lambda *, registry: events.append(("discovery", registry)),
    )
    monkeypatch.setattr(main, "acquire_audit_writer", lambda: None)
    monkeypatch.setattr(main, "release_audit_writer", lambda timeout: True)
    monkeypatch.setattr(main, "AsyncIOScheduler", Scheduler)
    monkeypatch.setattr(main.asyncio, "create_task", create_task)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            sync_interval_report_min=15,
            sync_interval_approval_min=30,
        ),
    )
    app = SimpleNamespace(state=SimpleNamespace(mcp_gateway=Gateway()))

    async def exercise():
        async with main.lifespan(app):
            events.append("running")

    asyncio.run(exercise())

    assert events[:3] == ["migrations", ("discovery", registry), "gateway"]


def test_discovered_batch_collision_is_atomic_against_builtins_and_packages():
    builtin = FakeConnector("wecom", tools=())
    first = FakeConnector(
        "feishu",
        tools=(ToolSpec(**{**TOOL.__dict__, "mcp_name": "lark"}),),
    )
    second = FakeConnector("lark", tools=())
    registry = ConnectorRegistry([builtin])
    result = ConnectorDiscoveryResult(
        (
            ValidatedConnector(first, first.spec()),
            ValidatedConnector(second, second.spec()),
        ),
        (),
    )

    with pytest.raises(ValueError, match="collision"):
        register_discovered_connectors(registry, result)

    assert tuple(registry.registered) == ("wecom",)


def test_optional_startup_batch_collision_is_tolerated_without_partial_state(
    monkeypatch,
):
    from app import main

    first = FakeConnector(
        "feishu",
        tools=(ToolSpec(**{**TOOL.__dict__, "tool_key": "lark"}),),
    )
    second = FakeConnector("lark", tools=())
    result = ConnectorDiscoveryResult(
        (
            ValidatedConnector(first, first.spec()),
            ValidatedConnector(second, second.spec()),
        ),
        (),
    )
    registry = ConnectorRegistry([FakeConnector("wecom", tools=())])
    monkeypatch.setattr(main, "discover_connector_packages", lambda: result)
    monkeypatch.setattr(main, "list_active_connector_dependencies", lambda: ())
    monkeypatch.setattr(main, "connector_registry", registry)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(connector_allowlist="feishu,lark"),
    )

    main.configure_trusted_connectors()

    assert tuple(registry.registered) == ("wecom", "feishu")


def test_optional_collision_does_not_fail_unrelated_active_package(monkeypatch):
    from app import main

    active_connector = FakeConnector(
        "feishu",
        tools=(ToolSpec(**{**TOOL.__dict__, "mcp_name": "lark"}),),
    )
    optional_collision = FakeConnector("lark", tools=())
    result = ConnectorDiscoveryResult(
        (
            ValidatedConnector(active_connector, active_connector.spec()),
            ValidatedConnector(optional_collision, optional_collision.spec()),
        ),
        (),
    )
    registry = ConnectorRegistry([FakeConnector("wecom", tools=())])
    monkeypatch.setattr(main, "discover_connector_packages", lambda: result)
    monkeypatch.setattr(
        main,
        "list_active_connector_dependencies",
        lambda: (SimpleNamespace(connector_key="feishu", status="active"),),
    )
    monkeypatch.setattr(main, "connector_registry", registry)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(connector_allowlist="feishu,lark"),
    )

    main.configure_trusted_connectors()

    assert registry.get("feishu") is active_connector
    assert "lark" not in registry.registered


def test_active_package_wins_identity_collision_with_earlier_optional_package(
    monkeypatch,
):
    from app import main

    optional_connector = FakeConnector(
        "feishu",
        tools=(ToolSpec(**{**TOOL.__dict__, "mcp_name": "lark"}),),
    )
    active_connector = FakeConnector("lark", tools=())
    result = ConnectorDiscoveryResult(
        (
            ValidatedConnector(optional_connector, optional_connector.spec()),
            ValidatedConnector(active_connector, active_connector.spec()),
        ),
        (),
    )
    registry = ConnectorRegistry([FakeConnector("wecom", tools=())])
    monkeypatch.setattr(main, "discover_connector_packages", lambda: result)
    monkeypatch.setattr(
        main,
        "list_active_connector_dependencies",
        lambda: (SimpleNamespace(connector_key="lark", status="active"),),
    )
    monkeypatch.setattr(main, "connector_registry", registry)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(connector_allowlist="feishu,lark"),
    )

    main.configure_trusted_connectors()

    assert registry.get("lark") is active_connector
    assert "feishu" not in registry.registered


def test_registry_validated_spec_is_deeply_detached_and_immutable():
    nested_input = {
        "type": "object",
        "properties": {"payload": {"type": "array", "items": ["original"]}},
    }
    config_schema = {"properties": {"region": {"enum": ["cn"]}}}
    tool = ToolSpec(**{**TOOL.__dict__, "input_schema": nested_input})
    connector = FakeConnector(tools=(tool,))
    object.__setattr__(connector._spec, "config_schema", config_schema)
    registry = ConnectorRegistry([connector])

    nested_input["properties"]["payload"]["items"].append("mutated")
    config_schema["properties"]["region"]["enum"].append("secret")
    snapshot = registry.validated_spec("feishu")

    assert snapshot.tools[0].input_schema["properties"]["payload"]["items"] == (
        "original",
    )
    assert snapshot.config_schema["properties"]["region"]["enum"] == ("cn",)
    with pytest.raises(TypeError):
        snapshot.tools[0].input_schema["new"] = "value"


def test_manifest_rejects_unsafe_description_and_cyclic_or_unbounded_schema():
    cyclic = {"type": "object"}
    cyclic["self"] = cyclic
    unsafe_description = ToolSpec(
        **{**TOOL.__dict__, "description": "secret\nforged-log"}
    )
    cyclic_schema = ToolSpec(**{**TOOL.__dict__, "input_schema": cyclic})
    deeply_nested = value = {}
    for _ in range(40):
        child = {}
        value["child"] = child
        value = child

    with pytest.raises(ValueError, match="description"):
        validate_connector_manifest(
            ConnectorSpec("feishu", (unsafe_description,), version="1")
        )
    with pytest.raises(ValueError, match="schema"):
        validate_connector_manifest(
            ConnectorSpec("feishu", (cyclic_schema,), version="1")
        )
    with pytest.raises(ValueError, match="schema"):
        validate_connector_manifest(
            ConnectorSpec(
                "feishu",
                (ToolSpec(**{**TOOL.__dict__, "input_schema": deeply_nested}),),
                version="1",
            )
        )


def test_manifest_rejects_unicode_control_abuse_in_metadata_and_schema():
    bidi_description = ToolSpec(**{**TOOL.__dict__, "description": "safe\u202eforged"})
    control_schema = ToolSpec(
        **{**TOOL.__dict__, "input_schema": {"title": "safe\u0085forged"}}
    )

    with pytest.raises(ValueError, match="description"):
        validate_connector_manifest(
            ConnectorSpec("feishu", (bidi_description,), version="1")
        )
    with pytest.raises(ValueError, match="schema"):
        validate_connector_manifest(
            ConnectorSpec("feishu", (control_schema,), version="1")
        )


def test_manifest_rejects_extremely_large_schema_integer_before_registration():
    oversized_integer = 10**100000
    tool = ToolSpec(**{**TOOL.__dict__, "input_schema": {"maximum": oversized_integer}})
    registry = ConnectorRegistry()

    with pytest.raises(ValueError, match="schema"):
        registry.register(FakeConnector(tools=(tool,)))

    assert tuple(registry.registered) == ()


def test_create_app_custom_gateway_builds_scheduler_from_gateway_registry():
    from app import main

    custom_registry = ConnectorRegistry([FakeConnector("custom", tools=())])
    gateway = SimpleNamespace(
        _runtime=SimpleNamespace(_registry=custom_registry),
        resolver=object(),
    )

    app = main.create_app(gateway=gateway)

    assert app.state.connection_sync_orchestrator._registry is custom_registry
