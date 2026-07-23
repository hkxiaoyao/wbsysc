from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from app import db
from app.mcp_services import crypto, store


class Result:
    def __init__(self, row=None, *, rows=(), rowcount=0):
        self.row = row
        self.rows = list(rows)
        self.rowcount = rowcount

    def fetchone(self):
        return self.row

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def scalar(self):
        if isinstance(self.row, dict):
            return next(iter(self.row.values()), None)
        return self.row


class TokenConnection:
    def __init__(self):
        self.statements = []
        self.bound_parameters = []
        self.transaction_ids = []
        self.transactions_started = 0
        self.active_transaction = None
        self.transactions_committed = 0
        self.db_now = datetime(2026, 7, 22, 12, 0, 0)
        self.fail_usage_update = False
        self.fail_commit = False
        self.delete_tenant_after_candidate = False
        self.disable_service_after_tenant_lock = False
        self.revoke_token_after_service_lock = False
        self.services = {
            "service-a": {
                "service_id": "service-a",
                "tenant_id": "tenant-a",
                "display_name": "Operations",
                "service_key": "operations",
                "status": "active",
                "config_version": 1,
            }
        }
        self.tenants = {"tenant-a": True}
        self.tokens = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = dict(params or {})
        self.statements.append(sql)
        self.bound_parameters.append(params)
        self.transaction_ids.append(self.active_transaction)
        if sql.startswith("SELECT s.tenant_id FROM mcp_service_token"):
            token = next(
                (
                    token
                    for token in self.tokens.values()
                    if token["service_id"] == params["service_id"]
                    and token["token_hmac"] == params["token_hmac"]
                    and token["revoked_at"] is None
                    and (
                        token["expires_at"] is None
                        or token["expires_at"] > self.db_now
                    )
                ),
                None,
            )
            service = self.services.get(params["service_id"]) if token else None
            tenant_id = service["tenant_id"] if service else None
            if (
                service is None
                or service["status"] != "active"
                or not self.tenants.get(tenant_id)
            ):
                tenant_id = None
            row = {"tenant_id": tenant_id} if tenant_id else None
            if row is not None and self.delete_tenant_after_candidate:
                self.tenants.pop(tenant_id, None)
            return Result(row)
        if sql.startswith("SELECT enabled FROM tenant_config"):
            enabled = 1 if self.tenants.get(params["tenant_id"]) else None
            if enabled == 1 and self.disable_service_after_tenant_lock:
                self.services["service-a"]["status"] = "disabled"
            return Result(enabled)
        if sql.startswith("SELECT token_id FROM mcp_service_token"):
            token = next(
                (
                    token
                    for token in self.tokens.values()
                    if token["service_id"] == params["service_id"]
                    and token["token_hmac"] == params["token_hmac"]
                    and token["revoked_at"] is None
                    and (
                        token["expires_at"] is None
                        or token["expires_at"] > self.db_now
                    )
                ),
                None,
            )
            return Result({"token_id": token["token_id"]} if token else None)
        if sql.startswith("SELECT service_id, tenant_id"):
            service = self.services.get(params.get("service_id"))
            if service is not None and "tenant_id=:tenant_id" in sql:
                if service["tenant_id"] != params["tenant_id"]:
                    service = None
            if service is not None and "AND status='active'" in sql:
                if service["status"] != "active":
                    service = None
            row = dict(service) if service else None
            if (
                row is not None
                and "AND status='active'" in sql
                and self.revoke_token_after_service_lock
            ):
                token = next(iter(self.tokens.values()), None)
                if token is not None:
                    token["revoked_at"] = "now"
            if row is not None and "UTC_TIMESTAMP() AS db_now" in sql:
                row["db_now"] = self.db_now
            return Result(row)
        if sql.startswith("INSERT INTO mcp_service_token"):
            self.tokens[params["token_id"]] = dict(params)
            self.tokens[params["token_id"]]["revoked_at"] = None
            self.tokens[params["token_id"]]["last_used_at"] = None
            return Result(rowcount=1)
        if sql.startswith("UPDATE mcp_service_token SET last_used_at=UTC_TIMESTAMP()"):
            if self.fail_usage_update:
                raise RuntimeError("usage update failed")
            token = self.tokens.get(params["token_id"])
            if token is not None:
                token["last_used_at"] = self.db_now
            return Result(rowcount=0)
        if sql.startswith("SELECT t.encrypted_token"):
            token = self.tokens.get(params["token_id"])
            service = self.services.get(params["service_id"])
            if (
                token is None
                or service is None
                or service["tenant_id"] != params["tenant_id"]
                or token["service_id"] != params["service_id"]
                or token["revoked_at"] is not None
                or (
                    token["expires_at"] is not None
                    and token["expires_at"] <= self.db_now
                )
                or token["encrypted_token"] is None
            ):
                return Result()
            return Result({"encrypted_token": token["encrypted_token"]})
        if sql.startswith("SELECT t.token_id"):
            service = self.services.get(params["service_id"])
            if service is None or service["tenant_id"] != params["tenant_id"]:
                return Result(rows=[])
            return Result(
                rows=[
                    {
                        "token_id": token["token_id"],
                        "prefix": token["token_prefix"],
                        "label": token["token_label"],
                        "expires_at": token["expires_at"],
                        "revoked_at": token["revoked_at"],
                        "last_used_at": token["last_used_at"],
                        "created_at": datetime(2026, 7, 17),
                    }
                    for token in self.tokens.values()
                    if token["service_id"] == params["service_id"]
                ]
            )
        if sql.startswith("UPDATE mcp_service_token SET revoked_at=UTC_TIMESTAMP()"):
            token = self.tokens.get(params["token_id"])
            service = self.services.get(params["service_id"])
            if (
                token is None
                or service is None
                or service["tenant_id"] != params["tenant_id"]
                or token["service_id"] != params["service_id"]
            ):
                return Result(rowcount=0)
            token["revoked_at"] = "now"
            token["encrypted_token"] = None
            return Result(rowcount=1)
        return Result()


class FakeEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        connection = self.connection
        connection.transactions_started += 1
        transaction_id = connection.transactions_started

        class Transaction:
            def __enter__(self):
                connection.active_transaction = transaction_id
                return connection

            def __exit__(self, *_args):
                connection.active_transaction = None
                if connection.fail_commit:
                    raise RuntimeError("commit failed")
                connection.transactions_committed += 1
                return False

        return Transaction()

    def connect(self):
        return self.connection


def _parameters_contain_secret(parameters, secret):
    return secret in repr(parameters)


@pytest.fixture
def fake_db(monkeypatch):
    connection = TokenConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))
    monkeypatch.setattr(
        crypto,
        "get_settings",
        lambda: SimpleNamespace(
            mcp_token_hmac_key="h" * 32,
            mcp_token_plaintext_key="p" * 32,
        ),
    )
    crypto._fernet.cache_clear()
    yield connection
    crypto._fernet.cache_clear()


def test_service_token_uses_hmac_for_auth_and_ciphertext_for_reveal(fake_db):
    issued = store.issue_token("service-a", "tenant-a", label="client-a")

    assert store.resolve_token(issued.raw_value, "service-a").service_id == "service-a"
    assert store.resolve_token(issued.raw_value, "service-b") is None
    revealed_digest = crypto.token_hmac(
        store.reveal_token("service-a", "tenant-a", issued.token_id)
    )
    assert revealed_digest == crypto.token_hmac(issued.raw_value)
    assert not _parameters_contain_secret(fake_db.bound_parameters, issued.raw_value)
    assert "raw_value" not in repr(issued)
    auth_query = next(sql for sql in fake_db.statements if "t.token_hmac=:token_hmac" in sql)
    assert "encrypted_token" not in auth_query


def test_service_token_requires_live_enabled_tenant_even_if_history_reactivated(fake_db):
    issued = store.issue_token("service-a", "tenant-a")
    fake_db.tenants["tenant-a"] = False

    assert store.resolve_token(issued.raw_value, "service-a") is None

    fake_db.services["service-a"]["status"] = "active"
    fake_db.tokens[issued.token_id]["revoked_at"] = None
    fake_db.tenants.pop("tenant-a")
    assert store.resolve_token(issued.raw_value, "service-a") is None
    resolver_sql = next(
        sql for sql in fake_db.statements
        if "JOIN tenant_config tenant_row" in sql
    )
    assert "tenant_row.enabled=1" in resolver_sql


