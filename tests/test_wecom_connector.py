import pytest

from app.connections.models import ConnectionRecord
from app.connectors.contracts import ConnectionContext
from app.connectors.wecom import WeComConnector


def connection_context(connection_id="conn-a", data_mode="direct"):
    return ConnectionContext(
        connection=ConnectionRecord(
            connection_id=connection_id,
            tenant_id="tenant-a",
            connector_key="wecom",
            display_name="WeCom",
            status="active",
            data_mode=data_mode,
            public_config={
                "corpid": "ww123",
                "schema_name": "wbd_123",
                "checkin_userids": ["user-a"],
            },
            config_version=1,
        ),
        credentials={
            "wecom_app_secret": "app-secret",
            "wecom_contact_secret": "contact-secret",
        },
        request_metadata={"request_id": "request-a"},
    )


def test_wecom_connector_manifest_preserves_public_tool_names():
    spec = WeComConnector().spec()

    assert spec.connector_key == "wecom"
    assert spec.supports_sync is True
    assert {
        tool.mcp_name for tool in spec.tools
    } == {
        "wecom_list_reports",
        "wecom_get_report",
        "wecom_list_approvals",
        "wecom_get_approval_detail",
        "wecom_list_checkins",
        "wecom_list_smart_table_records",
    }
    assert spec.tool("wecom_list_reports").tool_key == "reports.list"


def test_wecom_connector_manifest_owns_wecom_configuration_and_credentials():
    spec = WeComConnector().spec()

    assert spec.config_schema["required"] == ["corpid", "schema_name"]
    assert set(spec.config_schema["properties"]) >= {
        "corpid",
        "schema_name",
        "enabled_modules",
        "sync_interval_min",
        "checkin_userids",
        "trusted_domain",
    }
    assert spec.config_schema["properties"]["enabled_modules"]["type"] == "array"
    assert spec.config_schema["properties"]["sync_interval_min"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 1440,
    }
    assert spec.credential_schema["required"] == ["wecom_app_secret"]
    assert set(spec.credential_schema["properties"]) == {
        "wecom_app_secret",
        "wecom_contact_secret",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_key", "args", "accessor_name", "expected"),
    [
        (
            "reports.list",
            {"starttime": 1, "endtime": 2, "limit": 10},
            "list_reports",
            {"tenant": "tenant-a", "source": "wecom", "records": []},
        ),
        (
            "reports.get",
            {"journaluuid": "r-1"},
            "get_report",
            {"tenant": "tenant-a", "source": "db", "detail": {}},
        ),
    ],
)
async def test_wecom_connector_preserves_existing_result_envelope(
    monkeypatch, tool_key, args, accessor_name, expected
):
    from app.connectors import wecom

    context = connection_context()
    calls = []

    def accessor(*values):
        calls.append(values)
        return expected

    monkeypatch.setattr(wecom, "_use_mock", lambda: False)
    monkeypatch.setattr(wecom.data_access, accessor_name, accessor)

    result = await WeComConnector().execute(context, tool_key, args)

    assert result.data == expected
    assert result.data["tenant"] == "tenant-a"
    assert result.data["source"] in {"wecom", "db", "mock"}
    assert calls == [
        (
            context,
            *(
                (args["starttime"], args["endtime"], args["limit"])
                if tool_key == "reports.list"
                else (args["journaluuid"],)
            ),
        )
    ]


@pytest.mark.asyncio
async def test_wecom_connector_uses_connection_context_credentials(monkeypatch):
    from app.connectors import wecom

    context = connection_context()
    observed = []
    monkeypatch.setattr(wecom, "_use_mock", lambda: False)

    def accessor(access_context, starttime, endtime, limit):
        observed.append(access_context)
        return {
            "tenant": access_context.tenant_id,
            "source": "wecom",
            "count": 0,
            "records": [],
            "partial_count": 0,
        }

    monkeypatch.setattr(wecom.data_access, "list_reports", accessor)

    await WeComConnector().execute(
        context,
        "reports.list",
        {"starttime": 1, "endtime": 2, "limit": 10},
    )

    assert observed == [context]
    assert observed[0].credentials["wecom_app_secret"] == "app-secret"


def test_stored_data_access_uses_the_connection_public_schema(monkeypatch):
    from app import data_access

    captured = []

    def query(schema_name, starttime, endtime, limit):
        captured.append((schema_name, starttime, endtime, limit))
        return []

    monkeypatch.setattr(data_access.db, "query_reports_by_window", query)

    result = data_access.list_reports(connection_context(data_mode="stored"), 1, 2, 10)

    assert result == {
        "tenant": "tenant-a",
        "source": "db",
        "count": 0,
        "records": [],
        "partial_count": 0,
    }
    assert captured == [("wbd_123", 1, 2, 10)]


