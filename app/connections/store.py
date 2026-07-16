from __future__ import annotations

import json
import logging
import secrets
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Mapping

from sqlalchemy import text

from .crypto import encrypt_credential, token_hmac
from .models import ConnectionRecord, IssuedToken, ToolPolicy


logger = logging.getLogger(__name__)

_LEGACY_WATERMARK_KEY = "legacy_wecom_backfill_v1"
_LEGACY_WATERMARK_STATUS = "completed"

# The gateway owns the callback registration.  The store only emits the safe,
# exact cache key after a transaction has committed; it never receives raw
# credentials or bearer tokens.
ConnectionCacheInvalidator = Callable[[str, int], None]
_connection_cache_invalidators: list[ConnectionCacheInvalidator] = []
_connection_cache_invalidator_lock = threading.Lock()


_CONNECTION_DDLS = (
    """
    CREATE TABLE IF NOT EXISTS `connection_instance` (
      `connection_id` VARCHAR(64) NOT NULL,
      `tenant_id` VARCHAR(64) NOT NULL,
      `connector_key` VARCHAR(64) NOT NULL,
      `display_name` VARCHAR(128) NOT NULL DEFAULT '',
      `status` VARCHAR(16) NOT NULL DEFAULT 'draft',
      `data_mode` VARCHAR(16) NOT NULL DEFAULT 'stored',
      `public_config_json` TEXT NOT NULL,
      `config_version` INT NOT NULL DEFAULT 1,
      `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (`connection_id`),
      KEY `idx_connection_instance_tenant` (`tenant_id`, `status`, `connector_key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS `connection_credential` (
      `connection_id` VARCHAR(64) NOT NULL,
      `credential_key` VARCHAR(64) NOT NULL,
      `encrypted_value` VARBINARY(4096) NOT NULL,
      `metadata_json` TEXT NOT NULL,
      `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (`connection_id`, `credential_key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS `connection_token` (
      `token_id` VARCHAR(64) NOT NULL,
      `connection_id` VARCHAR(64) NOT NULL,
      `token_hmac` CHAR(64) NOT NULL,
      `token_prefix` VARCHAR(32) NOT NULL,
      `token_label` VARCHAR(128) NOT NULL DEFAULT '',
      `expires_at` DATETIME NULL,
      `revoked_at` DATETIME NULL,
      `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (`token_id`),
      UNIQUE KEY `uk_connection_token_hmac` (`token_hmac`),
      KEY `idx_connection_token_connection` (`connection_id`, `revoked_at`, `expires_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS `connection_tool_policy` (
      `connection_id` VARCHAR(64) NOT NULL,
      `tool_name` VARCHAR(128) NOT NULL,
      `enabled` TINYINT NOT NULL DEFAULT 1,
      `policy_json` TEXT NOT NULL,
      `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (`connection_id`, `tool_name`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS `connection_sync_state` (
      `connection_id` VARCHAR(64) NOT NULL,
      `state_key` VARCHAR(64) NOT NULL,
      `state_json` TEXT NOT NULL,
      `last_success_at` DATETIME NULL,
      `last_error` VARCHAR(512) NOT NULL DEFAULT '',
      `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (`connection_id`, `state_key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS `declarative_spec_revision` (
      `spec_id` VARCHAR(64) NOT NULL,
      `revision` INT NOT NULL,
      `tenant_id` VARCHAR(64) NOT NULL,
      `connection_id` VARCHAR(64) NOT NULL,
      `status` VARCHAR(16) NOT NULL DEFAULT 'draft',
      `spec_json` MEDIUMTEXT NOT NULL,
      `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (`tenant_id`, `spec_id`, `revision`),
      KEY `idx_declarative_spec_tenant` (`tenant_id`, `connection_id`, `status`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS `declarative_spec_operation` (
      `operation_id` VARCHAR(64) NOT NULL,
      `tenant_id` VARCHAR(64) NOT NULL,
      `spec_id` VARCHAR(64) NOT NULL,
      `revision` INT NOT NULL,
      `connection_id` VARCHAR(64) NOT NULL,
      `operation_key` VARCHAR(128) NOT NULL,
      `operation_json` MEDIUMTEXT NOT NULL,
      `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (`operation_id`),
      UNIQUE KEY `uk_declarative_spec_operation`
        (`tenant_id`, `spec_id`, `revision`, `operation_key`),
      KEY `idx_declarative_operation_connection` (`connection_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


def _engine():
    from ..db import get_engine

    return get_engine()


def register_connection_cache_invalidator(
    invalidator: ConnectionCacheInvalidator,
) -> None:
    """Register one lifecycle-owned post-commit cache invalidator."""
    if not callable(invalidator):
        raise TypeError("invalidator must be callable")
    with _connection_cache_invalidator_lock:
        if not any(existing is invalidator for existing in _connection_cache_invalidators):
            _connection_cache_invalidators.append(invalidator)


def unregister_connection_cache_invalidator(
    invalidator: ConnectionCacheInvalidator,
) -> None:
    """Remove one lifecycle-owned invalidator without disturbing other apps."""
    with _connection_cache_invalidator_lock:
        _connection_cache_invalidators[:] = [
            existing
            for existing in _connection_cache_invalidators
            if existing is not invalidator
        ]


def _retired_connection_version(conn: Any, connection_id: str) -> int | None:
    """Return the cached revision that was live before a mutation, if any."""
    value = conn.execute(
        text("""
            SELECT config_version FROM connection_instance
            WHERE connection_id=:connection_id
            LIMIT 1
        """),
        {"connection_id": connection_id},
    ).scalar()
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _notify_connection_cache_invalidator(
    connection_id: str,
    config_version: int | None,
) -> None:
    """Best-effort cache retirement after commit, with no secret-bearing data."""
    if config_version is None:
        return
    with _connection_cache_invalidator_lock:
        invalidators = tuple(_connection_cache_invalidators)
    for invalidator in invalidators:
        try:
            invalidator(connection_id, config_version)
        except Exception as exc:
            logger.warning(
                "Connection cache invalidation hook failed type=%s",
                type(exc).__name__,
            )


def _connection_from_row(row: Any) -> ConnectionRecord:
    values = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    return ConnectionRecord(
        connection_id=values["connection_id"],
        tenant_id=values["tenant_id"],
        connector_key=values["connector_key"],
        display_name=values["display_name"],
        status=values["status"],
        data_mode=values["data_mode"],
        public_config=json.loads(values["public_config_json"] or "{}"),
        config_version=int(values["config_version"]),
    )


def ensure_connection_tables() -> None:
    """Create the central connection-platform tables if they are absent."""
    with _engine().begin() as conn:
        for ddl in _CONNECTION_DDLS:
            conn.execute(text(ddl))
        _migrate_declarative_tenant_identity(conn)


def _declarative_index_columns(conn: Any, table_name: str, index_name: str) -> str:
    value = conn.execute(
        text("""
            SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',')
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA=DATABASE()
              AND TABLE_NAME=:table_name AND INDEX_NAME=:index_name
        """),
        {"table_name": table_name, "index_name": index_name},
    ).scalar()
    return value.lower() if isinstance(value, str) else ""


def _migrate_declarative_tenant_identity(conn: Any) -> None:
    """Idempotently upgrade pre-tenant declarative keys on MySQL 5.7."""
    tenant_column = conn.execute(
        text("""
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA=DATABASE()
              AND TABLE_NAME='declarative_spec_operation'
              AND COLUMN_NAME='tenant_id'
        """)
    ).scalar()
    added_tenant_column = not bool(tenant_column)
    if added_tenant_column:
        conn.execute(text("""
            ALTER TABLE declarative_spec_operation
            ADD COLUMN `tenant_id` VARCHAR(64) NOT NULL DEFAULT '' AFTER `operation_id`
        """))

    # This remains safe to retry after an interrupted DDL migration.  The old
    # global revision key guarantees one unambiguous tenant for every old row.
    conn.execute(text("""
        UPDATE declarative_spec_operation AS operation_row
        JOIN declarative_spec_revision AS revision_row
          ON revision_row.spec_id=operation_row.spec_id
         AND revision_row.revision=operation_row.revision
        SET operation_row.tenant_id=revision_row.tenant_id
        WHERE operation_row.tenant_id=''
    """))
    orphan_count = conn.execute(text("""
        SELECT COUNT(*) FROM declarative_spec_operation WHERE tenant_id=''
    """)).scalar()
    if isinstance(orphan_count, int) and orphan_count > 0:
        raise RuntimeError("declarative tenant identity migration is incomplete")
    tenant_column_shape = conn.execute(
        text("""
            SELECT CONCAT(
                IS_NULLABLE, '|',
                IF(COLUMN_DEFAULT IS NULL, '<NULL>', COLUMN_DEFAULT)
            )
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA=DATABASE()
              AND TABLE_NAME='declarative_spec_operation'
              AND COLUMN_NAME='tenant_id'
            LIMIT 1
        """)
    ).scalar()
    if added_tenant_column or tenant_column_shape != "NO|<NULL>":
        conn.execute(text("""
            ALTER TABLE declarative_spec_operation
            MODIFY COLUMN `tenant_id` VARCHAR(64) NOT NULL
        """))

    revision_pk = _declarative_index_columns(
        conn,
        "declarative_spec_revision",
        "PRIMARY",
    )
    if revision_pk != "tenant_id,spec_id,revision":
        conn.execute(text("""
            ALTER TABLE declarative_spec_revision
            DROP PRIMARY KEY,
            ADD PRIMARY KEY (`tenant_id`, `spec_id`, `revision`)
        """))

    operation_unique = _declarative_index_columns(
        conn,
        "declarative_spec_operation",
        "uk_declarative_spec_operation",
    )
    if operation_unique != "tenant_id,spec_id,revision,operation_key":
        if operation_unique:
            conn.execute(text("""
                ALTER TABLE declarative_spec_operation
                DROP INDEX `uk_declarative_spec_operation`
            """))
        conn.execute(text("""
            ALTER TABLE declarative_spec_operation
            ADD UNIQUE KEY `uk_declarative_spec_operation`
              (`tenant_id`, `spec_id`, `revision`, `operation_key`)
        """))


def create_connection(
    record: ConnectionRecord,
    credentials: Mapping[str, str] | None = None,
) -> ConnectionRecord:
    """Persist a connection and encrypt supplied third-party credential values."""
    if not isinstance(record, ConnectionRecord):
        raise TypeError("record must be a ConnectionRecord")
    if not record.connection_id or not record.tenant_id or not record.connector_key:
        raise ValueError("connection_id, tenant_id, and connector_key are required")
    if record.status not in {"draft", "active", "disabled", "error"}:
        raise ValueError("invalid connection status")
    if record.data_mode not in {"direct", "stored", "hybrid"}:
        raise ValueError("invalid connection data_mode")

    retired_version: int | None
    with _engine().begin() as conn:
        retired_version = _retired_connection_version(conn, record.connection_id)
        conn.execute(
            text("""
                INSERT INTO connection_instance
                    (connection_id, tenant_id, connector_key, display_name, status,
                     data_mode, public_config_json, config_version)
                VALUES (:connection_id, :tenant_id, :connector_key, :display_name,
                        :status, :data_mode, :public_config_json, :config_version)
                ON DUPLICATE KEY UPDATE
                    tenant_id=VALUES(tenant_id),
                    connector_key=VALUES(connector_key),
                    display_name=VALUES(display_name),
                    status=VALUES(status),
                    data_mode=VALUES(data_mode),
                    public_config_json=VALUES(public_config_json),
                    config_version=VALUES(config_version)
            """),
            {
                "connection_id": record.connection_id,
                "tenant_id": record.tenant_id,
                "connector_key": record.connector_key,
                "display_name": record.display_name,
                "status": record.status,
                "data_mode": record.data_mode,
                "public_config_json": json.dumps(
                    record.public_config, ensure_ascii=False, separators=(",", ":")
                ),
                "config_version": record.config_version,
            },
        )
        for credential_key, plaintext in (credentials or {}).items():
            if not credential_key:
                raise ValueError("credential_key is required")
            if not isinstance(plaintext, str):
                raise TypeError("credential values must be strings")
            conn.execute(
                text("""
                    INSERT INTO connection_credential
                        (connection_id, credential_key, encrypted_value, metadata_json)
                    VALUES (:connection_id, :credential_key, :encrypted_value, :metadata_json)
                    ON DUPLICATE KEY UPDATE
                        encrypted_value=VALUES(encrypted_value),
                        metadata_json=VALUES(metadata_json)
                """),
                {
                    "connection_id": record.connection_id,
                    "credential_key": credential_key,
                    "encrypted_value": encrypt_credential(plaintext),
                    "metadata_json": json.dumps({"source": "runtime"}),
                },
            )
    _notify_connection_cache_invalidator(
        record.connection_id,
        record.config_version if retired_version is None else retired_version,
    )
    return record


def get_connection(
    connection_id: str, tenant_id: str | None = None
) -> ConnectionRecord | None:
    if not connection_id:
        raise ValueError("connection_id is required")
    clauses = ["connection_id=:connection_id"]
    params: dict[str, Any] = {"connection_id": connection_id}
    if tenant_id is not None:
        clauses.append("tenant_id=:tenant_id")
        params["tenant_id"] = tenant_id
    statement = text(f"""
        SELECT connection_id, tenant_id, connector_key, display_name, status,
               data_mode, public_config_json, config_version
        FROM connection_instance
        WHERE {' AND '.join(clauses)}
        LIMIT 1
    """)
    with _engine().connect() as conn:
        row = conn.execute(statement, params).fetchone()
    return _connection_from_row(row) if row is not None else None


def list_connections(tenant_id: str) -> list[ConnectionRecord]:
    if not tenant_id:
        raise ValueError("tenant_id is required")
    statement = text("""
        SELECT connection_id, tenant_id, connector_key, display_name, status,
               data_mode, public_config_json, config_version
        FROM connection_instance
        WHERE tenant_id=:tenant_id
        ORDER BY created_at, connection_id
    """)
    with _engine().connect() as conn:
        rows = conn.execute(statement, {"tenant_id": tenant_id}).mappings().all()
    return [_connection_from_row(row) for row in rows]


def save_declarative_revision(revision: Any) -> None:
    """Persist one compiled revision without accepting a raw upstream payload.

    A revision is append-only: the primary key intentionally has no upsert
    clause, so a published revision cannot be overwritten in place.  Every
    value, including serialized operation metadata, is sent as a bound
    parameter rather than interpolated into SQL.
    """
    from app.connectors.declarative.models import DeclarativeRevision, MAX_DOCUMENT_BYTES
    from app.connectors.declarative.validator import validate_revision

    if not isinstance(revision, DeclarativeRevision):
        raise TypeError("revision must be a DeclarativeRevision")
    revision = validate_revision(revision)
    if not revision.spec_id or not revision.tenant_id or not revision.connection_id:
        raise ValueError("declarative revision identity is required")
    document = revision.storage_document()
    try:
        spec_json = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("declarative revision is not JSON serializable") from None
    if len(spec_json.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ValueError("declarative revision exceeds size limit")

    with _engine().begin() as conn:
        conn.execute(
            text("""
                INSERT INTO declarative_spec_revision
                    (spec_id, revision, tenant_id, connection_id, status, spec_json)
                VALUES (:spec_id, :revision, :tenant_id, :connection_id, :status, :spec_json)
            """),
            {
                "spec_id": revision.spec_id,
                "revision": revision.revision,
                "tenant_id": revision.tenant_id,
                "connection_id": revision.connection_id,
                "status": revision.status,
                "spec_json": spec_json,
            },
        )
        for operation in document["operations"]:
            operation_json = json.dumps(
                operation,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            conn.execute(
                text("""
                    INSERT INTO declarative_spec_operation
                        (operation_id, tenant_id, spec_id, revision, connection_id,
                         operation_key, operation_json)
                    VALUES (:operation_id, :tenant_id, :spec_id, :revision,
                            :connection_id, :operation_key, :operation_json)
                """),
                {
                    "operation_id": str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"wbsysc:declarative:{revision.tenant_id}:{revision.spec_id}:{revision.revision}:{operation['tool_key']}",
                        )
                    ),
                    "spec_id": revision.spec_id,
                    "revision": revision.revision,
                    "tenant_id": revision.tenant_id,
                    "connection_id": revision.connection_id,
                    "operation_key": operation["tool_key"],
                    "operation_json": operation_json,
                },
            )


def get_published_declarative_revision(
    spec_id: str,
    revision: int,
    tenant_id: str,
) -> Any | None:
    """Load one tenant-scoped, published declarative revision safely.

    The database stores only the compiled, credential-free declaration.  It
    is revalidated before use rather than trusted merely because it came from
    our persistence layer, which preserves the same execution boundary after
    corruption or an out-of-band database change.
    """
    from app.connectors.declarative.models import DeclarativeRevision, MAX_DOCUMENT_BYTES

    if (
        not isinstance(spec_id, str)
        or not spec_id
        or not isinstance(tenant_id, str)
        or not tenant_id
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
    ):
        raise ValueError("declarative revision identity is required")
    statement = text("""
        SELECT spec_id, revision, tenant_id, connection_id, status, spec_json
        FROM declarative_spec_revision
        WHERE spec_id=:spec_id AND revision=:revision AND tenant_id=:tenant_id
          AND status='published'
        LIMIT 1
    """)
    with _engine().connect() as conn:
        row = conn.execute(
            statement,
            {"spec_id": spec_id, "revision": revision, "tenant_id": tenant_id},
        ).fetchone()
    if row is None:
        return None
    values = _row_values(row)
    raw_document = values.get("spec_json")
    if isinstance(raw_document, str):
        encoded_document = raw_document.encode("utf-8")
    elif isinstance(raw_document, bytes):
        encoded_document = raw_document
    else:
        raise ValueError("stored declarative revision is invalid")
    if len(encoded_document) > MAX_DOCUMENT_BYTES:
        raise ValueError("stored declarative revision is invalid")
    try:
        document = json.loads(encoded_document)
        row_spec_id = values["spec_id"]
        row_revision = values["revision"]
        row_tenant_id = values["tenant_id"]
        row_connection_id = values["connection_id"]
        row_status = values["status"]
        if (
            row_spec_id != spec_id
            or row_revision != revision
            or row_tenant_id != tenant_id
            or row_status != "published"
        ):
            raise ValueError("stored declarative revision is invalid")
        loaded = DeclarativeRevision.from_storage_document(
            spec_id=row_spec_id,
            revision=row_revision,
            tenant_id=row_tenant_id,
            connection_id=row_connection_id,
            status=row_status,
            document=document,
        )
        from app.connectors.declarative.validator import validate_revision

        return validate_revision(loaded)
    except Exception:
        # Neither malformed stored JSON nor a rejected declaration should leak
        # raw database content into a connector response or log message.
        raise ValueError("stored declarative revision is invalid") from None


def _declarative_revision_from_row(row: Any) -> Any:
    from app.connectors.declarative.models import DeclarativeRevision, MAX_DOCUMENT_BYTES
    from app.connectors.declarative.validator import validate_revision

    values = _row_values(row)
    raw = values.get("spec_json")
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(encoded, bytes) or len(encoded) > MAX_DOCUMENT_BYTES:
        raise ValueError("stored declarative revision is invalid")
    try:
        loaded = DeclarativeRevision.from_storage_document(
            spec_id=values["spec_id"],
            revision=int(values["revision"]),
            tenant_id=values["tenant_id"],
            connection_id=values["connection_id"],
            status=values["status"],
            document=json.loads(encoded),
        )
        return validate_revision(loaded)
    except Exception:
        raise ValueError("stored declarative revision is invalid") from None


def get_declarative_revision(
    spec_id: str, revision: int, tenant_id: str, connection_id: str
) -> Any | None:
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT spec_id, revision, tenant_id, connection_id, status, spec_json
                FROM declarative_spec_revision
                WHERE spec_id=:spec_id AND revision=:revision
                  AND tenant_id=:tenant_id AND connection_id=:connection_id
                LIMIT 1
            """),
            {"spec_id": spec_id, "revision": revision, "tenant_id": tenant_id, "connection_id": connection_id},
        ).fetchone()
    return None if row is None else _declarative_revision_from_row(row)


