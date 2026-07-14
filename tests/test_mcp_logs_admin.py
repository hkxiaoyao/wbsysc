import math
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import mcp_logs_admin as api
from app.mcp_log_models import DeleteSpec, LogFilters


def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


@pytest.fixture
def authed_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(api, "_require_auth", lambda request: None)
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(
            app_env="test",
            credential_key="credential-key-for-tests",
            admin_password="admin-password-for-tests",
        ),
    )
    client = make_client()
    client.headers.update({"Authorization": "Bearer session-a"})
    return client


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/admin/mcp-logs", None),
        ("get", "/admin/mcp-logs/stats", None),
        ("post", "/admin/mcp-logs/delete-preview", {"mode": "all"}),
        ("post", "/admin/mcp-logs/delete", {"mode": "all", "confirm_token": "x"}),
        ("delete", "/admin/mcp-logs", {"mode": "all", "confirm_token": "x"}),
        ("get", "/admin/mcp-log-settings", None),
        ("put", "/admin/mcp-log-settings", {"retention_days": 90}),
    ],
)
def test_every_route_requires_admin_session(method, path, body):
    response = make_client().request(method, path, json=body)
    assert response.status_code == 401


def test_list_logs_defaults_to_24_hours_and_whitelists_output(
    monkeypatch, authed_client
):
    now = datetime(2026, 7, 14, 12, 0)
    captured = {}
    monkeypatch.setattr(api, "_utcnow", lambda: now)

    def fake_list(filters, page, page_size):
        captured.update(filters=filters, page=page, page_size=page_size)
        return {
            "items": [{
                "id": 2**53 + 1,
                "tenant_id": "tenant-a",
                "category": "tool",
                "event_name": "wecom_list_reports",
                "target": "report",
                "params_summary": "safe",
                "result_status": "ok",
                "error_code": "",
                "error_summary": "",
                "cost_ms": 12,
                "request_id": "req-1",
                "client_ip": "203.0.113.8",
                "http_method": "POST",
                "http_status": 200,
                "created_at": now,
                "legacy_schema": "must-not-leak",
                "authorization": "must-not-leak",
            }],
            "total": 1,
            "page": page,
            "page_size": page_size,
            "sql": "must-not-leak",
        }

    monkeypatch.setattr(api, "list_logs", fake_list)

    response = authed_client.get("/admin/mcp-logs")

    assert response.status_code == 200
    assert captured["filters"] == LogFilters(
        from_time=now - timedelta(hours=24), to_time=now
    )
    assert captured["page"] == 1
    assert captured["page_size"] == 20
    payload = response.json()
    assert set(payload) == {"items", "total", "page", "page_size"}
    assert set(payload["items"][0]) == api.SAFE_LOG_FIELDS
    assert payload["items"][0]["id"] == "9007199254740993"


def test_list_converts_only_structured_query_fields(monkeypatch, authed_client):
    captured = {}
    monkeypatch.setattr(
        api,
        "list_logs",
        lambda filters, page, page_size: captured.update(filters=filters)
        or {"items": [], "total": 0, "page": page, "page_size": page_size},
    )

    response = authed_client.get(
        "/admin/mcp-logs",
        params={
            "tenant_id": "tenant-a",
            "category": "auth",
            "event_name": "auth_invalid",
            "status": "denied",
            "from": "2026-07-13T00:00:00Z",
            "to": "2026-07-14T00:00:00+00:00",
            "q": r"50%_done\\ok",
            "request_id": "req-42",
            "client_ip": "203.0.113.8",
            "cost_min": 5,
            "cost_max": 50,
            "raw_sql": "DROP TABLE mcp_call_log",
        },
    )

    assert response.status_code == 200
    filters = captured["filters"]
    assert isinstance(filters, LogFilters)
    assert filters.tenant_id == "tenant-a"
    assert filters.category == "auth"
    assert filters.from_time == datetime(2026, 7, 13)
    assert filters.to_time == datetime(2026, 7, 14)
    assert filters.q == r"50%_done\\ok"
    assert not hasattr(filters, "raw_sql")