def test_hybrid_data_access_prefers_existing_stored_records(monkeypatch):
    from app import data_access

    monkeypatch.setattr(
        data_access.db,
        "query_reports_by_window",
        lambda *args: [
            {
                "journaluuid": "r-1",
                "template_id": "template-a",
                "template_name": "日报",
                "report_time": 2,
                "submitter_userid": "user-a",
                "is_partial": 0,
            }
        ],
    )
    monkeypatch.setattr(
        data_access,
        "sync_reports_window",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("hybrid must not fetch when stored records exist")
        ),
    )

    result = data_access.list_reports(connection_context(data_mode="hybrid"), 1, 2, 10)

    assert result["source"] == "db"
    assert result["records"][0]["journaluuid"] == "r-1"


def test_sync_window_reads_the_connection_scoped_cursor(monkeypatch):
    from app.wecom import dispatch

    calls = []
    monkeypatch.setattr(dispatch.time, "time", lambda: 1_000_000)
    monkeypatch.setattr(
        dispatch.db,
        "get_cursor",
        lambda schema_name, resource_key, cursor_key: calls.append(
            (schema_name, resource_key, cursor_key)
        )
        or "",
    )

    dispatch._window("wbd_123", "report", 30, cursor_key="conn-a")

    assert calls == [("wbd_123", "report", "conn-a")]


def test_connection_sync_does_not_return_raw_failure_text(monkeypatch):
    from app.wecom import dispatch

    class Lock:
        def __enter__(self):
            return True

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(dispatch.db, "tenant_sync_lock", lambda *args, **kwargs: Lock())
    monkeypatch.setattr(
        dispatch,
        "_sync_one_report",
        lambda *args, **kwargs: {
            "pulled": 1,
            "error": "secret=app-secret raw-response-body",
        },
    )

    result = dispatch.run_sync_connection(
        connection_context("conn-a", "stored"), "reports"
    )

    assert result == {"pulled": 1, "error": "sync_failed"}
    assert "app-secret" not in repr(result)
    assert "raw-response-body" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "resource_key",
        "tool_key",
        "identifier",
        "sync_name",
        "detail_name",
        "upsert_name",
        "stored_detail_name",
        "args",
    ),
    [
        (
            "reports",
            "reports.get",
            "report-1",
            "sync_reports_window",
            "fetch_report_detail",
            "upsert_report",
            "get_report_detail",
            {"journaluuid": "report-1"},
        ),
        (
            "approvals",
            "approvals.get",
            "approval-1",
            "sync_approvals_window",
            "fetch_approval_detail",
            "upsert_approval",
            "get_approval_detail",
            {"sp_no": "approval-1"},
        ),
    ],
)
async def test_connection_sync_never_persists_upstream_exception_text(
    monkeypatch,
    resource_key,
    tool_key,
    identifier,
    sync_name,
    detail_name,
    upsert_name,
    stored_detail_name,
    args,
):
    from app import data_access
    from app.wecom import dispatch

    secret_marker = "secret=connection-sync-marker"
    stored = {}

    class Lock:
        def __enter__(self):
            return True

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(dispatch.db, "tenant_sync_lock", lambda *args, **kwargs: Lock())
    monkeypatch.setattr(dispatch.db, "get_cursor", lambda *args: "")
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: None)
    monkeypatch.setattr(dispatch, sync_name, lambda *args, **kwargs: [identifier])
    monkeypatch.setattr(
        dispatch,
        detail_name,
        lambda *args: (_ for _ in ()).throw(RuntimeError(secret_marker)),
    )

    def upsert(*values, **kwargs):
        stored.update(values[2])

    monkeypatch.setattr(dispatch.db, upsert_name, upsert)
    monkeypatch.setattr(data_access.db, stored_detail_name, lambda *args: dict(stored))

    sync_result = dispatch.run_sync_connection(
        connection_context("conn-a", "stored"), resource_key
    )
    result = await WeComConnector(mock_enabled=lambda: False).execute(
        connection_context("conn-a", "stored"), tool_key, args
    )

    assert stored["_partial"] is True
    assert "_partial_error" not in stored
    assert secret_marker not in repr(stored)
    assert secret_marker not in repr(sync_result)
    assert result.data["detail"]["_partial"] is True
    assert secret_marker not in repr(result.data)


