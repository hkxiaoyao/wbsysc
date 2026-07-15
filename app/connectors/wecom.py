"""The trusted WeCom connector and its temporary legacy MCP bridge.

The connector owns WeCom-specific tool mapping, result envelopes, and sync
state.  The legacy bridge exists only while ``/mcp`` still uses FastMCP; the
connection-aware protocol route is introduced separately by the gateway task.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from app import data_access
from app.auth import TenantCtx
from app.config import get_settings
from app.connections.models import ConnectionRecord
from app.wecom import dispatch
from app.wecom.mock import (
    MOCK_APPROVAL_DETAIL,
    MOCK_APPROVAL_LIST,
    MOCK_REPORT_DETAIL,
    MOCK_REPORT_LIST,
    MOCK_SMARTTABLE_RECORDS,
)

from .contracts import ConnectionContext, ConnectorSpec, ExecutionResult, SyncResult, ToolSpec


logger = logging.getLogger(__name__)


def _read_tool(
    tool_key: str,
    mcp_name: str,
    description: str,
    input_schema: dict[str, Any],
    cache_ttl_seconds: int | None = 60,
) -> ToolSpec:
    return ToolSpec(
        tool_key=tool_key,
        mcp_name=mcp_name,
        description=description,
        input_schema=input_schema,
        output_schema={"type": "object"},
        operation_kind="read",
        default_timeout_ms=30_000,
        cache_ttl_seconds=cache_ttl_seconds,
    )


_WINDOW_SCHEMA = {
    "type": "object",
    "required": ["starttime", "endtime"],
    "properties": {
        "starttime": {"type": "integer"},
        "endtime": {"type": "integer"},
        "limit": {"type": "integer", "default": 100},
    },
    "additionalProperties": False,
}

REPORTS_LIST = _read_tool(
    "reports.list",
    "wecom_list_reports",
    "列出企业微信汇报记录。",
    _WINDOW_SCHEMA,
)
REPORTS_GET = _read_tool(
    "reports.get",
    "wecom_get_report",
    "获取企业微信汇报详情。",
    {
        "type": "object",
        "required": ["journaluuid"],
        "properties": {"journaluuid": {"type": "string"}},
        "additionalProperties": False,
    },
)
APPROVALS_LIST = _read_tool(
    "approvals.list",
    "wecom_list_approvals",
    "列出企业微信审批记录。",
    _WINDOW_SCHEMA,
)
APPROVALS_GET = _read_tool(
    "approvals.get",
    "wecom_get_approval_detail",
    "获取企业微信审批详情。",
    {
        "type": "object",
        "required": ["sp_no"],
        "properties": {"sp_no": {"type": "string"}},
        "additionalProperties": False,
    },
)
CHECKINS_LIST = _read_tool(
    "checkins.list",
    "wecom_list_checkins",
    "列出企业微信打卡记录。",
    _WINDOW_SCHEMA,
)
SMART_TABLE_RECORDS_LIST = _read_tool(
    "smart_tables.records.list",
    "wecom_list_smart_table_records",
    "查询企业微信智能表格记录。",
    {
        "type": "object",
        "required": ["docid", "sheet_id"],
        "properties": {
            "docid": {"type": "string"},
            "sheet_id": {"type": "string"},
            "limit": {"type": "integer", "default": 1000},
        },
        "additionalProperties": False,
    },
)

_TOOLS = (
    REPORTS_LIST,
    REPORTS_GET,
    APPROVALS_LIST,
    APPROVALS_GET,
    CHECKINS_LIST,
    SMART_TABLE_RECORDS_LIST,
)


class ConnectionSyncStore:
    """Process-local safe sync summaries, keyed strictly by connection ID.

    The durable cursor is still written through ``app.wecom.dispatch`` to the
    existing tenant schema with the connection ID as its filter key.  This
    small summary store gives callers a safe status handoff without retaining
    request/response payloads or credential-bearing exception text.
    """

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def load(self, connection_id: str, resource_key: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._states.get((connection_id, resource_key))
            return None if state is None else dict(state)

    def save(self, connection_id: str, resource_key: str, data: Mapping[str, Any]) -> None:
        with self._lock:
            self._states[(connection_id, resource_key)] = dict(data)


sync_store = ConnectionSyncStore()
_DEFAULT_SYNC_STORE = sync_store


def _use_mock() -> bool:
    return get_settings().wecom_use_mock


def _result_status(data: Mapping[str, Any]) -> str:
    if data.get("partial_count"):
        return "partial"
    if data.get("errcode"):
        return "error"
    return "ok"


def _error_result(
    context: ConnectionContext,
    exc: Exception,
) -> ExecutionResult:
    if isinstance(exc, data_access.PublicDataAccessError):
        data = {
            "tenant": context.tenant_id,
            "source": exc.source,
            "errcode": exc.errcode,
            "errmsg": exc.public_message,
        }
    else:
        data = {
            "tenant": context.tenant_id,
            "source": "db" if context.data_mode == "stored" else "wecom",
            "errcode": 502,
            "errmsg": "数据访问失败",
        }
    # Do not stringify a third-party exception: its message can contain a
    # credential, URL, or raw response body.  The type is enough for operators.
    logger.warning("WeCom data access failed type=%s", type(exc).__name__)
    return ExecutionResult(data=data, status="error")


def _mock_result(context: ConnectionContext, tool_key: str, args: Mapping[str, Any]) -> dict[str, Any]:
    if tool_key == REPORTS_LIST.tool_key:
        return {
            "tenant": context.tenant_id,
            "source": "mock",
            "count": len(MOCK_REPORT_LIST),
            "records": MOCK_REPORT_LIST,
        }
    if tool_key == REPORTS_GET.tool_key:
        return dict(
            MOCK_REPORT_DETAIL.get(
                str(args["journaluuid"]), {"errcode": 404, "errmsg": "不存在"}
            )
        )
    if tool_key == APPROVALS_LIST.tool_key:
        return {
            "tenant": context.tenant_id,
            "source": "mock",
            "count": len(MOCK_APPROVAL_LIST),
            "records": MOCK_APPROVAL_LIST,
        }
    if tool_key == APPROVALS_GET.tool_key:
        return dict(
            MOCK_APPROVAL_DETAIL.get(
                str(args["sp_no"]), {"errcode": 404, "errmsg": "不存在"}
            )
        )
    if tool_key == CHECKINS_LIST.tool_key:
        return {
            "tenant": context.tenant_id,
            "source": "mock",
            "count": 0,
            "records": [],
        }
    if tool_key == SMART_TABLE_RECORDS_LIST.tool_key:
        limit = int(args.get("limit", 1000))
        return {
            "tenant": context.tenant_id,
            "source": "mock",
            "note": "智能表格读取一期暂搁置",
            "records": MOCK_SMARTTABLE_RECORDS[:limit],
        }
    raise KeyError(f"unknown tool_key: {tool_key}")


def _execute_data_access(
    context: ConnectionContext,
    tool_key: str,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    if tool_key == REPORTS_LIST.tool_key:
        return data_access.list_reports(
            context,
            args["starttime"],
            args["endtime"],
            args.get("limit", 100),
        )
    if tool_key == REPORTS_GET.tool_key:
        return data_access.get_report(context, args["journaluuid"])
    if tool_key == APPROVALS_LIST.tool_key:
        return data_access.list_approvals(
            context,
            args["starttime"],
            args["endtime"],
            args.get("limit", 100),
        )
    if tool_key == APPROVALS_GET.tool_key:
        return data_access.get_approval(context, args["sp_no"])
    if tool_key == CHECKINS_LIST.tool_key:
        return data_access.list_checkins(
            context,
            args["starttime"],
            args["endtime"],
            args.get("limit", 100),
        )
    if tool_key == SMART_TABLE_RECORDS_LIST.tool_key:
        return _mock_result(context, tool_key, args)
    raise KeyError(f"unknown tool_key: {tool_key}")


def _safe_sync_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Select only scalar summary fields safe for results and status storage."""
    safe: dict[str, Any] = {}
    for key in (
        "pulled",
        "stored",
        "err",
        "write_err",
        "partial_count",
        "busy",
        "skipped",
    ):
        value = data.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if value is not None:
                safe[key] = value
    return safe


