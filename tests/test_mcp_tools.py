import json
import inspect

import pytest

from app import mcp_audit, mcp_server
from app.auth import TenantCtx, _ctx


EXPECTED_TOOL_SIGNATURES = {
    "wecom_list_reports": "(starttime: 'int', endtime: 'int', limit: 'int' = 100) -> 'str'",
    "wecom_get_report": "(journaluuid: 'str') -> 'str'",
    "wecom_list_approvals": "(starttime: 'int', endtime: 'int', limit: 'int' = 100) -> 'str'",
    "wecom_get_approval_detail": "(sp_no: 'str') -> 'str'",
    "wecom_list_checkins": "(starttime: 'int', endtime: 'int', limit: 'int' = 100) -> 'str'",
    "wecom_list_smart_table_records": "(docid: 'str', sheet_id: 'str', limit: 'int' = 1000) -> 'str'",
}


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


def test_registered_tool_names_and_signatures_remain_compatible():
    assert set(mcp_server.list_tool_names()) == set(EXPECTED_TOOL_SIGNATURES)
    assert {
        name: str(inspect.signature(getattr(mcp_server, name)))
        for name in EXPECTED_TOOL_SIGNATURES
    } == EXPECTED_TOOL_SIGNATURES


def test_real_data_tool_docstrings_describe_stored_and_direct_modes():
    for name in (
        "wecom_list_reports",
        "wecom_get_report",
        "wecom_list_approvals",
        "wecom_get_approval_detail",
        "wecom_list_checkins",
    ):
        doc = getattr(mcp_server, name).__doc__ or ""
        assert "stored" in doc, name
        assert "direct" in doc, name


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
    events = []
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: True)
    monkeypatch.setattr(mcp_server, "write_event", events.append)
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
    assert len(events) == 1
    assert events[0].category == "tool"
    assert events[0].event_name == tool_name
    assert events[0].tenant_id == "tenant-a"


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


def test_direct_accessor_failure_redacts_secrets_and_does_not_fall_back(
    monkeypatch, use_tenant_ctx, caplog
):
    use_tenant_ctx("direct")
    sensitive_error = (
        "secret=corp-secret access_token=token-value "
        "mysql+pymysql://root:db-password@db/gateway"
    )
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: False)
    monkeypatch.setattr(
        mcp_server.data_access,
        "list_reports",
        lambda *values: (_ for _ in ()).throw(RuntimeError(sensitive_error)),
    )
    monkeypatch.setattr(
        mcp_server.data_access.db,
        "query_reports_by_window",
        lambda *values: (_ for _ in ()).throw(
            AssertionError("direct failure must not read cached rows")
        ),
    )
    monkeypatch.setattr(mcp_server, "_audit", lambda *values: None)

    with caplog.at_level("WARNING", logger="app.mcp_server"):
        response = mcp_server.wecom_list_reports(1, 2, 10)

    assert json.loads(response) == {
        "tenant": "tenant-a",
        "source": "wecom",
        "errcode": 502,
        "errmsg": "数据访问失败",
    }
    assert "MCP data access failed tool=wecom_list_reports: RuntimeError" in caplog.text
    for secret in ("corp-secret", "token-value", "db-password"):
        assert secret not in response
        assert secret not in caplog.text


def test_public_wecom_failure_returns_controlled_error_and_audits_error(
    monkeypatch, use_tenant_ctx, caplog
):
    use_tenant_ctx("direct")
    sensitive = (
        "secret=corp-secret access_token=token-value "
        "mysql+pymysql://root:db-password@db/gateway [40014]"
    )
    audits = []
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: False)
    monkeypatch.setattr(
        mcp_server.data_access,
        "sync_reports_window",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(sensitive)),
    )
    monkeypatch.setattr(mcp_server, "_audit", lambda *values: audits.append(values))

    with caplog.at_level("WARNING", logger="app.mcp_server"):
        response = mcp_server.wecom_list_reports(1, 2, 10)

    assert json.loads(response) == {
        "tenant": "tenant-a",
        "source": "wecom",
        "errcode": 40014,
        "errmsg": "企微汇报请求失败",
    }
    assert len(audits) == 1
    assert audits[0][3] == "error"
    assert "PublicDataAccessError" in caplog.text
    for secret in ("corp-secret", "token-value", "db-password"):
        assert secret not in response
        assert secret not in caplog.text


def test_missing_checkin_identity_returns_actionable_public_error(
    monkeypatch, use_tenant_ctx, caplog
):
    use_tenant_ctx("direct")
    audits = []
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: False)
    monkeypatch.setattr(mcp_server, "_audit", lambda *values: audits.append(values))

    with caplog.at_level("WARNING", logger="app.mcp_server"):
        result = json.loads(mcp_server.wecom_list_checkins(1, 2, 10))

    assert result == {
        "tenant": "tenant-a",
        "source": "wecom",
        "errcode": 400,
        "errmsg": "直连打卡需要配置通讯录 Secret 或手工 userid",
    }
    assert len(audits) == 1
    assert audits[0][3] == "error"
    assert "PublicDataAccessError" in caplog.text