def test_connection_checkin_user_cache_is_partitioned_by_connection_id(monkeypatch):
    from dataclasses import replace

    from app.wecom import dispatch

    class Lock:
        def __enter__(self):
            return True

        def __exit__(self, *args):
            return False

    class FetchResult:
        attempted = 0
        failed = 0
        records = []
        errors = []

    fetches = []
    monkeypatch.setattr(dispatch, "_userid_cache", {})
    monkeypatch.setattr(dispatch.db, "tenant_sync_lock", lambda *args, **kwargs: Lock())
    monkeypatch.setattr(dispatch.db, "get_cursor", lambda *args: "")
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: None)
    monkeypatch.setattr(dispatch.db, "upsert_checkin", lambda *args: None)
    monkeypatch.setattr(
        dispatch,
        "fetch_all_userids",
        lambda corpid, secret: fetches.append(secret) or [secret],
    )
    monkeypatch.setattr(
        dispatch,
        "fetch_checkin_records_with_stats",
        lambda *args: FetchResult(),
    )
    context_a = replace(
        connection_context("conn-a", "stored"),
        credentials={"wecom_app_secret": "app-a", "wecom_contact_secret": "contact-a"},
    )
    context_b = replace(
        connection_context("conn-b", "stored"),
        credentials={"wecom_app_secret": "app-b", "wecom_contact_secret": "contact-b"},
    )

    dispatch.run_sync_connection(context_a, "checkins")
    dispatch.run_sync_connection(context_b, "checkins")

    assert fetches == ["contact-a", "contact-b"]


def test_connection_sync_rejects_a_blank_connection_id_before_using_cursor(monkeypatch):
    from app.wecom import dispatch

    def forbidden(*args, **kwargs):
        raise AssertionError("blank connection IDs must not touch sync state")

    monkeypatch.setattr(dispatch.db, "tenant_sync_lock", forbidden)
    monkeypatch.setattr(dispatch.db, "get_cursor", forbidden)
    monkeypatch.setattr(dispatch.db, "save_cursor", forbidden)
    monkeypatch.setattr(
        dispatch,
        "_sync_one_report",
        forbidden,
    )

    with pytest.raises(ValueError, match="connection_id"):
        dispatch.run_sync_connection(connection_context("", "stored"), "reports")


@pytest.mark.asyncio
async def test_hybrid_fallback_error_uses_wecom_source(monkeypatch):
    from app import data_access

    secret_marker = "secret=hybrid-fallback-marker"
    context = connection_context("conn-a", "hybrid")
    monkeypatch.setattr(data_access.db, "query_reports_by_window", lambda *args: [])
    monkeypatch.setattr(
        data_access,
        "sync_reports_window",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(secret_marker)),
    )

    result = await WeComConnector(mock_enabled=lambda: False).execute(
        context,
        "reports.list",
        {"starttime": 1, "endtime": 2, "limit": 10},
    )

    assert result.data["source"] == "wecom"
    assert secret_marker not in repr(result.data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_key", "args", "storage_name", "direct_name"),
    [
        (
            "reports.list",
            {"starttime": 1, "endtime": 2, "limit": 10},
            "query_reports_by_window",
            "sync_reports_window",
        ),
        (
            "reports.get",
            {"journaluuid": "report-1"},
            "get_report_detail",
            "fetch_report_detail",
        ),
        (
            "approvals.list",
            {"starttime": 1, "endtime": 2, "limit": 10},
            "query_approvals_by_window",
            "sync_approvals_window",
        ),
        (
            "approvals.get",
            {"sp_no": "approval-1"},
            "get_approval_detail",
            "fetch_approval_detail",
        ),
        (
            "checkins.list",
            {"starttime": 1, "endtime": 2, "limit": 10},
            "query_checkins_by_window",
            "fetch_all_userids",
        ),
    ],
)
async def test_hybrid_storage_failure_uses_db_source(
    monkeypatch, tool_key, args, storage_name, direct_name
):
    from app import data_access

    secret_marker = "secret=hybrid-storage-marker"
    direct_calls = []

    def storage_failure(*args, **kwargs):
        raise RuntimeError(secret_marker)

    def record_direct_call(*args, **kwargs):
        direct_calls.append((args, kwargs))
        return []

    monkeypatch.setattr(data_access.db, storage_name, storage_failure)
    monkeypatch.setattr(data_access, direct_name, record_direct_call)

    result = await WeComConnector(mock_enabled=lambda: False).execute(
        connection_context("conn-a", "hybrid"), tool_key, args
    )

    assert result.status == "error"
    assert result.data == {
        "tenant": "tenant-a",
        "source": "db",
        "errcode": 502,
        "errmsg": "数据访问失败",
    }
    assert direct_calls == []
    assert secret_marker not in repr(result.data)


@pytest.mark.asyncio
async def test_wecom_sync_uses_connection_scoped_cursor(monkeypatch):
    from app.connectors import wecom

    calls = []
    sync_store = wecom.ConnectionSyncStore()

    def run_sync(context, resource_key):
        calls.append((context.connection_id, resource_key))
        return {"pulled": 2, "stored": 2, "err": 0}

    monkeypatch.setattr(wecom.dispatch, "run_sync_connection", run_sync)

    result = await WeComConnector(sync_store=sync_store).sync(
        connection_context("conn-a", "stored"), "reports"
    )

    assert result.connection_id == "conn-a"
    assert result.resource_key == "reports"
    assert result.data == {"pulled": 2, "stored": 2, "err": 0}
    assert calls == [("conn-a", "reports")]
    assert sync_store.load("conn-b", "reports") is None
    assert sync_store.load("conn-a", "reports") == result.data
