from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from app import db
from app.connections import store
from app.connectors.declarative.validator import import_openapi_revision


class Result:
    def __init__(self, row=None, rows=(), scalar_value=None):
        self.row = row
        self.rows = list(rows)
        self.scalar_value = scalar_value

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def scalar(self):
        return self.scalar_value

    def mappings(self):
        return self

    def all(self):
        return self.rows


class FakeConnection:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        params = params or {}
        self.statements.append((str(statement), params))
        if "FROM connection_token" in str(statement):
            if params["connection_id"] == "conn-a":
                return Result(
                    {
                        "connection_id": "conn-a",
                        "tenant_id": "tenant-a",
                        "connector_key": "wecom",
                        "display_name": "WeCom",
                        "status": "active",
                        "data_mode": "stored",
                        "public_config_json": "{}",
                        "config_version": 1,
                    }
                )
            return Result()
        return Result()


class FakeEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return self.connection

    def connect(self):
        return self.connection


class LockedMutationConnection(FakeConnection):
    def execute(self, statement, params=None):
        params = params or {}
        sql = str(statement)
        self.statements.append((sql, params))
        if "SELECT connection_id, tenant_id, connector_key" in sql:
            return Result(
                {
                    "connection_id": "conn-a",
                    "tenant_id": "tenant-a",
                    "connector_key": "wecom",
                    "display_name": "WeCom",
                    "status": "active",
                    "data_mode": "stored",
                    "public_config_json": "{}",
                    "config_version": 7,
                }
            )
        return Result()


def test_update_connection_locks_row_and_rejects_illegal_status_transition(monkeypatch):
    connection = LockedMutationConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    with pytest.raises(ValueError, match="status transition"):
        store.update_connection(
            "conn-a",
            "tenant-a",
            display_name="WeCom",
            data_mode="stored",
            public_config={},
            status="draft",
            expected_config_version=7,
        )

    selects = [sql for sql, _ in connection.statements if sql.lstrip().startswith("SELECT")]
    assert any("FOR UPDATE" in sql for sql in selects)
    assert not any(sql.lstrip().startswith("UPDATE") for sql, _ in connection.statements)


def test_update_connection_rejects_stale_expected_version_without_writing(monkeypatch):
    connection = LockedMutationConnection()
    events = []
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    def invalidator(connection_id, config_version):
        events.append((connection_id, config_version))

    store.register_connection_cache_invalidator(invalidator)
    try:
        with pytest.raises(store.ConnectionVersionConflictError):
            store.update_connection(
                "conn-a",
                "tenant-a",
                display_name="stale",
                data_mode="stored",
                public_config={},
                expected_config_version=6,
            )
    finally:
        store.unregister_connection_cache_invalidator(invalidator)

    assert not any(
        sql.lstrip().startswith("UPDATE") for sql, _ in connection.statements
    )
    assert events == []


def test_generic_store_update_cannot_activate_declarative_connection(monkeypatch):
    class DeclarativeConnection(LockedMutationConnection):
        def execute(self, statement, params=None):
            result = super().execute(statement, params)
            if result.row is not None:
                result.row["connector_key"] = "http_declarative"
                result.row["status"] = "disabled"
                result.row["public_config_json"] = (
                    '{"spec_id":"spec-a","revision":1,'
                    '"pending_spec_id":"spec-b","pending_revision":2}'
                )
            return result

    connection = DeclarativeConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    with pytest.raises(ValueError, match="activation"):
        store.update_connection(
            "conn-a",
            "tenant-a",
            display_name="API",
            data_mode="direct",
            public_config={
                "spec_id": "spec-a",
                "revision": 1,
                "pending_spec_id": "spec-b",
                "pending_revision": 2,
            },
            status="active",
            expected_config_version=7,
        )

    assert not any(
        sql.lstrip().startswith("UPDATE") for sql, _ in connection.statements
    )