@pytest.mark.parametrize(
    "query",
    [
        {"category": "other"},
        {"status": "unknown"},
        {"from": "2026-07-14T00:00:00Z", "to": "2026-07-13T00:00:00Z"},
        {"page_size": 101},
        {"page": 0},
        {"q": "x" * 101},
        {"cost_min": -1},
        {"cost_min": 50, "cost_max": 5},
    ],
)
def test_list_rejects_invalid_query_bounds(authed_client, query):
    assert authed_client.get("/admin/mcp-logs", params=query).status_code == 422


def test_stats_uses_same_filters_and_safe_shape(monkeypatch, authed_client):
    captured = {}
    monkeypatch.setattr(
        api,
        "get_log_stats",
        lambda filters: captured.update(filters=filters) or {
            "total": 3,
            "success_rate": 66.67,
            "error_count": 1,
            "avg_cost_ms": 10.5,
            "p95_cost_ms": 20,
            "trend": [{"bucket": "2026-07-14 12:00:00", "count": 3, "sql": "x"}],
            "top_tools": [{"event_name": "tool-a", "count": 2, "secret": "x"}],
            "status_distribution": [{"result_status": "ok", "count": 2}],
            "internal": "must-not-leak",
        },
    )

    response = authed_client.get(
        "/admin/mcp-logs/stats",
        params={"category": "tool", "from": "2026-07-13T00:00:00Z", "to": "2026-07-14T00:00:00Z"},
    )

    assert response.status_code == 200
    assert isinstance(captured["filters"], LogFilters)
    assert response.json() == {
        "total": 3,
        "success_rate": 66.67,
        "error_count": 1,
        "avg_cost_ms": 10.5,
        "p95_cost_ms": 20,
        "trend": [{"bucket": "2026-07-14 12:00:00", "count": 3}],
        "top_tools": [{"event_name": "tool-a", "count": 2}],
        "status_distribution": [{"result_status": "ok", "count": 2}],
    }


@pytest.mark.parametrize(
    ("body", "expected_mode"),
    [
        ({"mode": "ids", "ids": [9, 2, 9]}, "ids"),
        ({"mode": "filter", "filter": {"tenant_id": "tenant-a", "status": "error"}}, "filter"),
        ({"mode": "before_date", "before_date": "2026-07-01T00:00:00Z"}, "before_date"),
        ({"mode": "all"}, "all"),
    ],
)
def test_delete_preview_builds_typed_specs(
    monkeypatch, authed_client, body, expected_mode
):
    captured = {}
    monkeypatch.setattr(api, "_now", lambda: 1_000.0)
    monkeypatch.setattr(
        api,
        "preview_delete",
        lambda spec: captured.update(spec=spec) or {"matched_count": 3, "max_id": 12},
    )

    response = authed_client.post("/admin/mcp-logs/delete-preview", json=body)

    assert response.status_code == 200
    assert isinstance(captured["spec"], DeleteSpec)
    assert captured["spec"].mode == expected_mode
    assert response.json()["matched_count"] == 3
    assert response.json()["max_id"] == 12
    assert response.json()["expires_at"] == 1_300
    assert response.json()["confirm_token"]


