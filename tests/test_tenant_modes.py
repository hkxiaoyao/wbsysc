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