def test_set_tool_policy_rejects_stale_expected_version_without_writing(monkeypatch):
    class StalePolicyConnection(FakeConnection):
        def execute(self, statement, params=None):
            params = params or {}
            sql = str(statement)
            self.statements.append((sql, params))
            if "SELECT config_version" in sql:
                return Result(scalar_value=7)
            raise AssertionError("stale policy reached a write")

    connection = StalePolicyConnection()
    events = []
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    def invalidator(connection_id, config_version):
        events.append((connection_id, config_version))

    store.register_connection_cache_invalidator(invalidator)
    try:
        with pytest.raises(store.ConnectionVersionConflictError):
            store.set_tool_policy(
                "conn-a",
                "reports.list",
                enabled=False,
                expected_config_version=6,
            )
    finally:
        store.unregister_connection_cache_invalidator(invalidator)

    assert len(connection.statements) == 1
    assert events == []


def test_direct_policy_write_makes_a_concurrent_activation_cas_stale(monkeypatch):
    class PolicyThenActivationConnection(FakeConnection):
        def __init__(self):
            super().__init__()
            self.version = 7

        def execute(self, statement, params=None):
            params = params or {}
            sql = str(statement)
            self.statements.append((sql, params))
            if "SELECT config_version" in sql:
                return Result(scalar_value=self.version)
            if sql.lstrip().startswith("UPDATE connection_instance SET config_version"):
                self.version += 1
                return Result()
            if "FROM connection_instance" in sql:
                return Result(
                    {
                        "connection_id": "conn-a",
                        "tenant_id": "tenant-a",
                        "connector_key": "http_declarative",
                        "display_name": "API",
                        "status": "disabled",
                        "data_mode": "direct",
                        "public_config_json": '{"pending_spec_id":"spec-b","pending_revision":2}',
                        "config_version": self.version,
                    }
                )
            return Result()

    connection = PolicyThenActivationConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    store.set_tool_policy(
        "conn-a",
        "new.health",
        enabled=True,
        expected_config_version=7,
    )

    with pytest.raises(store.ConnectionVersionConflictError):
        store.activate_declarative_revision(
            "spec-b",
            2,
            "tenant-a",
            "conn-a",
            expected_config_version=7,
        )

    assert connection.version == 8


def test_replacement_mutations_lock_connection_version_before_writes(monkeypatch):
    from app.connections.models import ToolPolicy

    class VersionLockConnection(FakeConnection):
        def execute(self, statement, params=None):
            params = params or {}
            sql = str(statement)
            self.statements.append((sql, params))
            if "SELECT config_version" in sql:
                return Result(scalar_value=7)
            if "SELECT tenant_id" in sql:
                return Result(scalar_value="tenant-a")
            return Result()

    monkeypatch.setattr(store, "encrypt_credential", lambda _value: b"ciphertext")
    monkeypatch.setattr(store, "token_hmac", lambda _value: "a" * 64)
    calls = (
        lambda: store.replace_credentials(
            "conn-a",
            "tenant-a",
            {"app_secret": "plain-secret"},
            expected_config_version=7,
        ),
        lambda: store.rotate_token("conn-a", "tenant-a"),
        lambda: store.replace_tool_policies(
            "conn-a",
            "tenant-a",
            [ToolPolicy("conn-a", "reports.list", True, {})],
            expected_config_version=7,
        ),
    )

    for call in calls:
        connection = VersionLockConnection()
        monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

        assert call()
        first_sql, _ = connection.statements[0]
        assert "SELECT config_version" in first_sql
        assert "FOR UPDATE" in first_sql
        assert "plain-secret" not in repr(connection.statements)