def publish_declarative_revision(
    spec_id: str, revision: int, tenant_id: str, connection_id: str
) -> Any | None:
    """Validate and publish an immutable tenant-owned draft in one transaction."""
    with _engine().begin() as conn:
        row = conn.execute(
            text("""
                SELECT spec_id, revision, tenant_id, connection_id, status, spec_json
                FROM declarative_spec_revision
                WHERE spec_id=:spec_id AND revision=:revision
                  AND tenant_id=:tenant_id AND connection_id=:connection_id
                LIMIT 1
            """),
            {"spec_id": spec_id, "revision": revision, "tenant_id": tenant_id, "connection_id": connection_id},
        ).fetchone()
        if row is None:
            return None
        draft = _declarative_revision_from_row(row)
        if draft.status not in {"draft", "published"}:
            raise ValueError("stored declarative revision is invalid")
        conn.execute(
            text("""
                UPDATE declarative_spec_revision SET status='published'
                WHERE spec_id=:spec_id AND revision=:revision
                  AND tenant_id=:tenant_id AND connection_id=:connection_id
            """),
            {"spec_id": spec_id, "revision": revision, "tenant_id": tenant_id, "connection_id": connection_id},
        )
    return replace(draft, status="published")


def activate_declarative_revision(
    spec_id: str, revision: int, tenant_id: str, connection_id: str
) -> ConnectionRecord | None:
    """Activate only a published revision on its exact tenant connection."""
    retired_version: int | None = None
    with _engine().begin() as conn:
        revision_row = conn.execute(
            text("""
                SELECT spec_id, revision, tenant_id, connection_id, status, spec_json
                FROM declarative_spec_revision
                WHERE spec_id=:spec_id AND revision=:revision
                  AND tenant_id=:tenant_id AND connection_id=:connection_id
                  AND status='published' LIMIT 1
            """),
            {"spec_id": spec_id, "revision": revision, "tenant_id": tenant_id, "connection_id": connection_id},
        ).fetchone()
        if revision_row is None:
            return None
        _declarative_revision_from_row(revision_row)
        row = conn.execute(
            text("""
                SELECT connection_id, tenant_id, connector_key, display_name, status,
                       data_mode, public_config_json, config_version
                FROM connection_instance
                WHERE connection_id=:connection_id AND tenant_id=:tenant_id LIMIT 1
            """),
            {"connection_id": connection_id, "tenant_id": tenant_id},
        ).fetchone()
        if row is None:
            return None
        current = _connection_from_row(row)
        retired_version = current.config_version
        public_config = dict(current.public_config)
        public_config.update({"spec_id": spec_id, "revision": revision})
        conn.execute(
            text("""
                UPDATE connection_instance SET public_config_json=:public_config_json,
                    status='active', config_version=config_version+1
                WHERE connection_id=:connection_id AND tenant_id=:tenant_id
            """),
            {"public_config_json": json.dumps(public_config, ensure_ascii=False, separators=(",", ":")), "connection_id": connection_id, "tenant_id": tenant_id},
        )
    updated = replace(current, public_config=public_config, status="active", config_version=current.config_version + 1)
    _notify_connection_cache_invalidator(connection_id, retired_version)
    return updated