def test_maximum_id_batch_fits_confirmation_token_and_execute_binds_canonical_spec(
    monkeypatch, authed_client
):
    max_bigint = 2**63 - 1
    ids = [str(max_bigint - index) for index in range(200)]
    normalized_ids = tuple(sorted(int(value) for value in ids))
    captured = {}
    monkeypatch.setattr(api, "_now", lambda: 1_000.0)
    monkeypatch.setattr(
        api,
        "preview_delete",
        lambda spec: {"matched_count": len(spec.ids), "max_id": max_bigint},
    )
    monkeypatch.setattr(
        api,
        "delete_matching",
        lambda spec, max_id: captured.update(spec=spec, max_id=max_id) or len(spec.ids),
    )

    preview_response = authed_client.post(
        "/admin/mcp-logs/delete-preview",
        json={"mode": "ids", "ids": ids},
    )

    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert len(preview["confirm_token"]) <= 8192

    execute_response = authed_client.post(
        "/admin/mcp-logs/delete",
        json={
            "mode": "ids",
            "ids": list(reversed(ids)),
            "confirm_token": preview["confirm_token"],
        },
    )

    assert execute_response.status_code == 200
    assert execute_response.json() == {"deleted": 200}
    assert captured["max_id"] == max_bigint
    assert captured["spec"] == DeleteSpec(mode="ids", ids=normalized_ids)


def test_delete_confirmation_binds_deduplicated_id_spec(monkeypatch, authed_client):
    captured = {}
    monkeypatch.setattr(api, "_now", lambda: 1_000.0)
    monkeypatch.setattr(
        api,
        "preview_delete",
        lambda spec: {"matched_count": len(spec.ids), "max_id": 9},
    )
    monkeypatch.setattr(
        api,
        "delete_matching",
        lambda spec, max_id: captured.update(spec=spec, max_id=max_id) or len(spec.ids),
    )
    preview = authed_client.post(
        "/admin/mcp-logs/delete-preview",
        json={"mode": "ids", "ids": ["9", 2, "9"]},
    ).json()

    response = authed_client.post(
        "/admin/mcp-logs/delete",
        json={"mode": "ids", "ids": ["2", "9"], "confirm_token": preview["confirm_token"]},
    )

    assert response.status_code == 200
    assert captured["spec"] == DeleteSpec(mode="ids", ids=(2, 9))


def test_delete_accepts_decimal_strings_and_safe_ints_across_bigint_range(
    monkeypatch, authed_client
):
    max_bigint = 2**63 - 1
    max_safe_integer = 2**53 - 1
    captured = {}
    monkeypatch.setattr(api, "_now", lambda: 1_000.0)
    monkeypatch.setattr(
        api,
        "preview_delete",
        lambda spec: captured.update(preview_spec=spec)
        or {"matched_count": len(spec.ids), "max_id": max_bigint},
    )
    monkeypatch.setattr(
        api,
        "delete_matching",
        lambda spec, max_id: captured.update(execute_spec=spec, max_id=max_id)
        or len(spec.ids),
    )
    preview = authed_client.post(
        "/admin/mcp-logs/delete-preview",
        json={
            "mode": "ids",
            "ids": [str(max_bigint), max_safe_integer, "9007199254740993", "1"],
        },
    )

    assert preview.status_code == 200
    expected = DeleteSpec(
        mode="ids",
        ids=(1, max_safe_integer, 9007199254740993, max_bigint),
    )
    assert captured["preview_spec"] == expected

    response = authed_client.post(
        "/admin/mcp-logs/delete",
        json={
            "mode": "ids",
            "ids": [
                "1",
                str(max_bigint),
                "9007199254740993",
                str(max_safe_integer),
            ],
            "confirm_token": preview.json()["confirm_token"],
        },
    )

    assert response.status_code == 200
    assert captured["execute_spec"] == expected


@pytest.mark.parametrize(
    "invalid_id",
    [
        0,
        -1,
        2**53,
        2**63,
        True,
        1.0,
        "0",
        "-1",
        "+1",
        " 1",
        "1 ",
        "01",
        "1.0",
        "9223372036854775808",
        "１",
    ],
)
def test_delete_rejects_noncanonical_or_out_of_bigint_range_ids(
    authed_client, invalid_id
):
    response = authed_client.post(
        "/admin/mcp-logs/delete-preview",
        json={"mode": "ids", "ids": [invalid_id]},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/admin/mcp-logs/delete-preview", {"mode": "ids", "ids": list(range(1, 202))}),
        (
            "/admin/mcp-logs/delete",
            {"mode": "ids", "ids": list(range(1, 202)), "confirm_token": "x"},
        ),
    ],
)
def test_delete_models_reject_more_than_two_hundred_ids(authed_client, path, body):
    assert authed_client.post(path, json=body).status_code == 422