@pytest.mark.parametrize("mutation", ["credentials", "policies"])
def test_replacement_mutations_reject_stale_versions_without_writing(
    monkeypatch, mutation
):
    from app.connections.models import ToolPolicy

    class StaleConnection(FakeConnection):
        def execute(self, statement, params=None):
            params = params or {}
            sql = str(statement)
            self.statements.append((sql, params))
            if "SELECT config_version" in sql:
                return Result(scalar_value=7)
            raise AssertionError("stale mutation reached a write or owner lookup")

    connection = StaleConnection()
    events = []
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))
    def invalidator(connection_id, config_version):
        events.append((connection_id, config_version))

    store.register_connection_cache_invalidator(invalidator)
    if mutation == "credentials":
        def call():
            return store.replace_credentials(
                "conn-a", "tenant-a", {}, expected_config_version=6
            )
    else:
        def call():
            return store.replace_tool_policies(
                "conn-a",
                "tenant-a",
                [ToolPolicy("conn-a", "reports.list", True, {})],
                expected_config_version=6,
            )

    try:
        with pytest.raises(store.ConnectionVersionConflictError):
            call()
    finally:
        store.unregister_connection_cache_invalidator(invalidator)

    assert len(connection.statements) == 1
    assert events == []


def test_publish_locks_scope_revalidates_and_invalidates_committed_version(monkeypatch):
    revision = import_openapi_revision(
        {
            "openapi": "3.0.3",
            "servers": [{"url": "https://api.example.com"}],
            "paths": {
                "/health": {
                    "get": {
                        "operationId": "health.get",
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {"ok": {"type": "boolean"}},
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
        },
        spec_id="spec-a",
        revision=1,
        tenant_id="tenant-a",
        connection_id="conn-a",
    )

    class PublishConnection(MutationConnection):
        def execute(self, statement, params=None):
            params = params or {}
            sql = str(statement)
            self.statements.append((sql, params))
            if "FROM connection_instance" in sql and "SELECT connection_id" in sql:
                return Result(
                    {
                        "connection_id": "conn-a",
                        "tenant_id": "tenant-a",
                        "connector_key": "http_declarative",
                        "display_name": "API",
                        "status": "draft",
                        "data_mode": "direct",
                        "public_config_json": "{}",
                        "config_version": 4,
                    }
                )
            if "FROM declarative_spec_revision" in sql:
                return Result(
                    {
                        "spec_id": "spec-a",
                        "revision": 1,
                        "tenant_id": "tenant-a",
                        "connection_id": "conn-a",
                        "status": "draft",
                        "spec_json": json.dumps(revision.storage_document()),
                    }
                )
            return Result()

    connection = PublishConnection()
    events = []
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))
    def invalidator(cid, version):
        events.append((cid, version, connection.commit_succeeded))
    store.register_connection_cache_invalidator(invalidator)
    try:
            published = store.publish_declarative_revision(
                "spec-a",
                1,
                "tenant-a",
                "conn-a",
                expected_config_version=4,
            )
    finally:
        store.unregister_connection_cache_invalidator(invalidator)

    assert published.status == "published"
    locked = [sql for sql, _ in connection.statements if sql.lstrip().startswith("SELECT")]
    assert len(locked) == 2 and all("FOR UPDATE" in sql for sql in locked)
    config_update = next(
        params
        for sql, params in connection.statements
        if "UPDATE connection_instance SET public_config_json" in sql
    )
    assert json.loads(config_update["public_config_json"]) == {
        "spec_id": "spec-a",
        "revision": 1,
    }
    assert events == [("conn-a", 4, True)]


@pytest.mark.parametrize("operation", ["publish", "activate"])
def test_declarative_lifecycle_rejects_stale_connection_version_before_revision_write(
    monkeypatch, operation
):
    class StaleLifecycleConnection(FakeConnection):
        def execute(self, statement, params=None):
            params = params or {}
            sql = str(statement)
            self.statements.append((sql, params))
            if "FROM connection_instance" in sql:
                return Result(
                    {
                        "connection_id": "conn-a",
                        "tenant_id": "tenant-a",
                        "connector_key": "http_declarative",
                        "display_name": "API",
                        "status": "disabled",
                        "data_mode": "direct",
                        "public_config_json": '{"pending_spec_id":"spec-a","pending_revision":1}',
                        "config_version": 7,
                    }
                )
            raise AssertionError("stale lifecycle reached revision mutation")

    connection = StaleLifecycleConnection()
    events = []
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    def invalidator(connection_id, config_version):
        events.append((connection_id, config_version))

    store.register_connection_cache_invalidator(invalidator)
    lifecycle = (
        store.publish_declarative_revision
        if operation == "publish"
        else store.activate_declarative_revision
    )
    try:
        with pytest.raises(store.ConnectionVersionConflictError):
            lifecycle(
                "spec-a",
                1,
                "tenant-a",
                "conn-a",
                expected_config_version=6,
            )
    finally:
        store.unregister_connection_cache_invalidator(invalidator)

    assert len(connection.statements) == 1
    assert events == []


def test_activation_rejects_a_published_revision_that_is_not_staged(monkeypatch):
    class PendingConnection(FakeConnection):
        def execute(self, statement, params=None):
            params = params or {}
            sql = str(statement)
            self.statements.append((sql, params))
            if "FROM connection_instance" in sql:
                return Result(
                    {
                        "connection_id": "conn-a",
                        "tenant_id": "tenant-a",
                        "connector_key": "http_declarative",
                        "display_name": "API",
                        "status": "disabled",
                        "data_mode": "direct",
                        "public_config_json": '{"spec_id":"spec-a","revision":1,"pending_spec_id":"spec-b","pending_revision":2}',
                        "config_version": 7,
                    }
                )
            raise AssertionError("unstaged revision reached revision lookup")

    connection = PendingConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    with pytest.raises(ValueError, match="pending"):
        store.activate_declarative_revision(
            "spec-c",
            3,
            "tenant-a",
            "conn-a",
            expected_config_version=7,
        )

    assert len(connection.statements) == 1


class LegacyBackfillConnection(FakeConnection):
    def __init__(self):
        super().__init__()
        self.completed_connection_ids = set()
        self.token_connection_ids = {}

    def execute(self, statement, params=None):
        params = params or {}
        sql = str(statement)
        self.statements.append((sql, params))
        if "FROM tenant_config" in sql:
            return Result(
                rows=[
                    {
                        "tenant_id": "tenant-a",
                        "display_name": "Legacy tenant",
                        "corpid": "ww123",
                        "secret_encrypted": b"encrypted-app-secret",
                        "mcp_token": "legacy-token",
                        "schema_name": "wbd_abc",
                        "sync_interval_min": 30,
                        "enabled_modules": "report,approval",
                        "checkin_userids": "u1,u2",
                        "contact_secret_encrypted": b"encrypted-contact-secret",
                        "trusted_domain": "example.test",
                        "data_mode": "stored",
                        "enabled": 1,
                    }
                ]
            )
        if "SELECT state_json FROM connection_sync_state" in sql:
            connection_id = params["connection_id"]
            return Result(
                scalar_value=(
                    '{"status":"completed"}'
                    if connection_id in self.completed_connection_ids
                    else None
                )
            )
        if "SELECT connection_id FROM connection_token" in sql:
            return Result(scalar_value=self.token_connection_ids.get(params["token_hmac"]))
        if "INSERT INTO connection_token" in sql:
            self.token_connection_ids[params["token_hmac"]] = params["connection_id"]
        if "INSERT INTO connection_sync_state" in sql:
            self.completed_connection_ids.add(params["connection_id"])
        return Result()


class QueryConnection(FakeConnection):
    def execute(self, statement, params=None):
        params = params or {}
        sql = str(statement)
        self.statements.append((sql, params))
        row = {
            "connection_id": "conn-a",
            "tenant_id": "tenant-a",
            "connector_key": "wecom",
            "display_name": "WeCom",
            "status": "active",
            "data_mode": "stored",
            "public_config_json": '{"corpid":"ww123"}',
            "config_version": 2,
        }
        if "FROM connection_instance" in sql and "LIMIT 1" in sql:
            return Result(row=row)
        if "FROM connection_instance" in sql:
            return Result(rows=[row])
        return Result()


class CollidingLegacyBackfillConnection(LegacyBackfillConnection):
    def execute(self, statement, params=None):
        if "SELECT connection_id FROM connection_token" in str(statement):
            params = params or {}
            self.statements.append((str(statement), params))
            return Result(scalar_value="other-connection")
        return super().execute(statement, params)


class RacingLegacyBackfillConnection(LegacyBackfillConnection):
    def __init__(self):
        super().__init__()
        self.token_owner_reads = 0

    def execute(self, statement, params=None):
        if "SELECT connection_id FROM connection_token" in str(statement):
            params = params or {}
            self.statements.append((str(statement), params))
            self.token_owner_reads += 1
            return Result(
                scalar_value=(
                    None if self.token_owner_reads == 1 else "other-connection"
                )
            )
        return super().execute(statement, params)


class MutationConnection(FakeConnection):
    def __init__(self, *, fail_policy_write=False):
        super().__init__()
        self.commit_succeeded = False
        self.fail_policy_write = fail_policy_write

    def __exit__(self, exc_type, _exc, _traceback):
        self.commit_succeeded = exc_type is None
        return False

    def execute(self, statement, params=None):
        params = params or {}
        sql = str(statement)
        self.statements.append((sql, params))
        if "SELECT config_version FROM connection_instance" in sql:
            return Result(scalar_value=7)
        if self.fail_policy_write and "INSERT INTO connection_tool_policy" in sql:
            raise RuntimeError("write failed")
        return Result()


def test_token_resolution_requires_matching_connection_id(monkeypatch):
    fake_connection = FakeConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(fake_connection))
    monkeypatch.setattr(store, "token_hmac", lambda raw_token: "a" * 64)

    token = store.issue_token("conn-a", "token-a")

    assert store.resolve_connection_token("token-a", "conn-a").connection_id == "conn-a"
    assert store.resolve_connection_token("token-a", "conn-b") is None
    assert token.raw_value == "token-a"


