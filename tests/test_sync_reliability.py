from app.tenant import _TenantCtx
from app.wecom import dispatch


def test_bounded_windows_processes_every_identifier():
    calls = []

    def fetch(start, end):
        calls.append((start, end))
        count = 600 if end - start > 60 else 300
        return [f"{start}-{index}" for index in range(count)]

    batches = list(dispatch._bounded_windows(fetch, 0, 120, limit=500))
    assert sum(len(items) for _, _, items in batches) == 600
    assert all(len(items) <= 500 for _, _, items in batches)


def test_force_does_not_reset_cursor(monkeypatch):
    tenant = _TenantCtx("t", "ww", "s", "wbd_x", 30, set(), [], "", "stored")
    reset_calls = []
    monkeypatch.setattr(dispatch, "reset_cursors", lambda *args: reset_calls.append(args))
    dispatch.run_sync_tenant(tenant, force=True, reset_cursor=False)
    assert reset_calls == []


def test_reset_cursor_persists_reset(monkeypatch):
    tenant = _TenantCtx("t", "ww", "s", "wbd_x", 30, set(), [], "", "stored")
    reset_calls = []
    monkeypatch.setattr(dispatch, "reset_cursors", lambda *args: reset_calls.append(args))
    dispatch.run_sync_tenant(tenant, force=False, reset_cursor=True)
    assert len(reset_calls) == 1


def report_tenant():
    return _TenantCtx("t", "ww", "s", "wbd_x", 30, {"report"}, [], "", "stored")


def test_report_sync_processes_all_identifiers(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (0, 120))
    monkeypatch.setattr(
        dispatch,
        "sync_reports_window",
        lambda corpid, secret, start, end: [
            f"{start}-{index}" for index in range(700 if end - start > 60 else 350)
        ],
    )
    monkeypatch.setattr(
        dispatch,
        "fetch_report_detail",
        lambda *args: {"journaluuid": args[-1], "report_time": 10},
    )
    stored = []
    cursors = []
    monkeypatch.setattr(
        dispatch.db,
        "upsert_report",
        lambda schema, identifier, info, source_window=None: stored.append(identifier),
    )
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: cursors.append(args))
    result = dispatch._sync_one_report(report_tenant(), 30)
    assert len(set(stored)) == 700
    assert result["stored"] == 700
    assert len(cursors) == 1


def test_report_write_failure_blocks_cursor(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (0, 60))
    monkeypatch.setattr(dispatch, "sync_reports_window", lambda *args: ["r1"])
    monkeypatch.setattr(
        dispatch,
        "fetch_report_detail",
        lambda *args: {"journaluuid": "r1", "report_time": 10},
    )
    monkeypatch.setattr(
        dispatch.db,
        "upsert_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    cursors = []
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: cursors.append(args))
    result = dispatch._sync_one_report(report_tenant(), 30)
    assert result["write_err"] == 1
    assert cursors == []


def test_partial_report_keeps_source_window(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (10, 70))
    monkeypatch.setattr(dispatch, "sync_reports_window", lambda *args: ["r1"])
    monkeypatch.setattr(
        dispatch,
        "fetch_report_detail",
        lambda *args: {"errcode": 40001, "errmsg": "bad secret"},
    )
    captured = {}
    monkeypatch.setattr(
        dispatch.db,
        "upsert_report",
        lambda schema, identifier, info, source_window=None: captured.update(
            info=info, source_window=source_window
        ),
    )
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: None)
    dispatch._sync_one_report(report_tenant(), 30)
    assert captured["info"]["_partial"] is True
    assert captured["source_window"] == (10, 70)


def approval_tenant():
    return _TenantCtx("t", "ww", "s", "wbd_x", 30, {"approval"}, [], "", "stored")


def test_approval_write_failure_blocks_cursor(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (0, 60))
    monkeypatch.setattr(dispatch, "sync_approvals_window", lambda *args: ["sp1"])
    monkeypatch.setattr(
        dispatch,
        "fetch_approval_detail",
        lambda *args: {"sp_no": "sp1", "apply_time": 10},
    )
    monkeypatch.setattr(
        dispatch.db,
        "upsert_approval",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    cursors = []
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: cursors.append(args))
    result = dispatch._sync_one_approval(approval_tenant(), 30)
    assert result["write_err"] == 1
    assert cursors == []


def test_partial_approval_keeps_source_window(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (10, 70))
    monkeypatch.setattr(dispatch, "sync_approvals_window", lambda *args: ["sp1"])
    monkeypatch.setattr(
        dispatch,
        "fetch_approval_detail",
        lambda *args: {"errcode": 301055, "errmsg": "forbidden"},
    )
    captured = {}
    monkeypatch.setattr(
        dispatch.db,
        "upsert_approval",
        lambda schema, identifier, info, source_window=None: captured.update(
            info=info, source_window=source_window
        ),
    )
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: None)
    dispatch._sync_one_approval(approval_tenant(), 30)
    assert captured["info"]["_partial"] is True
    assert captured["source_window"] == (10, 70)