def test_issue_token_locks_active_service_before_insert(fake_db):
    store.issue_token("service-a", "tenant-a", label="client-a")

    service_read_index = next(
        index
        for index, sql in enumerate(fake_db.statements)
        if sql.startswith("SELECT service_id, tenant_id")
    )
    insert_index = next(
        index
        for index, sql in enumerate(fake_db.statements)
        if sql.startswith("INSERT INTO mcp_service_token")
    )
    service_read = fake_db.statements[service_read_index]
    params = fake_db.bound_parameters[service_read_index]

    assert "FOR UPDATE" in service_read
    assert "service_id=:service_id AND tenant_id=:tenant_id" in service_read
    assert params == {"service_id": "service-a", "tenant_id": "tenant-a"}
    assert service_read_index < insert_index
    assert fake_db.transactions_started == 1
    assert fake_db.transaction_ids[service_read_index] is not None
    assert (
        fake_db.transaction_ids[service_read_index]
        == fake_db.transaction_ids[insert_index]
    )


def test_service_token_paths_enforce_tenant_ownership(fake_db):
    issued = store.issue_token("service-a", "tenant-a", label="client-a")
    fake_db.services["service-b"] = {
        **fake_db.services["service-a"],
        "service_id": "service-b",
        "service_key": "operations-b",
    }
    service_b_token = store.issue_token("service-b", "tenant-a", label="client-b")

    with pytest.raises(store.TokenUnavailableError):
        store.reveal_token("service-a", "tenant-b", issued.token_id)
    with pytest.raises(store.TokenUnavailableError):
        store.reveal_token("service-a", "tenant-a", service_b_token.token_id)
    assert store.revoke_token("service-a", "tenant-b", issued.token_id) is False
    assert store.resolve_token(issued.raw_value, "service-a") is not None


def test_revoked_token_cannot_authenticate_or_reveal_and_discards_ciphertext(fake_db):
    issued = store.issue_token("service-a", "tenant-a", label="client-a")

    assert store.revoke_token("service-a", "tenant-a", issued.token_id) is True

    assert store.resolve_token(issued.raw_value, "service-a") is None
    with pytest.raises(store.TokenUnavailableError):
        store.reveal_token("service-a", "tenant-a", issued.token_id)
    assert fake_db.tokens[issued.token_id]["encrypted_token"] is None
    revoke_sql = next(
        sql
        for sql in fake_db.statements
        if sql.startswith("UPDATE mcp_service_token SET revoked_at=UTC_TIMESTAMP()")
    )
    assert "encrypted_token=NULL" in revoke_sql


def test_issue_token_rejects_unknown_or_cross_tenant_service(fake_db):
    with pytest.raises(store.ServiceOwnershipError):
        store.issue_token("service-a", "tenant-b", label="client-a")
    with pytest.raises(store.ServiceOwnershipError):
        store.issue_token("missing-service", "tenant-a", label="client-a")


@pytest.mark.parametrize("status", ["draft", "disabled"])
def test_issue_token_requires_active_service(fake_db, status):
    fake_db.services["service-a"]["status"] = status

    with pytest.raises(ValueError, match="active"):
        store.issue_token("service-a", "tenant-a", label="client-a")

    assert fake_db.tokens == {}


def test_list_tokens_returns_safe_metadata_for_disabled_owned_service(fake_db):
    issued = store.issue_token("service-a", "tenant-a", label="client-a")
    fake_db.services["service-a"]["status"] = "disabled"

    listed = store.list_tokens("service-a", "tenant-a")

    assert listed[0].token_id == issued.token_id
    assert listed[0].prefix == issued.prefix
    assert listed[0].label == "client-a"
    assert issued.raw_value not in repr(listed)
    query = next(sql for sql in fake_db.statements if sql.startswith("SELECT t.token_id"))
    assert "encrypted_token" not in query
    assert "token_hmac" not in query
    assert "s.tenant_id=:tenant_id" in query


def test_list_tokens_rejects_foreign_service_without_leaking_metadata(fake_db):
    issued = store.issue_token("service-a", "tenant-a", label="client-a")

    with pytest.raises(store.ServiceOwnershipError):
        store.list_tokens("service-a", "tenant-b")

    assert issued.raw_value not in repr(fake_db.statements)


def test_token_runtime_table_contains_ciphertext_and_authentication_index(fake_db):
    store.ensure_mcp_service_tables()

    ddl = next(sql for sql in fake_db.statements if "CREATE TABLE IF NOT EXISTS mcp_service_token" in sql)
    assert "encrypted_token VARBINARY(4096) NULL" in ddl
    assert "UNIQUE KEY uk_mcp_service_token_hmac (token_hmac)" in ddl


