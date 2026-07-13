from contextlib import contextmanager

from app import db
from app.tenant import _TenantCtx
from app.wecom import dispatch
from app.wecom.checkin_sync import CheckinFetchResult


@contextmanager
def acquired_sync_lock(*args, **kwargs):
    yield True


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
    monkeypatch.setattr(dispatch.db, "tenant_sync_lock", acquired_sync_lock)
    monkeypatch.setattr(dispatch, "reset_cursors", lambda *args: reset_calls.append(args))
    dispatch.run_sync_tenant(tenant, force=True, reset_cursor=False)
    assert reset_calls == []


def test_reset_cursor_persists_reset(monkeypatch):
    tenant = _TenantCtx("t", "ww", "s", "wbd_x", 30, set(), [], "", "stored")
    reset_calls = []
    monkeypatch.setattr(dispatch.db, "tenant_sync_lock", acquired_sync_lock)
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


def test_report_detail_exception_is_stored_as_partial(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (10, 70))
    monkeypatch.setattr(dispatch, "sync_reports_window", lambda *args: ["r1"])
    monkeypatch.setattr(
        dispatch,
        "fetch_report_detail",
        lambda *args: (_ for _ in ()).throw(RuntimeError("detail timeout")),
    )
    captured = {}
    monkeypatch.setattr(
        dispatch.db,
        "upsert_report",
        lambda schema, identifier, info, source_window=None: captured.update(
            info=info, source_window=source_window
        ),
    )
    cursors = []
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: cursors.append(args))

    result = dispatch._sync_one_report(report_tenant(), 30)

    assert captured["info"]["_partial"] is True
    assert captured["source_window"] == (10, 70)
    assert result["stored"] == 1
    assert result["err"] == 1
    assert len(cursors) == 1


def test_report_detail_exception_partial_write_failure_blocks_cursor(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (10, 70))
    monkeypatch.setattr(dispatch, "sync_reports_window", lambda *args: ["r1"])
    monkeypatch.setattr(
        dispatch,
        "fetch_report_detail",
        lambda *args: (_ for _ in ()).throw(RuntimeError("detail timeout")),
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


def test_approval_detail_exception_is_stored_as_partial(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (10, 70))
    monkeypatch.setattr(dispatch, "sync_approvals_window", lambda *args: ["sp1"])
    monkeypatch.setattr(
        dispatch,
        "fetch_approval_detail",
        lambda *args: (_ for _ in ()).throw(RuntimeError("detail timeout")),
    )
    captured = {}
    monkeypatch.setattr(
        dispatch.db,
        "upsert_approval",
        lambda schema, identifier, info, source_window=None: captured.update(
            info=info, source_window=source_window
        ),
    )
    cursors = []
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: cursors.append(args))

    result = dispatch._sync_one_approval(approval_tenant(), 30)

    assert captured["info"]["_partial"] is True
    assert captured["source_window"] == (10, 70)
    assert result["stored"] == 1
    assert result["err"] == 1
    assert len(cursors) == 1


class RecordingConnection:
    def __init__(self, lock_result=1):
        self.lock_result = lock_result
        self.statements = []
        self.params = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        self.params.append(params or {})
        value = self.lock_result if "GET_LOCK" in sql else 1

        class Result:
            def scalar(self):
                return value

        return Result()


class RecordingEngine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection

    def begin(self):
        return self.connection


def test_partial_upserts_guard_existing_complete_rows(monkeypatch):
    connection = RecordingConnection()
    monkeypatch.setattr(db, "get_engine", lambda: RecordingEngine(connection))

    db.upsert_report("wbd_x", "r1", {"_partial": True})
    db.upsert_approval("wbd_x", "sp1", {"_partial": True})
    db.upsert_report("wbd_x", "r1", {"report_time": 10})
    db.upsert_approval("wbd_x", "sp1", {"apply_time": 10})

    report_sql, approval_sql = connection.statements[:2]
    guard = "is_partial=0 AND VALUES(is_partial)=1"
    assert guard in report_sql
    assert guard in approval_sql
    assert "is_partial=IF(" in report_sql
    assert "is_partial=IF(" in approval_sql
    assert connection.params[0]["partial"] == 1
    assert connection.params[1]["partial"] == 1
    assert connection.params[2]["partial"] == 0
    assert connection.params[3]["partial"] == 0


