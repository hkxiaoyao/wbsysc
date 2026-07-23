from dataclasses import FrozenInstanceError

import pytest

from app import db
from app.tenant_auth.models import IssuedTenantSession, TenantAccount, TenantPrincipal
from app.tenant_auth.passwords import hash_password, verify_password
from app.tenant_auth import store


class Result:
    def __init__(self, row=None):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeConnection:
    def __init__(self, account_row=None):
        self.account_row = account_row
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        values = dict(params or {})
        self.statements.append((sql, values))
        if "FROM tenant_account" in sql:
            return Result(self.account_row)
        return Result()


class FakeEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return self.connection

    def connect(self):
        return self.connection


class SessionConnection(FakeConnection):
    def __init__(self, principal_row=None):
        super().__init__()
        self.principal_row = principal_row

    def execute(self, statement, params=None):
        sql = str(statement)
        values = dict(params or {})
        self.statements.append((sql, values))
        if "FROM tenant_session" in sql:
            return Result(self.principal_row)
        return Result()


def test_password_hash_is_argon2_and_never_contains_raw_value():
    raw = "Tenant-Secure-123"

    encoded = hash_password(raw)

    assert encoded.startswith("$argon2id$")
    assert raw not in encoded
    assert verify_password(encoded, raw) is True
    assert verify_password(encoded, "wrong-password-456") is False


@pytest.mark.parametrize(
    "raw",
    ["short", "contains password word", " " * 16, "leading-space-password-123 "],
)
def test_password_hash_rejects_weak_or_ambiguous_values(raw):
    with pytest.raises(ValueError, match="password"):
        hash_password(raw)


def test_tenant_auth_contracts_are_immutable_and_do_not_hold_passwords():
    account = TenantAccount(
        tenant_id="tenant-a",
        status="active",
        failed_attempts=0,
    )
    principal = TenantPrincipal(principal_type="tenant", tenant_id="tenant-a")

    with pytest.raises(FrozenInstanceError):
        account.status = "disabled"
    assert principal.tenant_id == account.tenant_id
    assert "password" not in repr(account).lower()


def test_ensure_tenant_auth_tables_creates_account_table(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    store.ensure_tenant_auth_tables()

    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "CREATE TABLE IF NOT EXISTS `tenant_account`" in sql
    assert "password_hash" in sql


def test_authenticate_returns_active_account_and_never_binds_raw_password(monkeypatch):
    raw = "Tenant-Secure-123"
    connection = FakeConnection(
        {
            "tenant_id": "tenant-a",
            "password_hash": hash_password(raw),
            "status": "active",
            "failed_attempts": 2,
            "locked_until": None,
        }
    )
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    account = store.authenticate("tenant-a", raw)

    assert account == TenantAccount("tenant-a", "active", 0, None)
    assert raw not in repr(connection.statements)
    assert any("last_login_at" in sql for sql, _ in connection.statements)


def test_authenticate_does_not_hold_row_lock_during_argon2(monkeypatch):
    raw = "Tenant-Secure-123"
    connection = FakeConnection(
        {
            "tenant_id": "tenant-a",
            "password_hash": hash_password(raw),
            "status": "active",
            "failed_attempts": 0,
            "locked_until": None,
        }
    )
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))
    original_verify = store.verify_password

    def verify_without_existing_lock(hash_value, candidate):
        assert not any("FOR UPDATE" in sql for sql, _ in connection.statements)
        return original_verify(hash_value, candidate)

    monkeypatch.setattr(store, "verify_password", verify_without_existing_lock)

    assert store.authenticate("tenant-a", raw) is not None


def test_authenticate_increments_failures_without_disclosing_account_state(monkeypatch):
    connection = FakeConnection(
        {
            "tenant_id": "tenant-a",
            "password_hash": hash_password("Tenant-Secure-123"),
            "status": "active",
            "failed_attempts": 1,
            "locked_until": None,
        }
    )
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    assert store.authenticate("tenant-a", "Wrong-Secure-456") is None

    updates = [(sql, params) for sql, params in connection.statements if sql.lstrip().startswith("UPDATE")]
    assert updates[-1][1]["failed_attempts"] == 2
    assert "Wrong-Secure-456" not in repr(connection.statements)