def test_token_row_never_contains_raw_value(monkeypatch):
    fake_connection = FakeConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(fake_connection))
    monkeypatch.setattr(store, "token_hmac", lambda raw_token: "a" * 64)

    store.issue_token("conn-a", "token-a")

    params = fake_connection.statements[-1][1]
    assert "token-a" not in repr(params)
    assert params["token_hmac"] != "token-a"


def test_issue_token_is_idempotent_without_reassigning_an_existing_digest(
    monkeypatch,
):
    fake_connection = FakeConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(fake_connection))
    monkeypatch.setattr(store, "token_hmac", lambda raw_token: "a" * 64)

    store.issue_token("conn-a", "token-a")

    sql, _ = fake_connection.statements[-1]
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "connection_id=VALUES(connection_id)" not in sql


@pytest.mark.parametrize("raw_value", ["", 0])
def test_issue_token_rejects_an_explicit_invalid_raw_value(raw_value):
    with pytest.raises(ValueError, match="raw_value"):
        store.issue_token("conn-a", raw_value)


def test_connection_contracts_are_immutable_and_keep_secrets_encrypted():
    from app.connections.models import (
        ConnectionToken,
        CredentialRecord,
        SyncState,
        ToolPolicy,
    )

    credential = CredentialRecord(
        connection_id="conn-a",
        credential_key="app_secret",
        encrypted_value=b"ciphertext",
        metadata={"source": "test"},
    )
    token = ConnectionToken(
        token_id="token-id",
        connection_id="conn-a",
        token_hmac="a" * 64,
        prefix="a" * 12,
    )
    policy = ToolPolicy(
        connection_id="conn-a",
        tool_name="wecom_list_reports",
        enabled=True,
        policy={"allow": True},
    )
    state = SyncState(
        connection_id="conn-a",
        state_key="report",
        state={"cursor": "123"},
    )

    assert credential.encrypted_value == b"ciphertext"
    assert token.token_hmac == "a" * 64
    assert policy.enabled is True
    assert state.state["cursor"] == "123"
    with pytest.raises(FrozenInstanceError):
        policy.enabled = False