def test_delete_preview_and_execute_bind_same_snapshot(monkeypatch, authed_client):
    captured = {}
    monkeypatch.setattr(api, "_now", lambda: 1_000.0)
    monkeypatch.setattr(
        api,
        "preview_delete",
        lambda spec: {"matched_count": 3, "max_id": 12},
    )
    monkeypatch.setattr(
        api,
        "delete_matching",
        lambda spec, max_id: captured.update(spec=spec, max_id=max_id) or 3,
    )

    preview = authed_client.post(
        "/admin/mcp-logs/delete-preview", json={"mode": "all"}
    ).json()
    response = authed_client.request(
        "DELETE",
        "/admin/mcp-logs",
        json={"mode": "all", "confirm_token": preview["confirm_token"]},
    )

    assert response.status_code == 200
    assert response.json() == {"deleted": preview["matched_count"]}
    assert captured["max_id"] == preview["max_id"]
    assert captured["spec"] == DeleteSpec(mode="all")


def test_delete_token_rejects_tampering_changed_spec_session_and_expiry(
    monkeypatch, authed_client
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(api, "_now", lambda: clock["now"])
    monkeypatch.setattr(
        api, "preview_delete", lambda spec: {"matched_count": 1, "max_id": 7}
    )
    monkeypatch.setattr(api, "delete_matching", lambda spec, max_id: 1)
    preview = authed_client.post(
        "/admin/mcp-logs/delete-preview", json={"mode": "all"}
    ).json()
    token = preview["confirm_token"]

    payload, signature = token.split(".", 1)
    tampered = f"{payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    assert authed_client.request(
        "DELETE", "/admin/mcp-logs", json={"mode": "all", "confirm_token": tampered}
    ).status_code == 400
    assert authed_client.request(
        "DELETE",
        "/admin/mcp-logs",
        json={"mode": "ids", "ids": [7], "confirm_token": token},
    ).status_code == 400
    assert authed_client.request(
        "DELETE",
        "/admin/mcp-logs",
        headers={"Authorization": "Bearer session-b"},
        json={"mode": "all", "confirm_token": token},
    ).status_code == 400
    clock["now"] = 1_301.0
    assert authed_client.request(
        "DELETE", "/admin/mcp-logs", json={"mode": "all", "confirm_token": token}
    ).status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "ids", "ids": []},
        {"mode": "filter", "filter": {}},
        {"mode": "before_date"},
        {"mode": "all", "ids": [1]},
        {"mode": "sql", "raw_sql": "DELETE FROM mcp_call_log"},
    ],
)
def test_delete_preview_rejects_ambiguous_or_raw_specs(authed_client, body):
    assert authed_client.post("/admin/mcp-logs/delete-preview", json=body).status_code == 422