def test_issue_token_persists_normalized_future_expiry(fake_db):
    issued = store.issue_token(
        "service-a",
        "tenant-a",
        expires_at=datetime(2026, 7, 22, 20, 0, 1, tzinfo=timezone.utc),
    )

    assert fake_db.tokens[issued.token_id]["expires_at"] == datetime(
        2026, 7, 22, 20, 0, 1
    )
    insert_index = next(
        i
        for i, sql in enumerate(fake_db.statements)
        if sql.startswith("INSERT INTO mcp_service_token")
    )
    assert "expires_at" in fake_db.statements[insert_index]


def test_issue_token_omitted_expiry_persists_null(fake_db):
    issued = store.issue_token("service-a", "tenant-a")

    assert fake_db.tokens[issued.token_id]["expires_at"] is None


@pytest.mark.parametrize(
    "expires_at",
    [
        datetime(2026, 7, 22, 12, 0, 0),
        datetime(2026, 7, 22, 11, 59, 59),
    ],
)
def test_issue_token_rejects_expiry_at_or_before_database_now(fake_db, expires_at):
    with pytest.raises(ValueError, match="future"):
        store.issue_token("service-a", "tenant-a", expires_at=expires_at)

    assert fake_db.tokens == {}


@pytest.mark.parametrize(
    "expires_at",
    ["2026-07-23T00:00:00Z", True, datetime(2026, 7, 23, microsecond=1)],
)
def test_issue_token_defensively_rejects_invalid_direct_expiry(fake_db, expires_at):
    with pytest.raises((TypeError, ValueError)):
        store.issue_token("service-a", "tenant-a", expires_at=expires_at)

    assert fake_db.tokens == {}


def test_resolve_token_locks_updates_selected_token_and_commits(fake_db):
    issued = store.issue_token("service-a", "tenant-a")
    fake_db.statements.clear()
    fake_db.bound_parameters.clear()
    fake_db.transaction_ids.clear()
    fake_db.transactions_started = 0
    fake_db.transactions_committed = 0

    resolved = store.resolve_token(issued.raw_value, "service-a")

    assert resolved.service_id == "service-a"
    candidate_index = next(
        i
        for i, sql in enumerate(fake_db.statements)
        if sql.startswith("SELECT s.tenant_id FROM mcp_service_token")
    )
    tenant_index = next(
        i
        for i, sql in enumerate(fake_db.statements)
        if sql.startswith("SELECT enabled FROM tenant_config")
    )
    service_index = next(
        i
        for i, sql in enumerate(fake_db.statements)
        if sql.startswith("SELECT service_id, tenant_id")
    )
    token_index = next(
        i
        for i, sql in enumerate(fake_db.statements)
        if sql.startswith("SELECT token_id FROM mcp_service_token")
    )
    update_index = next(
        i
        for i, sql in enumerate(fake_db.statements)
        if sql.startswith("UPDATE mcp_service_token SET last_used_at")
    )
    assert candidate_index < tenant_index < service_index < token_index < update_index
    assert "FOR UPDATE" not in fake_db.statements[candidate_index]
    assert "encrypted_token" not in fake_db.statements[candidate_index]
    assert "token_hmac" not in fake_db.statements[candidate_index].split("SELECT", 1)[1].split(
        "FROM", 1
    )[0]
    assert "tenant_id=:tenant_id" in fake_db.statements[service_index]
    assert "status='active'" in fake_db.statements[service_index]
    assert "FOR UPDATE" in fake_db.statements[service_index]
    assert "revoked_at IS NULL" in fake_db.statements[token_index]
    assert "expires_at > UTC_TIMESTAMP()" in fake_db.statements[token_index]
    assert "FOR UPDATE" in fake_db.statements[token_index]
    transaction_id = fake_db.transaction_ids[candidate_index]
    assert transaction_id is not None
    assert all(
        fake_db.transaction_ids[index] == transaction_id
        for index in (
            tenant_index,
            service_index,
            token_index,
            update_index,
        )
    )
    assert fake_db.tokens[issued.token_id]["last_used_at"] == fake_db.db_now
    assert fake_db.transactions_committed == 1
    assert fake_db.bound_parameters[update_index]["token_id"] == issued.token_id
    assert not _parameters_contain_secret(
        fake_db.bound_parameters[update_index], issued.raw_value
    )