def test_connection_crypto_reuses_the_existing_secret_cipher(monkeypatch):
    from app.connections import crypto

    monkeypatch.setattr(crypto, "encrypt_secret", lambda value: f"enc:{value}".encode())
    monkeypatch.setattr(crypto, "decrypt_secret", lambda value: value.decode()[4:])

    encrypted = crypto.encrypt_credential("plain-secret")

    assert encrypted == b"enc:plain-secret"
    assert crypto.decrypt_credential(encrypted) == "plain-secret"


def test_create_connection_binds_public_config_and_encrypts_credentials(monkeypatch):
    from app.connections.models import ConnectionRecord

    fake_connection = FakeConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(fake_connection))
    monkeypatch.setattr(store, "encrypt_credential", lambda value: b"ciphertext")
    record = ConnectionRecord(
        connection_id="conn-a",
        tenant_id="tenant-a",
        connector_key="wecom",
        display_name="WeCom",
        status="active",
        data_mode="stored",
        public_config={"corpid": "ww123"},
        config_version=1,
    )

    store.create_connection(record, credentials={"app_secret": "plain-secret"})

    connection_sql, connection_params = next(
        (sql, params)
        for sql, params in fake_connection.statements
        if "INSERT INTO connection_instance" in sql
    )
    _credential_sql, credential_params = next(
        (sql, params)
        for sql, params in fake_connection.statements
        if "INSERT INTO connection_credential" in sql
    )
    assert "tenant-a" not in connection_sql
    assert connection_params["tenant_id"] == "tenant-a"
    assert credential_params["encrypted_value"] == b"ciphertext"
    assert "plain-secret" not in repr(credential_params)


