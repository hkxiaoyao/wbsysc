"""
数据访问层 - MySQL 落库 + 多租户分 schema 隔离
- 每租户独立 schema: wbd_{corpid_hash}
- SQL 表名用 schema 前缀（如 {schema}.wecom_report），避免连接级 USE 的并发竞态
- 中心表 tenant_config 在主库 websysc（无前缀）
- 幂等 UPSERT，兼容 MySQL 5.7（JSON 列）

设计决策：用 schema 前缀表名而非连接级 USE，因 SQLAlchemy 连接池复用，
并发请求 SET/USE 会互相污染；前缀表名让每条 SQL 自带定位，最稳。
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from .config import get_settings

_engine: Optional[Engine] = None
_lock_engine: Optional[Engine] = None

# 各租户 schema 的业务表名（所有 schema 结构相同）
BIZ_TABLES = ["wecom_report", "wecom_approval", "wecom_checkin", "sync_cursor", "audit_log"]

_TENANT_COLUMN_DEFINITIONS = {
    "enabled_modules": "VARCHAR(64) NOT NULL DEFAULT 'report,approval,checkin'",
    "checkin_userids": "TEXT NULL",
    "contact_secret_encrypted": "VARBINARY(512) NULL",
    "trusted_domain": "VARCHAR(255) NOT NULL DEFAULT ''",
    "data_mode": "VARCHAR(16) NOT NULL DEFAULT 'stored'",
}

_BUSINESS_COLUMN_DEFINITIONS = {
    "wecom_report": {
        "source_window_start": "BIGINT NOT NULL DEFAULT 0",
        "source_window_end": "BIGINT NOT NULL DEFAULT 0",
        "is_partial": "TINYINT NOT NULL DEFAULT 0",
    },
    "wecom_approval": {
        "source_window_start": "BIGINT NOT NULL DEFAULT 0",
        "source_window_end": "BIGINT NOT NULL DEFAULT 0",
        "is_partial": "TINYINT NOT NULL DEFAULT 0",
    },
}


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_engine(
            s.db_url,
            pool_size=s.db_pool_size,
            pool_recycle=3600,
            pool_pre_ping=True,
            json_serializer=lambda o: json.dumps(o, ensure_ascii=False, default=str),
        )
    return _engine


def get_lock_engine() -> Engine:
    """返回不复用 DBAPI 连接的独立 advisory-lock 引擎。"""
    global _lock_engine
    if _lock_engine is None:
        s = get_settings()
        _lock_engine = create_engine(
            s.db_url,
            poolclass=NullPool,
            pool_pre_ping=True,
        )
    return _lock_engine


def _q(schema: str, table: str) -> str:
    """生成带 schema 前缀的限定表名（防 SQL 注入：白名单校验）"""
    assert table in BIZ_TABLES, f"非法表名: {table}"
    # schema 名是系统生成(wbd_md5hash)，但仍做基本字符约束
    assert schema.replace("_", "").isalnum(), f"非法schema: {schema}"
    return f"`{schema}`.`{table}`"


def _valid_tenant_schema(schema: str) -> bool:
    return (
        isinstance(schema, str)
        and schema.startswith("wbd_")
        and len(schema) <= 64
        and schema.replace("_", "").isalnum()
    )


@contextmanager
def tenant_sync_lock(schema: str, timeout: int = 0) -> Iterator[bool]:
    """用独立物理连接持有租户级同步互斥锁，不占主 QueuePool。"""
    assert schema.replace("_", "").isalnum(), f"非法schema: {schema}"
    digest = hashlib.sha256(schema.encode("utf-8")).hexdigest()[:40]
    lock_name = f"wbsysc:tenant-sync:{digest}"
    with get_lock_engine().connect() as conn:
        acquired = conn.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": lock_name, "timeout": max(0, int(timeout))},
        ).scalar() == 1
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    released = conn.execute(
                        text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name}
                    ).scalar()
                except Exception:
                    try:
                        conn.invalidate()
                    finally:
                        conn.close()
                    raise
                if released != 1:
                    try:
                        conn.invalidate()
                    finally:
                        conn.close()
                    raise RuntimeError(f"failed to release tenant sync lock: {schema}")


def ensure_central_columns() -> None:
    """以 MySQL 5.7 兼容方式补齐 tenant_config 运行期所需列。"""
    with get_engine().begin() as conn:
        for column, definition in _TENANT_COLUMN_DEFINITIONS.items():
            exists = conn.execute(
                text("""SELECT COUNT(*) FROM information_schema.COLUMNS
                        WHERE TABLE_SCHEMA=DATABASE()
                          AND TABLE_NAME='tenant_config'
                          AND COLUMN_NAME=:column"""),
                {"column": column},
            ).scalar()
            if not exists:
                conn.execute(text(
                    f"ALTER TABLE tenant_config ADD COLUMN `{column}` {definition}"
                ))


def get_tenant_schema_names() -> List[str]:
    """中心列迁移完成后枚举合法租户 schema。"""
    sql = text("""SELECT DISTINCT schema_name FROM tenant_config
                  WHERE schema_name REGEXP '^wbd_[0-9A-Za-z_]+$'""")
    with get_engine().connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [row[0] for row in rows if _valid_tenant_schema(row[0])]


def ensure_schema(schema_name: str) -> None:
    """创建或修复租户 schema 的完整业务表结构。"""
    if not _valid_tenant_schema(schema_name):
        raise ValueError(f"非法租户schema: {schema_name}")
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS `{schema_name}`"))
        for ddl in _BIZ_DDLS:
            conn.execute(text(ddl.format(schema=schema_name)))
        for table, columns in _BUSINESS_COLUMN_DEFINITIONS.items():
            for column, definition in columns.items():
                exists = conn.execute(
                    text("""SELECT COUNT(*) FROM information_schema.COLUMNS
                            WHERE TABLE_SCHEMA=:schema
                              AND TABLE_NAME=:table
                              AND COLUMN_NAME=:column"""),
                    {"schema": schema_name, "table": table, "column": column},
                ).scalar()
                if not exists:
                    conn.execute(text(
                        f"ALTER TABLE `{schema_name}`.`{table}` "
                        f"ADD COLUMN `{column}` {definition}"
                    ))
        conn.execute(text(f"""INSERT IGNORE INTO `{schema_name}`.`sync_cursor`
            (tenant_id, data_source, filter_key, last_value) VALUES
            ('{schema_name}','report','',''),
            ('{schema_name}','approval','',''),
            ('{schema_name}','checkin','','')"""))


def run_startup_migrations() -> None:
    """启动时先升级中心表，再逐租户创建或修复业务 schema。"""
    ensure_central_columns()
    from .mcp_log_store import ensure_central_log_tables, migrate_legacy_logs
    from .connections import store as connection_store

    ensure_central_log_tables()
    connection_store.ensure_connection_tables()
    connection_store.migrate_legacy_wecom_connections()
    for schema_name in get_tenant_schema_names():
        ensure_schema(schema_name)
    migrate_legacy_logs(days=90)


# ----- 汇报落库 -----
def upsert_report(
    schema: str,
    journaluuid: str,
    info: Dict[str, Any],
    source_window: tuple[int, int] | None = None,
) -> None:
    window_start, window_end = source_window or (0, 0)
    partial = 1 if info.get("_partial") else 0
    submitter = info.get("submitter") or {}
    if isinstance(submitter, str):
        submitter_uid = submitter
    elif isinstance(submitter, dict):
        submitter_uid = submitter.get("userid", "") or ""
    else:
        submitter_uid = ""
    sql = text(f"""
        INSERT INTO {_q(schema,'wecom_report')}
            (tenant_id, journaluuid, template_id, template_name,
             report_time, submitter_userid, detail_json,
             source_window_start, source_window_end, is_partial)
        VALUES (:t,:j,:tid,:tname,:rt,:su,:dj,:ws,:we,:partial)
        ON DUPLICATE KEY UPDATE
            template_id=IF(is_partial=0 AND VALUES(is_partial)=1,
                           template_id, VALUES(template_id)),
            template_name=IF(is_partial=0 AND VALUES(is_partial)=1,
                             template_name, VALUES(template_name)),
            report_time=IF(is_partial=0 AND VALUES(is_partial)=1,
                           report_time, VALUES(report_time)),
            submitter_userid=IF(is_partial=0 AND VALUES(is_partial)=1,
                                submitter_userid, VALUES(submitter_userid)),
            detail_json=IF(is_partial=0 AND VALUES(is_partial)=1,
                           detail_json, VALUES(detail_json)),
            source_window_start=IF(is_partial=0 AND VALUES(is_partial)=1,
                                   source_window_start, VALUES(source_window_start)),
            source_window_end=IF(is_partial=0 AND VALUES(is_partial)=1,
                                 source_window_end, VALUES(source_window_end)),
            synced_at=IF(is_partial=0 AND VALUES(is_partial)=1, synced_at, NOW()),
            is_partial=IF(is_partial=0 AND VALUES(is_partial)=1,
                          is_partial, VALUES(is_partial))
    """)
    with get_engine().begin() as conn:
        conn.execute(sql, {
            "t": schema, "j": journaluuid,
            "tid": info.get("template_id", "") or "",
            "tname": info.get("template_name", "") or "",
            "rt": int(info.get("report_time", 0) or 0),
            "su": submitter_uid,
            "dj": json.dumps(info, ensure_ascii=False, default=str),
            "ws": window_start,
            "we": window_end,
            "partial": partial,
        })


def query_reports_by_window(schema: str, starttime: int, endtime: int, limit: int = 100) -> List[Dict]:
    sql = text(f"""SELECT journaluuid, template_id, template_name, report_time,
                          submitter_userid, detail_json, is_partial
                   FROM {_q(schema,'wecom_report')}
                   WHERE (
                     is_partial=0 AND report_time>=:s AND report_time<:e
                   ) OR (
                     is_partial=1 AND source_window_start<:e AND source_window_end>:s
                   )
                   ORDER BY CASE WHEN is_partial=1
                                 THEN source_window_end ELSE report_time END DESC
                   LIMIT :n""")
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"s": starttime, "e": endtime, "n": limit})
        return [dict(r._mapping) for r in rows]


def get_report_detail(schema: str, journaluuid: str) -> Optional[Dict]:
    sql = text(f"SELECT detail_json FROM {_q(schema,'wecom_report')} WHERE journaluuid=:j LIMIT 1")
    with get_engine().connect() as conn:
        r = conn.execute(sql, {"j": journaluuid}).fetchone()
        return json.loads(r[0]) if r and r[0] else None


# ----- 审批落库 -----
def upsert_approval(
    schema: str,
    sp_no: str,
    info: Dict[str, Any],
    source_window: tuple[int, int] | None = None,
) -> None:
    window_start, window_end = source_window or (0, 0)
    partial = 1 if info.get("_partial") else 0
    sql = text(f"""
        INSERT INTO {_q(schema,'wecom_approval')}
            (tenant_id, sp_no, sp_name, sp_status, template_id,
             apply_time, applyer_userid, detail_json,
             source_window_start, source_window_end, is_partial)
        VALUES (:t,:sp,:sn,:ss,:tid,:at,:au,:dj,:ws,:we,:partial)
        ON DUPLICATE KEY UPDATE
            sp_name=IF(is_partial=0 AND VALUES(is_partial)=1,
                       sp_name, VALUES(sp_name)),
            sp_status=IF(is_partial=0 AND VALUES(is_partial)=1,
                         sp_status, VALUES(sp_status)),
            template_id=IF(is_partial=0 AND VALUES(is_partial)=1,
                           template_id, VALUES(template_id)),
            apply_time=IF(is_partial=0 AND VALUES(is_partial)=1,
                          apply_time, VALUES(apply_time)),
            applyer_userid=IF(is_partial=0 AND VALUES(is_partial)=1,
                              applyer_userid, VALUES(applyer_userid)),
            detail_json=IF(is_partial=0 AND VALUES(is_partial)=1,
                           detail_json, VALUES(detail_json)),
            source_window_start=IF(is_partial=0 AND VALUES(is_partial)=1,
                                   source_window_start, VALUES(source_window_start)),
            source_window_end=IF(is_partial=0 AND VALUES(is_partial)=1,
                                 source_window_end, VALUES(source_window_end)),
            synced_at=IF(is_partial=0 AND VALUES(is_partial)=1, synced_at, NOW()),
            is_partial=IF(is_partial=0 AND VALUES(is_partial)=1,
                          is_partial, VALUES(is_partial))
    """)
    with get_engine().begin() as conn:
        conn.execute(sql, {
            "t": schema, "sp": sp_no, "sn": info.get("sp_name", ""),
            "ss": int(info.get("sp_status", 0) or 0), "tid": info.get("template_id", ""),
            "at": int(info.get("apply_time", 0) or 0),
            "au": (info.get("applyer") or {}).get("userid", ""),
            "dj": json.dumps(info, ensure_ascii=False, default=str),
            "ws": window_start,
            "we": window_end,
            "partial": partial,
        })


def query_approvals_by_window(schema: str, starttime: int, endtime: int, limit: int = 100) -> List[Dict]:
    sql = text(f"""SELECT sp_no, sp_name, sp_status, template_id,
                          apply_time, applyer_userid, detail_json, is_partial
                   FROM {_q(schema,'wecom_approval')}
                   WHERE (
                     is_partial=0 AND apply_time>=:s AND apply_time<:e
                   ) OR (
                     is_partial=1 AND source_window_start<:e AND source_window_end>:s
                   )
                   ORDER BY CASE WHEN is_partial=1
                                 THEN source_window_end ELSE apply_time END DESC
                   LIMIT :n""")
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"s": starttime, "e": endtime, "n": limit})
        return [dict(r._mapping) for r in rows]


def get_approval_detail(schema: str, sp_no: str) -> Optional[Dict]:
    sql = text(f"SELECT detail_json FROM {_q(schema,'wecom_approval')} WHERE sp_no=:s LIMIT 1")
    with get_engine().connect() as conn:
        r = conn.execute(sql, {"s": sp_no}).fetchone()
        return json.loads(r[0]) if r and r[0] else None


# ----- 打卡落库（一次拿全字段，无需二次拉详情）-----
def upsert_checkin(schema: str, rec: Dict[str, Any]) -> None:
    """幂等写入打卡记录。幂等键: userid+checkin_time+checkin_type"""
    sql = text(f"""
        INSERT INTO {_q(schema,'wecom_checkin')}
            (tenant_id, userid, checkin_type, checkin_time, exception_type,
             location_title, group_name, detail_json)
        VALUES (:t,:uid,:ct,:rt,:et,:lt,:gn,:dj)
        ON DUPLICATE KEY UPDATE
            exception_type=VALUES(exception_type), location_title=VALUES(location_title),
            group_name=VALUES(group_name), detail_json=VALUES(detail_json), synced_at=NOW()
    """)
    with get_engine().begin() as conn:
        conn.execute(sql, {
            "t": schema,
            "uid": rec.get("userid", ""),
            "ct": rec.get("checkin_type", ""),
            "rt": int(rec.get("checkin_time", 0) or 0),
            "et": rec.get("exception_type", ""),
            "lt": rec.get("location_title", ""),
            "gn": rec.get("groupname", ""),
            "dj": json.dumps(rec, ensure_ascii=False, default=str),
        })


def query_checkins_by_window(schema: str, starttime: int, endtime: int, limit: int = 100) -> List[Dict]:
    sql = text(f"""SELECT userid, checkin_type, checkin_time, exception_type,
                          location_title, group_name
                   FROM {_q(schema,'wecom_checkin')}
                   WHERE checkin_time>=:s AND checkin_time<:e
                   ORDER BY checkin_time DESC LIMIT :n""")
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"s": starttime, "e": endtime, "n": limit})
        return [dict(r._mapping) for r in rows]


# ----- 游标 + 审计（per schema）-----
def get_cursor(schema: str, data_source: str, filter_key: str = "") -> str:
    sql = text(f"SELECT last_value FROM {_q(schema,'sync_cursor')} WHERE data_source=:d AND filter_key=:f")
    with get_engine().connect() as conn:
        r = conn.execute(sql, {"d": data_source, "f": filter_key}).fetchone()
        return r[0] if r else ""


def save_cursor(schema: str, data_source: str, filter_key: str, last_value: str) -> None:
    sql = text(f"""INSERT INTO {_q(schema,'sync_cursor')}
                   (tenant_id, data_source, filter_key, last_value)
                   VALUES (:t,:d,:f,:v)
                   ON DUPLICATE KEY UPDATE last_value=VALUES(last_value)""")
    with get_engine().begin() as conn:
        conn.execute(sql, {"t": schema, "d": data_source, "f": filter_key, "v": last_value})


def log_audit(schema: str, tool_name: str, target: str, params: str, status: str, cost_ms: int) -> None:
    sql = text(f"""INSERT INTO {_q(schema,'audit_log')}
                   (tenant_id, tool_name, target, params_summary, result_status, cost_ms)
                   VALUES (:t,:tn,:tg,:p,:rs,:c)""")
    try:
        with get_engine().begin() as conn:
            conn.execute(sql, {"t": schema, "tn": tool_name, "tg": target,
                               "p": params[:500], "rs": status, "c": cost_ms})
    except Exception:
        pass


# ===== 各租户 schema 业务表 DDL（与 001_init.sql 一致，schema 占位）=====
# {schema} 占位由 ensure_schema 填入
_BIZ_DDLS = [
"""CREATE TABLE IF NOT EXISTS `{schema}`.`wecom_report` (
  id BIGINT NOT NULL AUTO_INCREMENT, tenant_id VARCHAR(64) NOT NULL,
  journaluuid VARCHAR(128) NOT NULL, template_id VARCHAR(128) NOT NULL DEFAULT '',
  template_name VARCHAR(128) NOT NULL DEFAULT '', report_time BIGINT NOT NULL DEFAULT 0,
  submitter_userid VARCHAR(64) NOT NULL DEFAULT '', detail_json JSON DEFAULT NULL,
  source_window_start BIGINT NOT NULL DEFAULT 0,
  source_window_end BIGINT NOT NULL DEFAULT 0,
  is_partial TINYINT NOT NULL DEFAULT 0,
  synced_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(id), UNIQUE KEY uk_tj(tenant_id,journaluuid),
  KEY idx_tt(tenant_id,report_time), KEY idx_tpl(tenant_id,template_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
"""CREATE TABLE IF NOT EXISTS `{schema}`.`wecom_approval` (
  id BIGINT NOT NULL AUTO_INCREMENT, tenant_id VARCHAR(64) NOT NULL,
  sp_no VARCHAR(64) NOT NULL, sp_name VARCHAR(128) NOT NULL DEFAULT '',
  sp_status INT NOT NULL DEFAULT 0, template_id VARCHAR(128) NOT NULL DEFAULT '',
  apply_time BIGINT NOT NULL DEFAULT 0, applyer_userid VARCHAR(64) NOT NULL DEFAULT '',
  detail_json JSON DEFAULT NULL,
  source_window_start BIGINT NOT NULL DEFAULT 0,
  source_window_end BIGINT NOT NULL DEFAULT 0,
  is_partial TINYINT NOT NULL DEFAULT 0,
  synced_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(id), UNIQUE KEY uk_ts(tenant_id,sp_no),
  KEY idx_tt(tenant_id,apply_time), KEY idx_ts(tenant_id,sp_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
"""CREATE TABLE IF NOT EXISTS `{schema}`.`sync_cursor` (
  tenant_id VARCHAR(64) NOT NULL, data_source VARCHAR(32) NOT NULL,
  filter_key VARCHAR(64) NOT NULL DEFAULT '', last_value VARCHAR(64) NOT NULL DEFAULT '',
  last_sync_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY(tenant_id,data_source,filter_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
"""CREATE TABLE IF NOT EXISTS `{schema}`.`audit_log` (
  id BIGINT NOT NULL AUTO_INCREMENT, tenant_id VARCHAR(64) NOT NULL,
  tool_name VARCHAR(64) NOT NULL, target VARCHAR(256) NOT NULL DEFAULT '',
  params_summary VARCHAR(512) NOT NULL DEFAULT '', result_status VARCHAR(16) NOT NULL DEFAULT '',
  cost_ms INT NOT NULL DEFAULT 0, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(id), KEY idx_tt(tenant_id,created_at), KEY idx_tool(tool_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
"""CREATE TABLE IF NOT EXISTS `{schema}`.`wecom_checkin` (
  id BIGINT NOT NULL AUTO_INCREMENT, tenant_id VARCHAR(64) NOT NULL,
  userid VARCHAR(64) NOT NULL, checkin_type VARCHAR(32) NOT NULL DEFAULT '',
  checkin_time BIGINT NOT NULL DEFAULT 0, exception_type VARCHAR(128) NOT NULL DEFAULT '',
  location_title VARCHAR(256) NOT NULL DEFAULT '', group_name VARCHAR(128) NOT NULL DEFAULT '',
  detail_json JSON DEFAULT NULL, synced_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(id),
  UNIQUE KEY uk_user_time (userid, checkin_time, checkin_type),
  KEY idx_time(checkin_time), KEY idx_user_time(userid, checkin_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]
