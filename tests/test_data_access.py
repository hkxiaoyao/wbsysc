import pytest

from app.auth import TenantCtx
from app import data_access
from app.wecom import approval_sync, checkin_sync, sync as report_sync


def ctx(mode):
    return TenantCtx(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="secret",
        schema_name="wbd_123",
        contact_secret="contact",
        checkin_userids=["user-a"],
        enabled_modules={"report", "approval", "checkin"},
        data_mode=mode,
    )


def test_stored_reports_use_database(monkeypatch):
    monkeypatch.setattr(
        data_access.db,
        "query_reports_by_window",
        lambda schema, start, end, limit: [{
            "journaluuid": "r1", "template_id": "t1", "template_name": "日报",
            "report_time": 100, "submitter_userid": "u1", "is_partial": 0,
        }],
    )
    result = data_access.list_reports(ctx("stored"), 1, 200, 20)
    assert result["source"] == "db"
    assert result["records"][0]["journaluuid"] == "r1"


def test_direct_reports_keep_failed_detail_as_partial(monkeypatch):
    monkeypatch.setattr(data_access, "sync_reports_window", lambda *args, **kwargs: ["r1", "r2"])
    monkeypatch.setattr(
        data_access,
        "fetch_report_detail",
        lambda corpid, secret, value: {"journaluuid": "r1", "report_time": 100}
        if value == "r1" else {
            "errcode": 40001,
            "errmsg": "access_token=token-value secret=corp-secret",
        },
    )
    result = data_access.list_reports(ctx("direct"), 1, 200, 20)
    assert result["source"] == "wecom"
    assert result["partial_count"] == 1
    assert result["records"][1]["_partial"] is True
    assert result["records"][1]["errmsg"] == "企微汇报详情请求失败"
    assert "token-value" not in repr(result)
    assert "corp-secret" not in repr(result)


def test_direct_limit_is_bounded_to_100(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        data_access,
        "sync_reports_window",
        lambda *args, **kwargs: captured.setdefault("limit", kwargs["max_records"]) and [],
    )
    data_access.list_reports(ctx("direct"), 1, 200, 1000)
    assert captured["limit"] == 100


def test_direct_approval_detail_failure_is_partial(monkeypatch):
    monkeypatch.setattr(data_access, "sync_approvals_window", lambda *args, **kwargs: ["sp1"])
    monkeypatch.setattr(
        data_access,
        "fetch_approval_detail",
        lambda *args: {
            "errcode": 301055,
            "errmsg": "access_token=token-value secret=corp-secret",
        },
    )
    result = data_access.list_approvals(ctx("direct"), 1, 200, 20)
    assert result["partial_count"] == 1
    assert result["records"][0]["sp_no"] == "sp1"
    assert result["records"][0]["errmsg"] == "企微审批详情请求失败"
    assert "token-value" not in repr(result)
    assert "corp-secret" not in repr(result)


def test_direct_checkins_require_userids(monkeypatch):
    context = ctx("direct")
    context.contact_secret = ""
    context.checkin_userids = []
    with pytest.raises(data_access.PublicDataAccessError) as caught:
        data_access.list_checkins(context, 1, 200, 20)
    assert caught.value.errcode == 400
    assert caught.value.source == "wecom"
    assert "通讯录 Secret" in caught.value.public_message
    assert "userid" in caught.value.public_message


def test_direct_reports_do_not_query_database(monkeypatch):
    monkeypatch.setattr(
        data_access.db,
        "query_reports_by_window",
        lambda *args: (_ for _ in ()).throw(AssertionError("database read is forbidden")),
    )
    monkeypatch.setattr(data_access, "sync_reports_window", lambda *args, **kwargs: [])
    result = data_access.list_reports(ctx("direct"), 1, 200, 20)
    assert result["source"] == "wecom"