def set_tool_policy(
    connection_id: str,
    tool_name: str,
    enabled: bool,
    policy: Mapping[str, Any] | None = None,
) -> ToolPolicy:
    if not connection_id or not tool_name:
        raise ValueError("connection_id and tool_name are required")
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a bool")
    if policy is not None and not isinstance(policy, Mapping):
        raise TypeError("policy must be a mapping")
    record = ToolPolicy(
        connection_id=connection_id,
        tool_name=tool_name,
        enabled=enabled,
        policy=dict(policy or {}),
    )
    retired_version: int | None
    with _engine().begin() as conn:
        retired_version = _retired_connection_version(conn, connection_id)
        conn.execute(
            text("""
                INSERT INTO connection_tool_policy
                    (connection_id, tool_name, enabled, policy_json)
                VALUES (:connection_id, :tool_name, :enabled, :policy_json)
                ON DUPLICATE KEY UPDATE
                    enabled=VALUES(enabled),
                    policy_json=VALUES(policy_json)
            """),
            {
                "connection_id": record.connection_id,
                "tool_name": record.tool_name,
                "enabled": 1 if record.enabled else 0,
                "policy_json": json.dumps(
                    record.policy, ensure_ascii=False, separators=(",", ":")
                ),
            },
        )
    _notify_connection_cache_invalidator(connection_id, retired_version)
    return record