def test_resolve_candidate_is_not_authorization_when_tenant_disappears(fake_db):
    issued = store.issue_token("service-a", "tenant-a")
    fake_db.statements.clear()
    fake_db.delete_tenant_after_candidate = True

    assert store.resolve_token(issued.raw_value, "service-a") is None

    assert fake_db.statements[0].startswith(
        "SELECT s.tenant_id FROM mcp_service_token"
    )
    assert fake_db.statements[1].startswith("SELECT enabled FROM tenant_config")
    assert len(fake_db.statements) == 2
    assert fake_db.tokens[issued.token_id]["last_used_at"] is None


def test_resolve_disabled_tenant_stops_before_any_lock_or_child_write(fake_db):
    issued = store.issue_token("service-a", "tenant-a")
    fake_db.statements.clear()
    fake_db.tenants["tenant-a"] = False

    assert store.resolve_token(issued.raw_value, "service-a") is None

    assert len(fake_db.statements) == 1
    assert "FOR UPDATE" not in fake_db.statements[0]
    assert fake_db.tokens[issued.token_id]["last_used_at"] is None


def test_resolve_revalidates_service_and_token_after_parent_locks(fake_db):
    issued = store.issue_token("service-a", "tenant-a")
    fake_db.statements.clear()
    fake_db.disable_service_after_tenant_lock = True

    assert store.resolve_token(issued.raw_value, "service-a") is None
    assert [
        sql.split(" ", 2)[:2] for sql in fake_db.statements
    ] == [["SELECT", "s.tenant_id"], ["SELECT", "enabled"], ["SELECT", "service_id,"]]
    assert fake_db.tokens[issued.token_id]["last_used_at"] is None

    fake_db.services["service-a"]["status"] = "active"
    fake_db.disable_service_after_tenant_lock = False
    fake_db.revoke_token_after_service_lock = True
    fake_db.statements.clear()

    assert store.resolve_token(issued.raw_value, "service-a") is None
    assert fake_db.statements[-1].startswith("SELECT token_id FROM mcp_service_token")
    assert not any(
        sql.startswith("UPDATE mcp_service_token SET last_used_at")
        for sql in fake_db.statements
    )


def test_resolve_token_ineligible_attempts_do_not_update_usage(fake_db):
    issued = store.issue_token(
        "service-a", "tenant-a", expires_at=datetime(2026, 7, 22, 12, 0, 1)
    )
    fake_db.db_now = datetime(2026, 7, 22, 12, 0, 1)

    assert store.resolve_token(issued.raw_value, "service-a") is None
    assert store.resolve_token(issued.raw_value, "service-b") is None
    assert store.resolve_token("wrong-token", "service-a") is None
    assert fake_db.tokens[issued.token_id]["last_used_at"] is None


def test_resolve_token_revoked_or_inactive_service_does_not_update_usage(fake_db):
    revoked = store.issue_token("service-a", "tenant-a")
    assert store.revoke_token("service-a", "tenant-a", revoked.token_id) is True
    assert store.resolve_token(revoked.raw_value, "service-a") is None
    assert fake_db.tokens[revoked.token_id]["last_used_at"] is None

    active = store.issue_token("service-a", "tenant-a")
    fake_db.services["service-a"]["status"] = "disabled"
    assert store.resolve_token(active.raw_value, "service-a") is None
    assert fake_db.tokens[active.token_id]["last_used_at"] is None


def test_expired_token_cannot_reveal_but_disabled_service_token_can(fake_db):
    expired = store.issue_token(
        "service-a", "tenant-a", expires_at=datetime(2026, 7, 22, 12, 0, 1)
    )
    fake_db.db_now = datetime(2026, 7, 22, 12, 0, 1)
    with pytest.raises(store.TokenUnavailableError):
        store.reveal_token("service-a", "tenant-a", expired.token_id)

    fake_db.db_now = datetime(2026, 7, 22, 12, 0, 0)
    fake_db.services["service-a"]["status"] = "disabled"
    assert store.reveal_token("service-a", "tenant-a", expired.token_id)


@pytest.mark.parametrize("failure", ["update", "commit"])
def test_resolve_token_update_or_commit_failure_fails_closed(fake_db, failure):
    issued = store.issue_token("service-a", "tenant-a")
    fake_db.fail_usage_update = failure == "update"
    fake_db.fail_commit = failure == "commit"

    with pytest.raises(RuntimeError):
        store.resolve_token(issued.raw_value, "service-a")