def test_capped_report_sync_reads_latest_segment_first(monkeypatch):
    windows = []
    records = [
        ("old", report_sync.MONTH - 1),
        ("new", report_sync.MONTH * 2 - 1),
    ]

    def list_records(corpid, secret, start, end, cursor, limit, filters):
        windows.append((start, end))
        identifiers = [
            identifier for identifier, timestamp in records
            if start <= timestamp < end
        ]
        return {
            "errcode": 0,
            "errmsg": "ok",
            "journaluuid_list": identifiers,
            "endflag": 1,
            "next_cursor": 0,
        }

    monkeypatch.setattr(report_sync.api, "list_report_records", list_records)

    result = report_sync.sync_reports_window(
        "ww123", "secret", 0, report_sync.MONTH * 2, max_records=1
    )

    assert result == ["new"]
    assert windows == [(report_sync.MONTH, report_sync.MONTH * 2)]


def test_unbounded_report_sync_keeps_full_old_to_new_window(monkeypatch):
    windows = []
    records = [
        ("old", report_sync.MONTH - 1),
        ("new", report_sync.MONTH * 2 - 1),
    ]

    def list_records(corpid, secret, start, end, cursor, limit, filters):
        windows.append((start, end))
        identifiers = [
            identifier for identifier, timestamp in records
            if start <= timestamp < end
        ]
        return {
            "errcode": 0,
            "errmsg": "ok",
            "journaluuid_list": identifiers,
            "endflag": 1,
            "next_cursor": 0,
        }

    monkeypatch.setattr(report_sync.api, "list_report_records", list_records)

    result = report_sync.sync_reports_window(
        "ww123", "secret", 0, report_sync.MONTH * 2
    )

    assert result == ["old", "new"]
    assert windows == [
        (0, report_sync.MONTH),
        (report_sync.MONTH, report_sync.MONTH * 2),
    ]


def test_capped_approval_sync_reads_latest_segment_first(monkeypatch):
    windows = []
    records = [
        ("old", approval_sync.SPAN - 1),
        ("new", approval_sync.SPAN * 2 - 1),
    ]

    def list_approvals(corpid, secret, start, end, cursor, size, filters):
        windows.append((start, end))
        identifiers = [
            identifier for identifier, timestamp in records
            if start <= timestamp < end
        ]
        return {
            "errcode": 0,
            "errmsg": "ok",
            "sp_no_list": identifiers,
            "new_next_cursor": "",
        }

    monkeypatch.setattr(approval_sync.api, "list_approvals", list_approvals)

    result = approval_sync.sync_approvals_window(
        "ww123", "secret", 0, approval_sync.SPAN * 2, max_records=1
    )

    assert result == ["new"]
    assert windows == [(approval_sync.SPAN, approval_sync.SPAN * 2)]


def test_unbounded_approval_sync_keeps_full_old_to_new_window(monkeypatch):
    windows = []
    records = [
        ("old", approval_sync.SPAN - 1),
        ("new", approval_sync.SPAN * 2 - 1),
    ]

    def list_approvals(corpid, secret, start, end, cursor, size, filters):
        windows.append((start, end))
        identifiers = [
            identifier for identifier, timestamp in records
            if start <= timestamp < end
        ]
        return {
            "errcode": 0,
            "errmsg": "ok",
            "sp_no_list": identifiers,
            "new_next_cursor": "",
        }

    monkeypatch.setattr(approval_sync.api, "list_approvals", list_approvals)

    result = approval_sync.sync_approvals_window(
        "ww123", "secret", 0, approval_sync.SPAN * 2
    )

    assert result == ["old", "new"]
    assert windows == [
        (0, approval_sync.SPAN),
        (approval_sync.SPAN, approval_sync.SPAN * 2),
    ]