def issue_token(
    connection_id: str,
    raw_value: str | None = None,
    *,
    label: str = "",
) -> IssuedToken:
    if not connection_id:
        raise ValueError("connection_id is required")
    if raw_value is None:
        token_value = f"mcp_{secrets.token_urlsafe(32)}"
    elif not isinstance(raw_value, str) or not raw_value:
        raise ValueError("raw_value is required")
    else:
        token_value = raw_value
    digest = token_hmac(token_value)
    issued = IssuedToken(
        token_id=str(uuid.uuid4()),
        raw_value=token_value,
        prefix=digest[:12],
    )
    retired_version: int | None
    with _engine().begin() as conn:
        retired_version = _retired_connection_version(conn, connection_id)
        conn.execute(
            text("""
                INSERT INTO connection_token
                    (token_id, connection_id, token_hmac, token_prefix, token_label)
                VALUES (:token_id, :connection_id, :token_hmac, :token_prefix, :token_label)
                ON DUPLICATE KEY UPDATE
                    token_prefix=VALUES(token_prefix), token_label=VALUES(token_label)
            """),
            {
                "token_id": issued.token_id,
                "connection_id": connection_id,
                "token_hmac": digest,
                "token_prefix": issued.prefix,
                "token_label": label,
            },
        )
    _notify_connection_cache_invalidator(connection_id, retired_version)
    return issued