def checkin_tenant():
    return _TenantCtx(
        "t", "ww", "s", "wbd_x", 30, {"checkin"}, ["u1"], "", "stored"
    )


def test_checkin_source_failure_blocks_cursor_and_returns_summary(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (0, 60))
    monkeypatch.setattr(
        dispatch,
        "fetch_checkin_records_with_stats",
        lambda *args: CheckinFetchResult(
            records=[{"userid": "u1", "checkin_time": 10}],
            attempted=2,
            failed=1,
            errors=[{"userid": "u2", "errcode": 301021, "errmsg": "invisible"}],
        ),
    )
    monkeypatch.setattr(dispatch.db, "upsert_checkin", lambda *args: None)
    cursors = []
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: cursors.append(args))

    result = dispatch._sync_one_checkin(checkin_tenant(), 30)

    assert result["stored"] == 1
    assert result["partial_count"] == 1
    assert result["errors"][0]["userid"] == "u2"
    assert cursors == []


def test_checkin_write_failure_blocks_cursor(monkeypatch):
    monkeypatch.setattr(dispatch, "_window", lambda *args, **kwargs: (0, 60))
    monkeypatch.setattr(
        dispatch,
        "fetch_checkin_records_with_stats",
        lambda *args: CheckinFetchResult(
            records=[{"userid": "u1", "checkin_time": 10}],
            attempted=1,
            failed=0,
            errors=[],
        ),
    )
    monkeypatch.setattr(
        dispatch.db,
        "upsert_checkin",
        lambda *args: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    cursors = []
    monkeypatch.setattr(dispatch.db, "save_cursor", lambda *args: cursors.append(args))

    result = dispatch._sync_one_checkin(checkin_tenant(), 30)

    assert result["write_err"] == 1
    assert cursors == []


def test_tenant_sync_lock_uses_one_connection_for_acquire_and_release(monkeypatch):
    connection = RecordingConnection()
    monkeypatch.setattr(db, "get_engine", lambda: RecordingEngine(connection))

    with db.tenant_sync_lock("wbd_x") as acquired:
        assert acquired is True

    assert "GET_LOCK" in connection.statements[0]
    assert "RELEASE_LOCK" in connection.statements[1]


def test_busy_tenant_does_not_reset_or_sync(monkeypatch):
    tenant = report_tenant()

    @contextmanager
    def busy_lock(*args, **kwargs):
        yield False

    monkeypatch.setattr(dispatch.db, "tenant_sync_lock", busy_lock)
    monkeypatch.setattr(
        dispatch,
        "reset_cursors",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not reset")),
    )
    monkeypatch.setattr(
        dispatch,
        "_sync_one_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not sync")),
    )

    result = dispatch.run_sync_tenant(tenant, reset_cursor=True)

    assert result["busy"] is True
    assert "error" in result


def test_lock_acquisition_error_is_returned(monkeypatch):
    tenant = report_tenant()

    @contextmanager
    def failed_lock(*args, **kwargs):
        raise RuntimeError("lock backend down")
        yield

    monkeypatch.setattr(dispatch.db, "tenant_sync_lock", failed_lock)

    result = dispatch.run_sync_tenant(tenant)

    assert result["busy"] is False
    assert "lock backend down" in result["error"]


def test_reset_and_sync_share_tenant_lock(monkeypatch):
    tenant = report_tenant()
    events = []

    @contextmanager
    def tracked_lock(*args, **kwargs):
        events.append("lock-enter")
        yield True
        events.append("lock-exit")

    monkeypatch.setattr(dispatch.db, "tenant_sync_lock", tracked_lock)
    monkeypatch.setattr(
        dispatch, "reset_cursors", lambda *args: events.append("reset")
    )
    monkeypatch.setattr(
        dispatch,
        "_sync_one_report",
        lambda *args, **kwargs: events.append("sync") or {"stored": 0},
    )

    dispatch.run_sync_tenant(tenant, reset_cursor=True)

    assert events == ["lock-enter", "reset", "sync", "lock-exit"]