def _sync_status(summary: Mapping[str, Any], raw_data: Mapping[str, Any]) -> str:
    if raw_data.get("error") or summary.get("busy"):
        return "error"
    if summary.get("err") or summary.get("write_err") or summary.get("partial_count"):
        return "partial"
    return "ok"


class WeComConnector:
    """Trusted code connector wrapping the existing WeCom data paths."""

    connector_key = "wecom"

    def __init__(
        self,
        *,
        sync_store: ConnectionSyncStore | None = None,
        mock_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._sync_store = sync_store if sync_store is not None else _DEFAULT_SYNC_STORE
        self._mock_enabled = mock_enabled

    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            connector_key=self.connector_key,
            tools=_TOOLS,
            supports_sync=True,
            version="1",
            config_schema={
                "type": "object",
                "properties": {
                    "corpid": {"type": "string"},
                    "schema_name": {"type": "string"},
                    "checkin_userids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            credential_schema={
                "type": "object",
                "properties": {
                    "wecom_app_secret": {"type": "string", "writeOnly": True},
                    "wecom_contact_secret": {"type": "string", "writeOnly": True},
                },
            },
        )

    def execute_sync(
        self,
        context: ConnectionContext,
        tool_key: str,
        args: dict[str, Any],
    ) -> ExecutionResult:
        if not isinstance(args, dict):
            raise TypeError("args must be a dict")
        resolved_key = self.spec().tool(tool_key).tool_key
        mock_enabled = self._mock_enabled or _use_mock
        try:
            data = (
                _mock_result(context, resolved_key, args)
                if mock_enabled()
                else _execute_data_access(context, resolved_key, args)
            )
        except Exception as exc:
            return _error_result(context, exc)
        return ExecutionResult(data=data, status=_result_status(data))

    async def execute(
        self,
        context: ConnectionContext,
        tool_key: str,
        args: dict[str, Any],
    ) -> ExecutionResult:
        return self.execute_sync(context, tool_key, args)

    async def sync(
        self,
        context: ConnectionContext,
        resource_key: str,
    ) -> SyncResult:
        try:
            raw_data = dispatch.run_sync_connection(context, resource_key)
        except Exception as exc:
            logger.warning("WeCom sync failed type=%s", type(exc).__name__)
            raw_data = {"error": "sync_failed"}
        summary = _safe_sync_data(raw_data)
        status = _sync_status(summary, raw_data)
        self._sync_store.save(context.connection_id, resource_key, summary)
        return SyncResult(
            connection_id=context.connection_id,
            resource_key=resource_key,
            data=summary,
            status=status,
        )


def legacy_connection_context(legacy_context: TenantCtx) -> ConnectionContext:
    """Build a safe, stable connection scope for the temporary ``/mcp`` path."""
    connection_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"wbsysc:legacy-wecom:{legacy_context.tenant_id}",
        )
    )
    return ConnectionContext(
        connection=ConnectionRecord(
            connection_id=connection_id,
            tenant_id=legacy_context.tenant_id,
            connector_key="wecom",
            display_name=f"WeCom ({legacy_context.tenant_id})",
            status="active",
            data_mode=legacy_context.data_mode,
            public_config={
                "corpid": legacy_context.corpid,
                "schema_name": legacy_context.schema_name,
                "checkin_userids": list(legacy_context.checkin_userids),
                "enabled_modules": sorted(legacy_context.enabled_modules),
            },
            config_version=0,
        ),
        credentials={
            "wecom_app_secret": legacy_context.secret,
            "wecom_contact_secret": legacy_context.contact_secret,
        },
    )


class LegacyWeComAdapter:
    """Small compatibility facade for the old FastMCP function signatures."""

    def __init__(
        self,
        connector: WeComConnector,
        legacy_context_provider: Callable[[], TenantCtx],
        audit: Callable[[str, str, str, str, int], None],
    ) -> None:
        self._connector = connector
        self._legacy_context_provider = legacy_context_provider
        self._audit = audit

    def execute(
        self,
        tool_key: str,
        args: dict[str, Any],
        *,
        target: str = "",
        params: str = "",
    ) -> str:
        started_at = time.time()
        context = legacy_connection_context(self._legacy_context_provider())
        result = self._connector.execute_sync(context, tool_key, args)
        self._audit(
            self._connector.spec().tool(tool_key).mcp_name,
            target,
            params,
            result.status,
            int((time.time() - started_at) * 1000),
        )
        return json.dumps(result.data, ensure_ascii=False, default=str)