def create_connection_with_token(
    record: ConnectionRecord,
    credentials: Mapping[str, str] | None = None,
) -> tuple[ConnectionRecord, IssuedToken]:
    """Atomically create one connection, its credentials, and initial token."""
    if not isinstance(record, ConnectionRecord):
        raise TypeError("record must be a ConnectionRecord")
    raw_value = f"mcp_{secrets.token_urlsafe(32)}"
    digest = token_hmac(raw_value)
    issued = IssuedToken(str(uuid.uuid4()), raw_value, digest[:12])
    with _engine().begin() as conn:
        conn.execute(
            text("""
                INSERT INTO connection_instance
                    (connection_id, tenant_id, connector_key, display_name, status,
                     data_mode, public_config_json, config_version)
                VALUES (:connection_id, :tenant_id, :connector_key, :display_name,
                        :status, :data_mode, :public_config_json, :config_version)
            """),
            {
                "connection_id": record.connection_id,
                "tenant_id": record.tenant_id,
                "connector_key": record.connector_key,
                "display_name": record.display_name,
                "status": record.status,
                "data_mode": record.data_mode,
                "public_config_json": json.dumps(record.public_config, ensure_ascii=False, separators=(",", ":")),
                "config_version": record.config_version,
            },
        )
        for credential_key, plaintext in (credentials or {}).items():
            if not credential_key or not isinstance(plaintext, str):
                raise ValueError("invalid credential")
            conn.execute(
                text("""
                    INSERT INTO connection_credential
                        (connection_id, credential_key, encrypted_value, metadata_json)
                    VALUES (:connection_id, :credential_key, :encrypted_value, :metadata_json)
                """),
                {
                    "connection_id": record.connection_id,
                    "credential_key": credential_key,
                    "encrypted_value": encrypt_credential(plaintext),
                    "metadata_json": '{"source":"admin"}',
                },
            )
        conn.execute(
            text("""
                INSERT INTO connection_token
                    (token_id, connection_id, token_hmac, token_prefix)
                VALUES (:token_id, :connection_id, :token_hmac, :token_prefix)
            """),
            {
                "token_id": issued.token_id,
                "connection_id": record.connection_id,
                "token_hmac": digest,
                "token_prefix": issued.prefix,
            },
        )
    _notify_connection_cache_invalidator(record.connection_id, record.config_version)
    return record, issued