def test_capped_report_min_window_truncates_in_api_order(monkeypatch):
    def list_records(corpid, secret, start, end, cursor, limit, filters):
        if cursor == 0:
            return {
                "errcode": 0,
                "errmsg": "ok",
                "journaluuid_list": ["first-page"],
                "endflag": 0,
                "next_cursor": 1,
            }
        return {
            "errcode": 0,
            "errmsg": "ok",
            "journaluuid_list": ["second-page"],
            "endflag": 1,
            "next_cursor": 0,
        }

    monkeypatch.setattr(report_sync.api, "list_report_records", list_records)

    result = report_sync.sync_reports_window(
        "ww123", "secret", 100, 150, max_records=1
    )

    assert result == ["first-page"]


def test_capped_approval_min_window_truncates_in_api_order(monkeypatch):
    def list_approvals(corpid, secret, start, end, cursor, size, filters):
        if cursor == "":
            return {
                "errcode": 0,
                "errmsg": "ok",
                "sp_no_list": ["first-page"],
                "new_next_cursor": "1",
            }
        return {
            "errcode": 0,
            "errmsg": "ok",
            "sp_no_list": ["second-page"],
            "new_next_cursor": "",
        }

    monkeypatch.setattr(approval_sync.api, "list_approvals", list_approvals)

    result = approval_sync.sync_approvals_window(
        "ww123", "secret", 100, 150, max_records=1
    )

    assert result == ["first-page"]


def test_capped_report_rejects_repeated_pagination_cursor(monkeypatch):
    calls = 0

    def list_records(corpid, secret, start, end, cursor, limit, filters):
        nonlocal calls
        calls += 1
        if calls > 2:
            raise AssertionError("pagination loop was not stopped")
        return {
            "errcode": 0,
            "errmsg": "ok",
            "journaluuid_list": [f"record-{calls}"],
            "endflag": 0,
            "next_cursor": 7,
        }

    monkeypatch.setattr(report_sync.api, "list_report_records", list_records)

    with pytest.raises(RuntimeError, match="游标重复"):
        report_sync.sync_reports_window(
            "ww123", "secret", 100, 150, max_records=1
        )


def test_capped_approval_rejects_repeated_pagination_cursor(monkeypatch):
    calls = 0

    def list_approvals(corpid, secret, start, end, cursor, size, filters):
        nonlocal calls
        calls += 1
        if calls > 2:
            raise AssertionError("pagination loop was not stopped")
        return {
            "errcode": 0,
            "errmsg": "ok",
            "sp_no_list": [f"record-{calls}"],
            "new_next_cursor": "repeated",
        }

    monkeypatch.setattr(approval_sync.api, "list_approvals", list_approvals)

    with pytest.raises(RuntimeError, match="游标重复"):
        approval_sync.sync_approvals_window(
            "ww123", "secret", 100, 150, max_records=1
        )


def test_direct_reports_sort_newest_and_apply_limit(monkeypatch):
    monkeypatch.setattr(
        data_access, "sync_reports_window", lambda *args, **kwargs: ["old", "new"]
    )
    monkeypatch.setattr(
        data_access,
        "fetch_report_detail",
        lambda corpid, secret, identifier: {
            "journaluuid": identifier,
            "report_time": 200 if identifier == "new" else 100,
        },
    )

    result = data_access.list_reports(ctx("direct"), 1, 300, 1)

    assert result["count"] == 1
    assert [record["journaluuid"] for record in result["records"]] == ["new"]


def test_direct_approvals_sort_newest_and_apply_limit(monkeypatch):
    monkeypatch.setattr(
        data_access, "sync_approvals_window", lambda *args, **kwargs: ["old", "new"]
    )
    monkeypatch.setattr(
        data_access,
        "fetch_approval_detail",
        lambda corpid, secret, identifier: {
            "sp_no": identifier,
            "apply_time": 200 if identifier == "new" else 100,
        },
    )

    result = data_access.list_approvals(ctx("direct"), 1, 300, 1)

    assert result["count"] == 1
    assert [record["sp_no"] for record in result["records"]] == ["new"]


