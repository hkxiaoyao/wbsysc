import json

import pytest

from app import mcp_server
from app.auth import TenantCtx, _ctx


def tenant_ctx(mode="direct"):
    return TenantCtx(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="secret",
        schema_name="wbd_123",
        contact_secret="",
        checkin_userids=[],
        enabled_modules={"report", "approval", "checkin"},
        data_mode=mode,
    )


@pytest.fixture
def use_tenant_ctx():
    tokens = []

    def activate(mode="direct"):
        token = _ctx.set(tenant_ctx(mode))
        tokens.append(token)
        return _ctx.get()

    yield activate

    while tokens:
        _ctx.reset(tokens.pop())


@pytest.mark.parametrize(
    ("tool_name", "accessor_name", "args"),
    [
        ("wecom_list_reports", "list_reports", (1, 2, 10)),
        ("wecom_get_report", "get_report", ("r1",)),
        ("wecom_list_approvals", "list_approvals", (1, 2, 10)),
        ("wecom_get_approval_detail", "get_approval", ("sp1",)),
        ("wecom_list_checkins", "list_checkins", (1, 2, 10)),
    ],
)
def test_real_tools_delegate_once_to_data_access(
    monkeypatch, use_tenant_ctx, tool_name, accessor_name, args
):
    context = use_tenant_ctx()
    calls = []

    def accessor(*values):
        calls.append(values)
        return {
            "tenant": values[0].tenant_id,
            "source": "wecom",
            "count": 0,
            "records": [],
            "partial_count": 0,
        }

    monkeypatch.setattr(mcp_server, "_use_mock", lambda: False)
    monkeypatch.setattr(mcp_server.data_access, accessor_name, accessor)
    monkeypatch.setattr(mcp_server, "_audit", lambda *values: None)

    result = json.loads(getattr(mcp_server, tool_name)(*args))

    assert result["source"] == "wecom"
    assert calls == [(context, *args)]


@pytest.mark.parametrize(
    ("tool_name", "accessor_name", "args"),
    [
        ("wecom_list_reports", "list_reports", (1, 2, 10)),
        ("wecom_get_report", "get_report", ("r1",)),
        ("wecom_list_approvals", "list_approvals", (1, 2, 10)),
        ("wecom_get_approval_detail", "get_approval", ("sp1",)),
        ("wecom_list_checkins", "list_checkins", (1, 2, 10)),
    ],
)
def test_mock_tools_bypass_data_access(
    monkeypatch, use_tenant_ctx, tool_name, accessor_name, args
):
    use_tenant_ctx("stored")
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: True)
    monkeypatch.setattr(
        mcp_server.data_access,
        accessor_name,
        lambda *values: (_ for _ in ()).throw(
            AssertionError("mock tools must not call data access")
        ),
    )

    result = getattr(mcp_server, tool_name)(*args)

    assert isinstance(result, str)
    assert isinstance(json.loads(result), dict)


@pytest.mark.parametrize(
    ("tool_name", "query_name"),
    [
        ("wecom_list_reports", "query_reports_by_window"),
        ("wecom_list_approvals", "query_approvals_by_window"),
        ("wecom_list_checkins", "query_checkins_by_window"),
    ],
)
@pytest.mark.parametrize(
    ("requested", "bounded"), [(-1, 1), (0, 100), (101, 100)]
)
def test_list_limits_remain_bounded_through_data_access(
    monkeypatch, use_tenant_ctx, tool_name, query_name, requested, bounded
):
    use_tenant_ctx("stored")
    captured = []

    def query(schema, start, end, limit):
        captured.append(limit)
        return []

    monkeypatch.setattr(mcp_server, "_use_mock", lambda: False)
    monkeypatch.setattr(mcp_server.data_access.db, query_name, query)
    monkeypatch.setattr(mcp_server, "_audit", lambda *values: None)

    getattr(mcp_server, tool_name)(1, 2, requested)

    assert captured == [bounded]


def test_direct_accessor_failure_does_not_fall_back_to_database(
    monkeypatch, use_tenant_ctx
):
    use_tenant_ctx("direct")
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: False)
    monkeypatch.setattr(
        mcp_server.data_access,
        "list_reports",
        lambda *values: (_ for _ in ()).throw(RuntimeError("wecom unavailable")),
    )
    monkeypatch.setattr(
        mcp_server.data_access.db,
        "query_reports_by_window",
        lambda *values: (_ for _ in ()).throw(
            AssertionError("direct failure must not read cached rows")
        ),
    )
    monkeypatch.setattr(mcp_server, "_audit", lambda *values: None)

    result = json.loads(mcp_server.wecom_list_reports(1, 2, 10))

    assert result == {
        "tenant": "tenant-a",
        "source": "wecom",
        "errcode": 502,
        "errmsg": "wecom unavailable",
    }


def test_audit_failure_logs_warning_without_changing_tool_result(
    monkeypatch, use_tenant_ctx, caplog
):
    use_tenant_ctx("direct")
    expected = {
        "tenant": "tenant-a",
        "source": "wecom",
        "count": 0,
        "records": [],
        "partial_count": 0,
    }
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: False)
    monkeypatch.setattr(
        mcp_server.data_access, "list_reports", lambda *values: expected
    )
    monkeypatch.setattr(
        mcp_server.data_access.db,
        "log_audit",
        lambda *values: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with caplog.at_level("WARNING", logger="app.mcp_server"):
        result = json.loads(mcp_server.wecom_list_reports(1, 2, 10))

    assert result == expected
    assert "MCP audit write failed tool=wecom_list_reports: RuntimeError" in caplog.text