def update_connection(
    connection_id: str,
    tenant_id: str,
    *,
    display_name: str,
    data_mode: str,
    public_config: Mapping[str, Any],
    status: str | None = None,
) -> ConnectionRecord | None:
    """Update a tenant-owned connection and retire only its committed cache key."""
    retired_version: int | None = None
    with _engine().begin() as conn:
        row = conn.execute(
            text("""
                SELECT connection_id, tenant_id, connector_key, display_name, status,
                       data_mode, public_config_json, config_version
                FROM connection_instance
                WHERE connection_id=:connection_id AND tenant_id=:tenant_id LIMIT 1
            """),
            {"connection_id": connection_id, "tenant_id": tenant_id},
        ).fetchone()
        if row is None:
            return None
        current = _connection_from_row(row)
        retired_version = current.config_version
        next_status = current.status if status is None else status
        conn.execute(
            text("""
                UPDATE connection_instance SET display_name=:display_name,
                    data_mode=:data_mode, public_config_json=:public_config_json,
                    status=:status, config_version=config_version+1
                WHERE connection_id=:connection_id AND tenant_id=:tenant_id
            """),
            {
                "connection_id": connection_id,
                "tenant_id": tenant_id,
                "display_name": display_name,
                "data_mode": data_mode,
                "public_config_json": json.dumps(dict(public_config), ensure_ascii=False, separators=(",", ":")),
                "status": next_status,
            },
        )
    updated = ConnectionRecord(
        connection_id=connection_id,
        tenant_id=tenant_id,
        connector_key=current.connector_key,
        display_name=display_name,
        status=next_status,
        data_mode=data_mode,  # type: ignore[arg-type]
        public_config=dict(public_config),
        config_version=current.config_version + 1,
    )
    _notify_connection_cache_invalidator(connection_id, retired_version)
    return updated


def disable_connection(connection_id: str, tenant_id: str) -> ConnectionRecord | None:
    current = get_connection(connection_id, tenant_id)
    if current is None:
        return None
    return update_connection(
        connection_id,
        tenant_id,
        display_name=current.display_name,
        data_mode=current.data_mode,
        public_config=current.public_config,
        status="disabled",
    )


def replace_credentials(
    connection_id: str, tenant_id: str, credentials: Mapping[str, str]
) -> bool:
    retired_version: int | None = None
    with _engine().begin() as conn:
        retired_version = _retired_connection_version(conn, connection_id)
        owner = conn.execute(
            text("SELECT tenant_id FROM connection_instance WHERE connection_id=:connection_id LIMIT 1"),
            {"connection_id": connection_id},
        ).scalar()
        if owner != tenant_id:
            return False
        conn.execute(text("DELETE FROM connection_credential WHERE connection_id=:connection_id"), {"connection_id": connection_id})
        for key, value in credentials.items():
            conn.execute(
                text("""
                    INSERT INTO connection_credential
                        (connection_id, credential_key, encrypted_value, metadata_json)
                    VALUES (:connection_id, :credential_key, :encrypted_value, :metadata_json)
                """),
                {"connection_id": connection_id, "credential_key": key, "encrypted_value": encrypt_credential(value), "metadata_json": '{"source":"admin"}'},
            )
        conn.execute(text("UPDATE connection_instance SET config_version=config_version+1 WHERE connection_id=:connection_id"), {"connection_id": connection_id})
    _notify_connection_cache_invalidator(connection_id, retired_version)
    return True


def list_connection_tokens(connection_id: str) -> list[dict[str, Any]]:
    """Return non-sensitive token metadata; the digest is intentionally not selected."""
    with _engine().connect() as conn:
        rows = conn.execute(
            text("""
                SELECT token_id, token_prefix, token_label, expires_at, revoked_at, created_at
                FROM connection_token WHERE connection_id=:connection_id
                ORDER BY created_at DESC, token_id
            """),
            {"connection_id": connection_id},
        ).fetchall()
    result = []
    for row in rows:
        values = _row_values(row)
        result.append({
            "token_id": values["token_id"],
            "prefix": values.get("token_prefix", ""),
            "label": values.get("token_label", "") or "",
            "expires_at": str(values["expires_at"]) if values.get("expires_at") else None,
            "revoked": values.get("revoked_at") is not None,
            "created_at": str(values["created_at"]) if values.get("created_at") else None,
        })
    return result


def revoke_token(connection_id: str, tenant_id: str, token_id: str) -> bool:
    retired_version: int | None = None
    with _engine().begin() as conn:
        retired_version = _retired_connection_version(conn, connection_id)
        result = conn.execute(
            text("""
                UPDATE connection_token t JOIN connection_instance c
                  ON c.connection_id=t.connection_id
                SET t.revoked_at=UTC_TIMESTAMP()
                WHERE t.connection_id=:connection_id AND t.token_id=:token_id
                  AND c.tenant_id=:tenant_id AND t.revoked_at IS NULL
            """),
            {"connection_id": connection_id, "token_id": token_id, "tenant_id": tenant_id},
        )
    if result.rowcount:
        _notify_connection_cache_invalidator(connection_id, retired_version)
        return True
    return False


