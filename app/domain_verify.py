"""
企微可信域名验证文件
- 反代域名接入时，企微要求根路径可访问校验文件（如 /xxxx.txt）
- 文件内容落库，容器重建后仍可访问；每租户仅保留一份，新上传覆盖旧文件
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import text

from .db import get_engine

# 企微校验文件多为纯文本 .txt；顺带允许 html
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(txt|html|htm)$", re.I)
_MAX_BYTES = 64 * 1024

_DDL = """
CREATE TABLE IF NOT EXISTS `domain_verify_file` (
  `filename`       VARCHAR(160) NOT NULL COMMENT '根路径文件名，如 WW_verify_xxx.txt',
  `content`        MEDIUMTEXT   NOT NULL,
  `content_type`   VARCHAR(64)  NOT NULL DEFAULT 'text/plain; charset=utf-8',
  `tenant_id`      VARCHAR(64)  NULL COMMENT '归属租户，可空',
  `trusted_domain` VARCHAR(255) NULL COMMENT '绑定可信域名（展示用）',
  `updated_at`     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`filename`),
  UNIQUE KEY `uk_tenant` (`tenant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='可信域名校验文件(中心库)'
"""

def _column_exists(conn, table: str, column: str) -> bool:
    """兼容 MySQL 5.7/8.0：不用 ADD COLUMN IF NOT EXISTS（5.7 不支持会整句失败）"""
    r = conn.execute(
        text(
            """
            SELECT 1 FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = :t
              AND COLUMN_NAME = :c
            LIMIT 1
            """
        ),
        {"t": table, "c": column},
    ).fetchone()
    return bool(r)


def ensure_domain_tables() -> None:
    """建表 + 兼容旧库补列（幂等，兼容 MySQL 5.7）"""
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text(_DDL))
        if not _column_exists(conn, "tenant_config", "trusted_domain"):
            try:
                conn.execute(text(
                    "ALTER TABLE tenant_config "
                    "ADD COLUMN trusted_domain VARCHAR(255) NOT NULL DEFAULT '' "
                    "COMMENT '租户可信域名(反代后对外域名)'"
                ))
            except Exception:
                # 并发下可能已被其他进程加上；后续 SELECT 再兜底
                pass


def is_safe_verify_filename(name: str) -> bool:
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    return bool(_SAFE_NAME.match(name))


def normalize_domain(domain: str) -> str:
    """去掉协议/路径/尾斜杠，仅保留 host[:port]"""
    d = (domain or "").strip().lower()
    if not d:
        return ""
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/", 1)[0].strip().rstrip(".")
    # 基础校验：允许字母数字.- 与端口
    if not re.match(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:\d{1,5})?$", d):
        raise ValueError(f"非法域名: {domain}")
    return d


def get_verify_file(filename: str) -> Optional[dict]:
    if not is_safe_verify_filename(filename):
        return None
    ensure_domain_tables()
    sql = text(
        "SELECT filename, content, content_type, tenant_id, trusted_domain, updated_at "
        "FROM domain_verify_file WHERE filename=:f LIMIT 1"
    )
    with get_engine().connect() as conn:
        r = conn.execute(sql, {"f": filename}).fetchone()
    if not r:
        return None
    return {
        "filename": r[0],
        "content": r[1] or "",
        "content_type": r[2] or "text/plain; charset=utf-8",
        "tenant_id": r[3],
        "trusted_domain": r[4] or "",
        "updated_at": str(r[5]) if r[5] is not None else "",
    }


def get_verify_by_tenant(tenant_id: str) -> Optional[dict]:
    ensure_domain_tables()
    sql = text(
        "SELECT filename, content, content_type, tenant_id, trusted_domain, updated_at "
        "FROM domain_verify_file WHERE tenant_id=:t LIMIT 1"
    )
    with get_engine().connect() as conn:
        r = conn.execute(sql, {"t": tenant_id}).fetchone()
    if not r:
        return None
    return {
        "filename": r[0],
        "content": r[1] or "",
        "content_type": r[2] or "text/plain; charset=utf-8",
        "tenant_id": r[3],
        "trusted_domain": r[4] or "",
        "updated_at": str(r[5]) if r[5] is not None else "",
    }


def save_verify_file(
    *,
    filename: str,
    content: str,
    tenant_id: str,
    trusted_domain: str = "",
    content_type: str = "text/plain; charset=utf-8",
) -> dict:
    """保存/覆盖：同租户旧文件删除；同文件名直接覆盖内容。"""
    if not is_safe_verify_filename(filename):
        raise ValueError("文件名不合法，仅允许字母数字._- 且后缀 .txt/.html")
    raw = content if isinstance(content, str) else str(content)
    if len(raw.encode("utf-8")) > _MAX_BYTES:
        raise ValueError(f"文件过大，上限 {_MAX_BYTES} 字节")
    domain = normalize_domain(trusted_domain) if trusted_domain else ""
    ensure_domain_tables()

    eng = get_engine()
    with eng.begin() as conn:
        # 同租户仅保留一份：删掉旧文件名（若与新文件名不同）
        old = conn.execute(
            text("SELECT filename FROM domain_verify_file WHERE tenant_id=:t"),
            {"t": tenant_id},
        ).fetchone()
        if old and old[0] != filename:
            conn.execute(
                text("DELETE FROM domain_verify_file WHERE tenant_id=:t"),
                {"t": tenant_id},
            )
        # 若文件名已被其他租户占用，拒绝
        owner = conn.execute(
            text("SELECT tenant_id FROM domain_verify_file WHERE filename=:f"),
            {"f": filename},
        ).fetchone()
        if owner and owner[0] and owner[0] != tenant_id:
            raise ValueError(f"文件名已被租户 {owner[0]} 占用")

        conn.execute(
            text(
                """
                INSERT INTO domain_verify_file
                    (filename, content, content_type, tenant_id, trusted_domain)
                VALUES (:f, :c, :ct, :t, :d)
                ON DUPLICATE KEY UPDATE
                    content=VALUES(content),
                    content_type=VALUES(content_type),
                    tenant_id=VALUES(tenant_id),
                    trusted_domain=VALUES(trusted_domain)
                """
            ),
            {"f": filename, "c": raw, "ct": content_type, "t": tenant_id, "d": domain},
        )
        if domain:
            conn.execute(
                text("UPDATE tenant_config SET trusted_domain=:d WHERE tenant_id=:t"),
                {"d": domain, "t": tenant_id},
            )

    return get_verify_by_tenant(tenant_id) or {
        "filename": filename,
        "content": raw,
        "content_type": content_type,
        "tenant_id": tenant_id,
        "trusted_domain": domain,
        "updated_at": "",
    }


def delete_verify_by_tenant(tenant_id: str) -> bool:
    ensure_domain_tables()
    with get_engine().begin() as conn:
        r = conn.execute(
            text("DELETE FROM domain_verify_file WHERE tenant_id=:t"),
            {"t": tenant_id},
        )
    return bool(r.rowcount)