@pytest.mark.parametrize("field", ["cost_min", "cost_max"])
@pytest.mark.parametrize("value", ["10", 10.0, True, -1])
def test_delete_filter_costs_require_strict_nonnegative_integers(
    authed_client, field, value
):
    response = authed_client.post(
        "/admin/mcp-logs/delete-preview",
        json={
            "mode": "filter",
            "filter": {"tenant_id": "tenant-a", field: value},
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("operation", "method", "path", "body", "malformed"),
    [
        ("list_logs", "get", "/admin/mcp-logs", None, None),
        (
            "get_log_stats",
            "get",
            "/admin/mcp-logs/stats",
            None,
            {"trend": [None]},
        ),
        (
            "preview_delete",
            "post",
            "/admin/mcp-logs/delete-preview",
            {"mode": "all"},
            {"matched_count": "not-an-integer", "max_id": 12},
        ),
        ("get_retention_days", "get", "/admin/mcp-log-settings", None, None),
        (
            "set_retention_days",
            "put",
            "/admin/mcp-log-settings",
            {"retention_days": 90},
            "not-an-integer",
        ),
    ],
)
def test_malformed_store_results_return_generic_service_error(
    monkeypatch, authed_client, operation, method, path, body, malformed
):
    monkeypatch.setattr(api, operation, lambda *args: malformed)
    client = TestClient(authed_client.app, raise_server_exceptions=False)
    client.headers.update({"Authorization": "Bearer session-a"})

    response = client.request(method, path, json=body)

    assert response.status_code == 500
    assert response.json() == {"detail": "日志服务暂不可用"}


def test_malformed_delete_count_returns_generic_service_error(
    monkeypatch, authed_client
):
    monkeypatch.setattr(
        api,
        "preview_delete",
        lambda spec: {"matched_count": 1, "max_id": 7},
    )
    preview = authed_client.post(
        "/admin/mcp-logs/delete-preview", json={"mode": "all"}
    ).json()
    monkeypatch.setattr(api, "delete_matching", lambda spec, max_id: "not-an-integer")
    client = TestClient(authed_client.app, raise_server_exceptions=False)
    client.headers.update({"Authorization": "Bearer session-a"})

    response = client.request(
        "delete",
        "/admin/mcp-logs",
        json={"mode": "all", "confirm_token": preview["confirm_token"]},
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "日志服务暂不可用"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exp", True),
        ("exp", "300"),
        ("exp", math.inf),
        ("exp", 10**400),
        ("max_id", True),
        ("count", True),
    ],
)
def test_confirmation_rejects_non_strict_or_non_finite_numeric_claims(
    monkeypatch, authed_client, field, value
):
    monkeypatch.setattr(api, "_now", lambda: 0.0)
    monkeypatch.setattr(
        api,
        "preview_delete",
        lambda spec: {"matched_count": 1, "max_id": 7},
    )
    monkeypatch.setattr(api, "delete_matching", lambda spec, max_id: 1)
    preview = authed_client.post(
        "/admin/mcp-logs/delete-preview", json={"mode": "all"}
    ).json()
    payload = api._decode_confirmation(preview["confirm_token"])
    payload[field] = value
    malformed_token = api._confirmation_token(payload)

    response = authed_client.request(
        "DELETE",
        "/admin/mcp-logs",
        json={"mode": "all", "confirm_token": malformed_token},
    )

    assert response.status_code == 400


def test_retention_get_and_strict_put(monkeypatch, authed_client):
    saved = []
    monkeypatch.setattr(api, "get_retention_days", lambda: 90)
    monkeypatch.setattr(
        api, "set_retention_days", lambda days: saved.append(days) or days
    )

    assert authed_client.get("/admin/mcp-log-settings").json() == {
        "retention_days": 90
    }
    assert authed_client.put(
        "/admin/mcp-log-settings", json={"retention_days": 0}
    ).json() == {"retention_days": 0}
    assert authed_client.put(
        "/admin/mcp-log-settings", json={"retention_days": 3650}
    ).json() == {"retention_days": 3650}
    assert saved == [0, 3650]


@pytest.mark.parametrize("value", [-1, 3651, True, "90", 1.5])
def test_retention_rejects_invalid_or_non_integer_values(authed_client, value):
    response = authed_client.put(
        "/admin/mcp-log-settings", json={"retention_days": value}
    )
    assert response.status_code == 422


def test_store_failures_return_generic_error(monkeypatch, authed_client):
    monkeypatch.setattr(
        api,
        "list_logs",
        lambda *args: (_ for _ in ()).throw(
            RuntimeError("SELECT secret FROM tenant_config token=abc")
        ),
    )

    response = authed_client.get("/admin/mcp-logs")

    assert response.status_code == 500
    body = response.text.lower()
    assert "select" not in body
    assert "secret" not in body
    assert "token" not in body