def test_ensure_connection_tables_uses_mysql57_compatible_ddl(monkeypatch):
    fake_connection = FakeConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(fake_connection))

    store.ensure_connection_tables()

    sql = "\n".join(statement for statement, _ in fake_connection.statements).lower()
    for table in (
        "connection_instance",
        "connection_credential",
        "connection_token",
        "connection_tool_policy",
        "connection_sync_state",
        "declarative_spec_revision",
        "declarative_spec_operation",
    ):
        assert f"create table if not exists `{table}`" in sql
    assert "unique key `uk_connection_token_hmac` (`token_hmac`)" in sql
    assert "add column if not exists" not in sql


def test_connection_platform_migration_expands_declarative_documents_idempotently():
    migration = (
        Path(__file__).resolve().parents[1] / "sql" / "006_connection_platform.sql"
    ).read_text(encoding="utf-8").lower()

    assert "`spec_json` mediumtext not null" in migration
    assert "`operation_json` mediumtext not null" in migration
    assert migration.count("from information_schema.columns") >= 2
    assert "modify column `spec_json` mediumtext not null" in migration
    assert "modify column `operation_json` mediumtext not null" in migration
    assert migration.count("prepare ") >= 2
    assert "add column if not exists" not in migration


def test_legacy_wecom_backfill_is_idempotent_and_never_repersists_raw_token(
    monkeypatch,
):
    fake_connection = LegacyBackfillConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(fake_connection))
    monkeypatch.setattr(store, "token_hmac", lambda raw_token: "b" * 64)

    assert store.migrate_legacy_wecom_connections() == 1
    assert store.migrate_legacy_wecom_connections() == 0

    statements = fake_connection.statements
    assert all("legacy-token" not in repr(params) for _, params in statements)
    copy_index = next(
        index
        for index, (sql, _) in enumerate(statements)
        if "INSERT INTO connection_instance" in sql
    )
    watermark_index = next(
        index
        for index, (sql, _) in enumerate(statements)
        if "INSERT INTO connection_sync_state" in sql
    )
    assert copy_index < watermark_index


def test_legacy_backfill_does_not_reassign_an_existing_token_digest(monkeypatch):
    fake_connection = CollidingLegacyBackfillConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(fake_connection))
    monkeypatch.setattr(store, "token_hmac", lambda raw_token: "b" * 64)

    assert store.migrate_legacy_wecom_connections() == 0

    sql = "\n".join(statement for statement, _ in fake_connection.statements)
    assert "SELECT connection_id FROM connection_token" in sql
    assert "INSERT INTO connection_token" not in sql
    assert "INSERT INTO connection_sync_state" not in sql