def test_direct_reports_choose_newest_detail_from_unsorted_pages(monkeypatch):
    records = [
        ("older-on-first-page", 100),
        ("newer-on-second-page", 200),
    ]
    detail_calls = []

    def list_records(corpid, secret, start, end, cursor, limit, filters):
        eligible = [
            identifier for identifier, timestamp in records
            if start <= timestamp < end
        ]
        offset = int(cursor or 0)
        page = eligible[offset:offset + 1]
        next_cursor = offset + 1 if offset + 1 < len(eligible) else 0
        return {
            "errcode": 0,
            "errmsg": "ok",
            "journaluuid_list": page,
            "endflag": 0 if next_cursor else 1,
            "next_cursor": next_cursor,
        }

    def get_detail(corpid, secret, identifier):
        detail_calls.append(identifier)
        report_time = 200 if identifier == "newer-on-second-page" else 100
        return {
            "errcode": 0,
            "errmsg": "ok",
            "info": {"journaluuid": identifier, "report_time": report_time},
        }

    monkeypatch.setattr(report_sync.api, "list_report_records", list_records)
    monkeypatch.setattr(report_sync.api, "get_report_detail", get_detail)

    result = data_access.list_reports(
        ctx("direct"), 1, 300, 1
    )

    assert result["count"] == 1
    assert result["records"][0]["journaluuid"] == "newer-on-second-page"
    assert detail_calls == ["newer-on-second-page"]


def test_direct_approvals_choose_newest_detail_from_unsorted_pages(monkeypatch):
    records = [
        ("older-on-first-page", 100),
        ("newer-on-second-page", 200),
    ]
    detail_calls = []

    def list_approvals(corpid, secret, start, end, cursor, size, filters):
        eligible = [
            identifier for identifier, timestamp in records
            if start <= timestamp < end
        ]
        offset = int(cursor or 0)
        page = eligible[offset:offset + 1]
        next_cursor = str(offset + 1) if offset + 1 < len(eligible) else ""
        return {
            "errcode": 0,
            "errmsg": "ok",
            "sp_no_list": page,
            "new_next_cursor": next_cursor,
        }

    def get_detail(corpid, secret, identifier):
        detail_calls.append(identifier)
        apply_time = 200 if identifier == "newer-on-second-page" else 100
        return {
            "errcode": 0,
            "errmsg": "ok",
            "info": {"sp_no": identifier, "apply_time": apply_time},
        }

    monkeypatch.setattr(approval_sync.api, "list_approvals", list_approvals)
    monkeypatch.setattr(approval_sync.api, "get_approval_detail", get_detail)

    result = data_access.list_approvals(
        ctx("direct"), 1, 300, 1
    )

    assert result["count"] == 1
    assert result["records"][0]["sp_no"] == "newer-on-second-page"
    assert detail_calls == ["newer-on-second-page"]


def test_direct_checkins_raise_when_every_api_attempt_fails(monkeypatch):
    context = ctx("direct")
    context.contact_secret = ""
    context.checkin_userids = ["bad-a", "bad-b"]
    monkeypatch.setattr(
        checkin_sync.api,
        "get_checkin_data",
        lambda *args: {
            "errcode": 301021,
            "errmsg": "userid is outside the visible range",
            "checkindata": [],
        },
    )

    with pytest.raises(data_access.PublicDataAccessError) as caught:
        data_access.list_checkins(context, 1, 200, 20)
    assert caught.value.errcode == 301021
    assert caught.value.source == "wecom"
    assert caught.value.public_message == "企微打卡请求失败"


def test_direct_checkins_handle_null_error_message(monkeypatch):
    context = ctx("direct")
    context.contact_secret = ""
    context.checkin_userids = ["bad"]
    monkeypatch.setattr(
        checkin_sync.api,
        "get_checkin_data",
        lambda *args: {"errcode": 301021, "errmsg": None, "checkindata": []},
    )

    with pytest.raises(data_access.PublicDataAccessError) as caught:
        data_access.list_checkins(context, 1, 200, 20)
    assert caught.value.errcode == 301021
    assert caught.value.public_message == "企微打卡请求失败"