def rotate_token(
    connection_id: str, tenant_id: str, *, label: str = ""
) -> IssuedToken | None:
    """Atomically revoke current tokens and issue one replacement."""
    raw_value = f"mcp_{secrets.token_urlsafe(32)}"
    digest = token_hmac(raw_value)
    issued = IssuedToken(str(uuid.uuid4()), raw_value, digest[:12])
    retired_version: int | None = None
    with _engine().begin() as conn:
        retired_version = _retired_connection_version(conn, connection_id)
        owner = conn.execute(
            text("SELECT tenant_id FROM connection_instance WHERE connection_id=:connection_id LIMIT 1"),
            {"connection_id": connection_id},
        ).scalar()
        if owner != tenant_id:
            return None
        conn.execute(
            text("UPDATE connection_token SET revoked_at=UTC_TIMESTAMP() WHERE connection_id=:connection_id AND revoked_at IS NULL"),
            {"connection_id": connection_id},
        )
        conn.execute(
            text("""
                INSERT INTO connection_token
                    (token_id, connection_id, token_hmac, token_prefix, token_label)
                VALUES (:token_id, :connection_id, :token_hmac, :token_prefix, :token_label)
            """),
            {"token_id": issued.token_id, "connection_id": connection_id, "token_hmac": digest, "token_prefix": issued.prefix, "token_label": label},
        )
    _notify_connection_cache_invalidator(connection_id, retired_version)
    return issued


def list_tool_policies(connection_id: str) -> list[ToolPolicy]:
    with _engine().connect() as conn:
        rows = conn.execute(
            text("SELECT connection_id, tool_name, enabled, policy_json FROM connection_tool_policy WHERE connection_id=:connection_id"),
            {"connection_id": connection_id},
        ).fetchall()
    return [ToolPolicy(connection_id, _row_values(row)["tool_name"], bool(_row_values(row)["enabled"]), json.loads(_row_values(row)["policy_json"] or "{}")) for row in rows]


def replace_tool_policies(
    connection_id: str, tenant_id: str, policies: list[ToolPolicy]
) -> bool:
    retired_version: int | None = None
    with _engine().begin() as conn:
        retired_version = _retired_connection_version(conn, connection_id)
        owner = conn.execute(text("SELECT tenant_id FROM connection_instance WHERE connection_id=:connection_id LIMIT 1"), {"connection_id": connection_id}).scalar()
        if owner != tenant_id:
            return False
        conn.execute(text("DELETE FROM connection_tool_policy WHERE connection_id=:connection_id"), {"connection_id": connection_id})
        for policy in policies:
            conn.execute(
                text("INSERT INTO connection_tool_policy (connection_id, tool_name, enabled, policy_json) VALUES (:connection_id,:tool_name,:enabled,:policy_json)"),
                {"connection_id": connection_id, "tool_name": policy.tool_name, "enabled": 1 if policy.enabled else 0, "policy_json": json.dumps(policy.policy, ensure_ascii=False, separators=(",", ":"))},
            )
        conn.execute(text("UPDATE connection_instance SET config_version=config_version+1 WHERE connection_id=:connection_id"), {"connection_id": connection_id})
    _notify_connection_cache_invalidator(connection_id, retired_version)
    return True


def resolve_connection_token(
    raw_token: str, connection_id: str
) -> ConnectionRecord | None:
    statement = text("""
        SELECT c.connection_id, c.tenant_id, c.connector_key, c.display_name,
               c.status, c.data_mode, c.public_config_json, c.config_version
        FROM connection_token t
        JOIN connection_instance c ON c.connection_id=t.connection_id
        WHERE t.connection_id=:connection_id AND t.token_hmac=:token_hmac
          AND t.revoked_at IS NULL
          AND (t.expires_at IS NULL OR t.expires_at > UTC_TIMESTAMP())
          AND c.status='active'
        LIMIT 1
    """)
    with _engine().connect() as conn:
        row = conn.execute(
            statement,
            {"connection_id": connection_id, "token_hmac": token_hmac(raw_token)},
        ).fetchone()
    return _connection_from_row(row) if row is not None else None


def _row_values(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row)


def _legacy_connection_id(tenant_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"wbsysc:legacy-wecom:{tenant_id}"))


def _legacy_backfill_completed(value: Any) -> bool:
    if not value:
        return False
    try:
        state = json.loads(value)
    except (TypeError, ValueError):
        return False
    return isinstance(state, dict) and state.get("status") == _LEGACY_WATERMARK_STATUS


def _legacy_public_config(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "corpid": values.get("corpid") or "",
        "schema_name": values.get("schema_name") or "",
        "sync_interval_min": int(values.get("sync_interval_min") or 30),
        "enabled_modules": values.get("enabled_modules") or "",
        "checkin_userids": values.get("checkin_userids") or "",
        "trusted_domain": values.get("trusted_domain") or "",
        "legacy_source": "tenant_config",
    }