def test_legacy_backfill_rechecks_token_owner_before_writing_a_watermark(
    monkeypatch,
):
    fake_connection = RacingLegacyBackfillConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(fake_connection))
    monkeypatch.setattr(store, "token_hmac", lambda raw_token: "b" * 64)

    assert store.migrate_legacy_wecom_connections() == 0

    sql = "\n".join(statement for statement, _ in fake_connection.statements)
    assert sql.count("SELECT connection_id FROM connection_token") == 2
    assert "INSERT INTO connection_sync_state" not in sql


def test_connection_queries_are_tenant_scoped_and_return_typed_records(monkeypatch):
    fake_connection = QueryConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(fake_connection))

    record = store.get_connection("conn-a", tenant_id="tenant-a")
    records = store.list_connections("tenant-a")

    assert record.connection_id == "conn-a"
    assert record.public_config == {"corpid": "ww123"}
    assert records == [record]
    get_sql, get_params = fake_connection.statements[0]
    list_sql, list_params = fake_connection.statements[1]
    assert "tenant-a" not in get_sql and "tenant-a" not in list_sql
    assert get_params == {"connection_id": "conn-a", "tenant_id": "tenant-a"}
    assert list_params == {"tenant_id": "tenant-a"}


def test_set_tool_policy_uses_bound_json_and_returns_typed_policy(monkeypatch):
    fake_connection = MutationConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(fake_connection))

    policy = store.set_tool_policy(
        "conn-a",
        "wecom_list_reports",
        enabled=False,
        policy={"reason": "restricted"},
        expected_config_version=7,
    )

    sql, params = next(
        (sql, params)
        for sql, params in fake_connection.statements
        if "INSERT INTO connection_tool_policy" in sql
    )
    assert policy.enabled is False
    assert policy.policy == {"reason": "restricted"}
    assert "restricted" not in sql
    assert params["policy_json"] == '{"reason":"restricted"}'


def test_successful_connection_mutations_notify_after_commit_without_raw_secrets(
    monkeypatch,
):
    from app.connections.models import ConnectionRecord

    fake_connection = MutationConnection()
    events = []
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(fake_connection))
    monkeypatch.setattr(store, "encrypt_credential", lambda _value: b"ciphertext")
    monkeypatch.setattr(store, "token_hmac", lambda _value: "a" * 64)

    def invalidator(connection_id, config_version):
        assert fake_connection.commit_succeeded is True
        events.append((connection_id, config_version))

    store.register_connection_cache_invalidator(invalidator)
    try:
        store.create_connection(
            ConnectionRecord(
                connection_id="conn-a",
                tenant_id="tenant-a",
                connector_key="wecom",
                display_name="WeCom",
                status="disabled",
                data_mode="stored",
                public_config={},
                config_version=8,
            ),
            credentials={"app_secret": "plain-secret"},
        )
        store.issue_token("conn-a", "raw-token")
        store.set_tool_policy(
            "conn-a",
            "reports.list",
            enabled=False,
            expected_config_version=7,
        )
    finally:
        store.unregister_connection_cache_invalidator(invalidator)

    assert events == [("conn-a", 7), ("conn-a", 7), ("conn-a", 7)]
    assert "plain-secret" not in repr(events)
    assert "raw-token" not in repr(events)


def test_failed_connection_mutation_does_not_notify_before_transaction_commit(
    monkeypatch,
):
    fake_connection = MutationConnection(fail_policy_write=True)
    events = []
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(fake_connection))

    def invalidator(connection_id, config_version):
        events.append((connection_id, config_version))

    store.register_connection_cache_invalidator(invalidator)
    try:
        with pytest.raises(RuntimeError, match="write failed"):
            store.set_tool_policy(
                "conn-a",
                "reports.list",
                enabled=False,
                expected_config_version=7,
            )
    finally:
        store.unregister_connection_cache_invalidator(invalidator)

    assert fake_connection.commit_succeeded is False
    assert events == []