def test_direct_checkins_report_partial_api_failures(monkeypatch):
    context = ctx("direct")
    context.contact_secret = ""
    context.checkin_userids = ["bad", "good"]

    def get_checkin_data(corpid, secret, start, end, userids, data_type):
        if userids == ["bad"]:
            return {
                "errcode": 301021,
                "errmsg": "userid is outside the visible range",
                "checkindata": [],
            }
        return {
            "errcode": 0,
            "errmsg": "ok",
            "checkindata": [{
                "userid": "good",
                "checkin_time": 100,
                "checkin_type": "上班打卡",
            }],
        }

    monkeypatch.setattr(checkin_sync.api, "get_checkin_data", get_checkin_data)

    result = data_access.list_checkins(context, 1, 200, 20)

    assert result["count"] == 1
    assert result["partial_count"] == 1
    assert result["errors"][0]["userid"] == "bad"


@pytest.mark.parametrize(
    ("accessor_name", "dependency_name", "expected_message"),
    [
        ("list_reports", "sync_reports_window", "企微汇报请求失败"),
        ("list_approvals", "sync_approvals_window", "企微审批请求失败"),
        ("list_checkins", "fetch_all_userids", "企微打卡请求失败"),
    ],
)
def test_known_direct_source_failures_raise_safe_public_errors(
    monkeypatch, accessor_name, dependency_name, expected_message
):
    sensitive = (
        "secret=corp-secret access_token=token-value "
        "mysql+pymysql://root:db-password@db/gateway [40014]"
    )
    monkeypatch.setattr(
        data_access,
        dependency_name,
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(sensitive)),
    )

    with pytest.raises(data_access.PublicDataAccessError) as caught:
        getattr(data_access, accessor_name)(ctx("direct"), 1, 200, 20)

    error = caught.value
    assert error.errcode == 40014
    assert error.source == "wecom"
    assert error.public_message == expected_message
    assert error.__context__ is None
    for secret in ("corp-secret", "token-value", "db-password"):
        assert secret not in str(error)
        assert secret not in repr(error.__dict__)


@pytest.mark.parametrize(
    ("accessor_name", "dependency_name", "identifier", "expected_message"),
    [
        ("get_report", "fetch_report_detail", "r1", "企微汇报请求失败"),
        ("get_approval", "fetch_approval_detail", "sp1", "企微审批请求失败"),
    ],
)
def test_direct_detail_errors_become_safe_public_errors(
    monkeypatch, accessor_name, dependency_name, identifier, expected_message
):
    monkeypatch.setattr(
        data_access,
        dependency_name,
        lambda *args: {
            "errcode": 40014,
            "errmsg": "access_token=token-value secret=corp-secret",
        },
    )

    with pytest.raises(data_access.PublicDataAccessError) as caught:
        getattr(data_access, accessor_name)(ctx("direct"), identifier)

    assert caught.value.errcode == 40014
    assert caught.value.source == "wecom"
    assert caught.value.public_message == expected_message
    assert "token-value" not in str(caught.value)
    assert "corp-secret" not in str(caught.value)


def test_partial_checkin_errors_do_not_expose_exception_text(monkeypatch):
    context = ctx("direct")
    context.contact_secret = ""
    context.checkin_userids = ["bad", "good"]

    def get_checkin_data(corpid, secret, start, end, userids, data_type):
        if userids == ["bad"]:
            raise RuntimeError(
                "secret=corp-secret access_token=token-value "
                "mysql://root:db-password@db/gateway"
            )
        return {
            "errcode": 0,
            "errmsg": "ok",
            "checkindata": [{"userid": "good", "checkin_time": 100}],
        }

    monkeypatch.setattr(checkin_sync.api, "get_checkin_data", get_checkin_data)

    result = data_access.list_checkins(context, 1, 200, 20)

    assert result["partial_count"] == 1
    assert result["errors"] == [{
        "userid": "bad",
        "errcode": None,
        "errmsg": "企微打卡请求失败",
    }]
    serialized = repr(result)
    for secret in ("corp-secret", "token-value", "db-password"):
        assert secret not in serialized