def _insert_legacy_credential(
    conn: Any,
    connection_id: str,
    credential_key: str,
    encrypted_value: Any,
    legacy_column: str,
) -> None:
    if encrypted_value is None:
        return
    conn.execute(
        text("""
            INSERT INTO connection_credential
                (connection_id, credential_key, encrypted_value, metadata_json)
            VALUES (:connection_id, :credential_key, :encrypted_value, :metadata_json)
            ON DUPLICATE KEY UPDATE
                encrypted_value=VALUES(encrypted_value),
                metadata_json=VALUES(metadata_json)
        """),
        {
            "connection_id": connection_id,
            "credential_key": credential_key,
            "encrypted_value": bytes(encrypted_value),
            "metadata_json": json.dumps(
                {"legacy_column": legacy_column, "source": "tenant_config"},
                separators=(",", ":"),
            ),
        },
    )


def _token_owner_connection_id(conn: Any, digest: str) -> str | None:
    return conn.execute(
        text("""
            SELECT connection_id FROM connection_token
            WHERE token_hmac=:token_hmac
            LIMIT 1
        """),
        {"token_hmac": digest},
    ).scalar()


def _backfill_legacy_tenant(values: Mapping[str, Any]) -> bool:
    tenant_id = values.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        logger.warning("Skipping legacy connection row without a tenant_id")
        return False
    connection_id = _legacy_connection_id(tenant_id)
    with _engine().connect() as conn:
        marker = conn.execute(
            text("""
                SELECT state_json FROM connection_sync_state
                WHERE connection_id=:connection_id AND state_key=:state_key
            """),
            {"connection_id": connection_id, "state_key": _LEGACY_WATERMARK_KEY},
        ).scalar()
    if _legacy_backfill_completed(marker):
        return False

    enabled = bool(values.get("enabled"))
    data_mode = values.get("data_mode")
    if data_mode not in {"direct", "stored", "hybrid"}:
        data_mode = "stored"
    display_name = values.get("display_name") or f"WeCom ({tenant_id})"
    try:
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO connection_instance
                        (connection_id, tenant_id, connector_key, display_name, status,
                         data_mode, public_config_json, config_version)
                    VALUES (:connection_id, :tenant_id, 'wecom', :display_name, :status,
                            :data_mode, :public_config_json, 1)
                    ON DUPLICATE KEY UPDATE
                        display_name=VALUES(display_name),
                        status=VALUES(status),
                        data_mode=VALUES(data_mode),
                        public_config_json=VALUES(public_config_json),
                        config_version=VALUES(config_version)
                """),
                {
                    "connection_id": connection_id,
                    "tenant_id": tenant_id,
                    "display_name": display_name,
                    "status": "active" if enabled else "disabled",
                    "data_mode": data_mode,
                    "public_config_json": json.dumps(
                        _legacy_public_config(values),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            )
            _insert_legacy_credential(
                conn,
                connection_id,
                "wecom_app_secret",
                values.get("secret_encrypted"),
                "secret_encrypted",
            )
            _insert_legacy_credential(
                conn,
                connection_id,
                "wecom_contact_secret",
                values.get("contact_secret_encrypted"),
                "contact_secret_encrypted",
            )
            raw_token = values.get("mcp_token")
            if raw_token:
                digest = token_hmac(raw_token)
                existing_connection_id = _token_owner_connection_id(conn, digest)
                if (
                    existing_connection_id is not None
                    and existing_connection_id != connection_id
                ):
                    raise RuntimeError(
                        "legacy token digest is already assigned to another connection"
                    )
                conn.execute(
                    text("""
                        INSERT INTO connection_token
                            (token_id, connection_id, token_hmac, token_prefix, token_label)
                        VALUES (:token_id, :connection_id, :token_hmac, :token_prefix,
                                :token_label)
                        ON DUPLICATE KEY UPDATE
                            token_prefix=VALUES(token_prefix),
                            token_label=VALUES(token_label)
                    """),
                    {
                        "token_id": str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"wbsysc:legacy-wecom-token:{tenant_id}",
                            )
                        ),
                        "connection_id": connection_id,
                        "token_hmac": digest,
                        "token_prefix": digest[:12],
                        "token_label": "legacy tenant_config token",
                    },
                )
                if _token_owner_connection_id(conn, digest) != connection_id:
                    raise RuntimeError(
                        "legacy token digest could not be assigned to its connection"
                    )
        # This separate transaction is deliberate: a completion marker is not
        # attempted until all copied connection data has committed successfully.
        with _engine().begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO connection_sync_state
                        (connection_id, state_key, state_json, last_success_at, last_error)
                    VALUES (:connection_id, :state_key, :state_json, UTC_TIMESTAMP(), '')
                    ON DUPLICATE KEY UPDATE
                        state_json=VALUES(state_json),
                        last_success_at=VALUES(last_success_at),
                        last_error=''
                """),
                {
                    "connection_id": connection_id,
                    "state_key": _LEGACY_WATERMARK_KEY,
                    "state_json": json.dumps(
                        {"status": _LEGACY_WATERMARK_STATUS, "source": "tenant_config"},
                        separators=(",", ":"),
                    ),
                },
            )
    except Exception:
        logger.exception("Legacy WeCom connection migration failed tenant_id=%s", tenant_id)
        return False
    return True


def migrate_legacy_wecom_connections() -> int:
    """Backfill deterministic default WeCom connections from legacy tenant rows.

    The source tables are never changed or deleted.  Each tenant receives an
    independent completion watermark only after its copy transaction commits,
    so an interrupted startup can safely retry the remaining tenants.
    """
    statement = text("""
        SELECT tenant_id, display_name, corpid, secret_encrypted, mcp_token,
               schema_name, sync_interval_min, enabled_modules, checkin_userids,
               contact_secret_encrypted, trusted_domain, data_mode, enabled
        FROM tenant_config
    """)
    with _engine().connect() as conn:
        rows = conn.execute(statement).mappings().all()
    return sum(1 for row in rows if _backfill_legacy_tenant(_row_values(row)))