@pytest.mark.parametrize(
    ("accessor_result", "expected_source", "expected_errcode"),
    [
        (
            {"tenant": "tenant-a", "source": "db", "count": 0, "records": []},
            "db",
            None,
        ),
        (
            {"source": "db", "errcode": 404, "errmsg": "汇报单号不存在"},
            "db",
            404,
        ),
    ],
)
def test_stored_success_and_not_found_keep_database_source(
    monkeypatch,
    use_tenant_ctx,
    accessor_result,
    expected_source,
    expected_errcode,
):
    use_tenant_ctx("stored")
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: False)
    monkeypatch.setattr(
        mcp_server.data_access, "get_report", lambda *values: accessor_result
    )
    monkeypatch.setattr(mcp_server, "_audit", lambda *values: None)

    result = json.loads(mcp_server.wecom_get_report("r1"))

    assert result["source"] == expected_source
    assert result.get("errcode") == expected_errcode


def test_stored_accessor_exception_keeps_safe_database_source(
    monkeypatch, use_tenant_ctx
):
    use_tenant_ctx("stored")
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: False)
    monkeypatch.setattr(
        mcp_server.data_access,
        "get_report",
        lambda *values: (_ for _ in ()).throw(RuntimeError("secret=db-secret")),
    )
    monkeypatch.setattr(mcp_server, "_audit", lambda *values: None)

    response = mcp_server.wecom_get_report("r1")

    assert json.loads(response) == {
        "tenant": "tenant-a",
        "source": "db",
        "errcode": 502,
        "errmsg": "数据访问失败",
    }
    assert "db-secret" not in response


@pytest.mark.parametrize(
    ("accessor_result", "expected_status"),
    [
        (
            {
                "tenant": "tenant-a",
                "source": "db",
                "count": 0,
                "records": [],
                "partial_count": 0,
            },
            "ok",
        ),
        (
            {
                "tenant": "tenant-a",
                "source": "db",
                "count": 1,
                "records": [{"_partial": True}],
                "partial_count": 1,
            },
            "partial",
        ),
        (
            {"source": "db", "errcode": 404, "errmsg": "not found"},
            "error",
        ),
    ],
)
def test_real_result_selects_expected_audit_status(
    monkeypatch, use_tenant_ctx, accessor_result, expected_status
):
    use_tenant_ctx("stored")
    audits = []
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: False)
    monkeypatch.setattr(
        mcp_server.data_access, "list_reports", lambda *values: accessor_result
    )
    monkeypatch.setattr(mcp_server, "write_event", audits.append)

    mcp_server.wecom_list_reports(1, 2, 10)

    assert len(audits) == 1
    assert audits[0].category == "tool"
    assert audits[0].event_name == "wecom_list_reports"
    assert audits[0].result_status == expected_status


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
    writer = mcp_audit.AuditEventWriter(
        insert=lambda event: (_ for _ in ()).throw(
            RuntimeError("secret=database unavailable")
        ),
    )
    monkeypatch.setattr(mcp_audit, "_audit_writer", writer)

    try:
        with caplog.at_level("WARNING", logger="app.mcp_audit"):
            result = json.loads(mcp_server.wecom_list_reports(1, 2, 10))
            assert writer.flush(1) is True
    finally:
        writer.shutdown(1)

    assert result == expected
    assert "RuntimeError" in caplog.text
    assert "database unavailable" not in caplog.text


@pytest.mark.parametrize(
    "failure_point",
    ["current_ctx", "current_request_metadata", "McpLogEvent", "write_event"],
)
def test_tool_audit_instrumentation_failure_never_changes_mock_result(
    monkeypatch, use_tenant_ctx, caplog, failure_point
):
    use_tenant_ctx("stored")
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: True)

    def fail(*args, **kwargs):
        raise RuntimeError("secret=audit-instrumentation")

    monkeypatch.setattr(mcp_server, failure_point, fail)

    with caplog.at_level("WARNING", logger="app.mcp_server"):
        result = json.loads(mcp_server.wecom_list_reports(1, 2, 10))

    assert result["source"] == "mock"
    assert "RuntimeError" in caplog.text
    assert "audit-instrumentation" not in caplog.text


def test_mock_tool_audit_redacts_sensitive_target(monkeypatch, use_tenant_ctx):
    use_tenant_ctx("stored")
    events = []
    monkeypatch.setattr(mcp_server, "_use_mock", lambda: True)
    monkeypatch.setattr(mcp_server, "write_event", events.append)

    mcp_server.wecom_get_report("secret=corp-secret")

    assert len(events) == 1
    assert "corp-secret" not in repr(events[0])
