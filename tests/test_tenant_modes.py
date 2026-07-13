import pytest
from sqlalchemy.exc import OperationalError

from app import tenant as tenant_store
from app.tenant import _TenantCtx
from app.wecom import dispatch


def tenant(mode="stored"):
    return _TenantCtx(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="secret",
        schema_name="wbd_123",
        sync_interval_min=30,
        enabled_modules={"report"},
        checkin_userids=[],
        contact_secret="",
        data_mode=mode,
    )


def test_direct_tenant_is_not_synchronized(monkeypatch):
    monkeypatch.setattr(dispatch, "get_all_tenants", lambda: [tenant("direct")])
    monkeypatch.setattr(
        dispatch,
        "run_sync_tenant",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not sync")),
    )
    assert dispatch.run_sync_all() == {"tenant-a": {"skipped": "direct_mode"}}


def test_stored_tenant_runs_sync(monkeypatch):
    monkeypatch.setattr(dispatch, "get_all_tenants", lambda: [tenant("stored")])
    monkeypatch.setattr(dispatch, "run_sync_tenant", lambda *args, **kwargs: {"report": {"stored": 1}})
    assert dispatch.run_sync_all()["tenant-a"]["report"]["stored"] == 1


def test_run_sync_tenant_direct_skips_before_lock_or_writes(monkeypatch):
    context = tenant("direct")
    monkeypatch.setattr(
        dispatch.db,
        "tenant_sync_lock",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("direct mode must not acquire sync lock")
        ),
    )
    monkeypatch.setattr(
        dispatch,
        "_sync_one_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("direct mode must not write stored data")
        ),
    )

    assert dispatch.run_sync_tenant(context) == {"skipped": "direct_mode"}


class TenantRows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class TenantConnection:
    def __init__(self, first_error=None):
        self.first_error = first_error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement):
        self.calls.append(str(statement))
        if len(self.calls) == 1 and self.first_error:
            raise self.first_error
        return TenantRows([
            ("tenant-a", "ww123", None, "token-a", "wbd_123", 30,
             "report", "", None)
        ])


class TenantEngine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection


def mysql_unknown_column(column):
    return OperationalError(
        "SELECT",
        {},
        Exception(1054, f"Unknown column '{column}' in 'field list'"),
    )


def test_tenant_loader_falls_back_only_when_data_mode_is_missing(monkeypatch, caplog):
    connection = TenantConnection(mysql_unknown_column("data_mode"))
    monkeypatch.setattr(tenant_store, "get_engine", lambda: TenantEngine(connection))

    loaded = tenant_store._load_all()

    assert loaded["token-a"].data_mode == "stored"
    assert len(connection.calls) == 2
    assert "data_mode" in connection.calls[0]
    assert "data_mode" not in connection.calls[1]
    assert "data_mode" in caplog.text


def test_tenant_loader_does_not_hide_other_unknown_columns(monkeypatch):
    connection = TenantConnection(mysql_unknown_column("enabled_modules"))
    monkeypatch.setattr(tenant_store, "get_engine", lambda: TenantEngine(connection))

    with pytest.raises(OperationalError):
        tenant_store._load_all()

    assert len(connection.calls) == 1