def test_direct_reports_capped_detail_failure_stays_partial(monkeypatch):
    records = [
        ("older-candidate", report_sync.MONTH + 100),
        ("failed-latest", report_sync.MONTH * 2 - 100),
    ]
    detail_calls = []

    def list_records(corpid, secret, start, end, cursor, limit, filters):
        identifiers = [
            identifier for identifier, timestamp in records
            if start <= timestamp < end
        ]
        return {
            "errcode": 0,
            "errmsg": "ok",
            "journaluuid_list": identifiers,
            "endflag": 1,
            "next_cursor": 0,
        }

    def get_detail(corpid, secret, identifier):
        detail_calls.append(identifier)
        if identifier == "failed-latest":
            return {"errcode": 40001, "errmsg": "invalid credential"}
        return {
            "errcode": 0,
            "errmsg": "ok",
            "info": {"journaluuid": identifier, "report_time": report_sync.MONTH + 100},
        }

    monkeypatch.setattr(report_sync.api, "list_report_records", list_records)
    monkeypatch.setattr(report_sync.api, "get_report_detail", get_detail)

    result = data_access.list_reports(
        ctx("direct"), 0, report_sync.MONTH * 2, 1
    )

    assert detail_calls == ["failed-latest"]
    assert result["count"] == 1
    assert result["partial_count"] == 1
    assert result["records"][0]["journaluuid"] == "failed-latest"
    assert result["records"][0]["_partial"] is True


def test_direct_approvals_capped_detail_failure_stays_partial(monkeypatch):
    records = [
        ("older-candidate", approval_sync.SPAN + 100),
        ("failed-latest", approval_sync.SPAN * 2 - 100),
    ]
    detail_calls = []

    def list_approvals(corpid, secret, start, end, cursor, size, filters):
        identifiers = [
            identifier for identifier, timestamp in records
            if start <= timestamp < end
        ]
        return {
            "errcode": 0,
            "errmsg": "ok",
            "sp_no_list": identifiers,
            "new_next_cursor": "",
        }

    def get_detail(corpid, secret, identifier):
        detail_calls.append(identifier)
        if identifier == "failed-latest":
            return {"errcode": 301055, "errmsg": "not authorized"}
        return {
            "errcode": 0,
            "errmsg": "ok",
            "info": {"sp_no": identifier, "apply_time": approval_sync.SPAN + 100},
        }

    monkeypatch.setattr(approval_sync.api, "list_approvals", list_approvals)
    monkeypatch.setattr(approval_sync.api, "get_approval_detail", get_detail)

    result = data_access.list_approvals(
        ctx("direct"), 0, approval_sync.SPAN * 2, 1
    )

    assert detail_calls == ["failed-latest"]
    assert result["count"] == 1
    assert result["partial_count"] == 1
    assert result["records"][0]["sp_no"] == "failed-latest"
    assert result["records"][0]["_partial"] is True


def test_background_checkin_fetch_remains_tolerant(monkeypatch):
    good_record = {
        "userid": "good",
        "checkin_time": 100,
        "checkin_type": "上班打卡",
    }

    def get_checkin_data(corpid, secret, start, end, userids, data_type):
        if userids == ["bad"]:
            return {
                "errcode": 301021,
                "errmsg": "userid is outside the visible range",
                "checkindata": [],
            }
        return {"errcode": 0, "errmsg": "ok", "checkindata": [good_record]}

    monkeypatch.setattr(checkin_sync.api, "get_checkin_data", get_checkin_data)

    result = checkin_sync.fetch_checkin_records(
        "ww123", "secret", 1, 200, ["bad", "good"]
    )

    assert result == [good_record]
