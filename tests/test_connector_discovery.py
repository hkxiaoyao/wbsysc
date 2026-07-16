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
    ConnectorDiscoveryError,
    discover_trusted_connectors,
    validate_active_connector_dependencies,
)


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