def test_authenticate_rejects_disabled_account_without_updating_login(monkeypatch):
    connection = FakeConnection(
        {
            "tenant_id": "tenant-a",
            "password_hash": hash_password("Tenant-Secure-123"),
            "status": "disabled",
            "failed_attempts": 0,
            "locked_until": None,
        }
    )
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    assert store.authenticate("tenant-a", "Tenant-Secure-123") is None
    assert not any("last_login_at" in sql for sql, _ in connection.statements)


def test_issue_session_persists_digest_and_resolves_principal(monkeypatch):
    connection = SessionConnection({"tenant_id": "tenant-a"})
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    issued = store.issue_session("tenant-a", ttl_seconds=3600)
    principal = store.resolve_session(issued.raw_value)

    assert isinstance(issued, IssuedTenantSession)
    assert issued.tenant_id == "tenant-a"
    assert principal == TenantPrincipal(principal_type="tenant", tenant_id="tenant-a")
    inserts = [(sql, params) for sql, params in connection.statements if sql.lstrip().startswith("INSERT")]
    assert len(inserts[-1][1]["session_digest"]) == 64
    assert issued.raw_value not in repr(connection.statements)


def test_password_reset_and_disable_revoke_all_tenant_sessions(monkeypatch):
    connection = SessionConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    store.upsert_account("tenant-a", "Replacement-Secure-456")
    store.set_account_status("tenant-a", "disabled")

    revocations = [
        params
        for sql, params in connection.statements
        if sql.lstrip().startswith("UPDATE tenant_session")
    ]
    assert revocations == [{"tenant_id": "tenant-a"}, {"tenant_id": "tenant-a"}]


def test_account_mutations_can_join_caller_transaction(monkeypatch):
    connection = SessionConnection()
    monkeypatch.setattr(
        db,
        "get_engine",
        lambda: (_ for _ in ()).throw(AssertionError("opened a nested transaction")),
    )

    store.upsert_account(
        "tenant-a", "Replacement-Secure-456", conn=connection
    )
    changed = store.set_account_status("tenant-a", "disabled", conn=connection)

    assert changed is False
    statements = [sql.strip() for sql, _ in connection.statements]
    assert statements[0].startswith("INSERT INTO tenant_account")
    assert statements[1].startswith("UPDATE tenant_session")
    assert statements[2].startswith("UPDATE tenant_account")
    assert statements[3].startswith("UPDATE tenant_session")


def test_revoked_or_expired_session_does_not_resolve(monkeypatch):
    connection = SessionConnection(None)
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    assert store.resolve_session("missing-session-value") is None
    assert "missing-session-value" not in repr(connection.statements)
    session_query = next(sql for sql, _ in connection.statements if "FROM tenant_session" in sql)
    assert "JOIN tenant_config" in session_query
    assert "enabled=1" in session_query


def test_delete_account_removes_credentials_and_sessions_in_one_transaction(monkeypatch):
    connection = SessionConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    store.delete_account("tenant-a")

    statements = [sql.strip() for sql, _ in connection.statements]
    assert statements[0].startswith("DELETE FROM tenant_session")
    assert statements[1].startswith("DELETE FROM tenant_account")


def test_delete_account_uses_caller_transaction_without_opening_nested_one(monkeypatch):
    connection = SessionConnection()
    monkeypatch.setattr(
        db,
        "get_engine",
        lambda: (_ for _ in ()).throw(AssertionError("opened nested transaction")),
    )

    store.delete_account("tenant-a", conn=connection)

    statements = [sql.strip() for sql, _ in connection.statements]
    assert statements == [
        "DELETE FROM tenant_session WHERE tenant_id=:tenant_id",
        "DELETE FROM tenant_account WHERE tenant_id=:tenant_id",
    ]
