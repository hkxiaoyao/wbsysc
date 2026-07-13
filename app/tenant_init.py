"""
租户初始化 - 首次接入新租户时运行
- 中心库建 tenant_config 表
- 写入租户记录（secret AES 加密）
- 建该租户的独立 schema + 业务表

用法：
  python -m app.tenant_init --tenant-id tenant1 --corpid XXX --secret XXX --token XXX --display "客户A"

前置：DB_USER 需有 CREATE SCHEMA 权限。
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from .config import get_settings
from .crypto import encrypt_secret
from .db import ensure_schema, get_engine
from .tenant import _hash_corpid, reload_tenants


_TENANT_CONFIG_DDL = """
CREATE TABLE IF NOT EXISTS `tenant_config` (
  tenant_id VARCHAR(64) NOT NULL,
  display_name VARCHAR(128) NOT NULL DEFAULT '',
  corpid VARCHAR(64) NOT NULL,
  secret_encrypted VARBINARY(512) NOT NULL,
  mcp_token VARCHAR(128) NOT NULL,
  schema_name VARCHAR(64) NOT NULL DEFAULT '',
  sync_interval_min INT NOT NULL DEFAULT 30,
  enabled_modules VARCHAR(64) NOT NULL DEFAULT 'report,approval,checkin' COMMENT '启用模块:report,approval,checkin',
  checkin_userids TEXT NULL COMMENT '打卡可见员工userid,逗号分隔(无通讯录secret时用)',
  contact_secret_encrypted VARBINARY(512) NULL COMMENT '通讯录同步secret(AES加密,可选,用于自动拉userid)',
  enabled TINYINT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id),
  UNIQUE KEY uk_corpid (corpid),
  UNIQUE KEY uk_mcp_token (mcp_token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def init_tenant(tenant_id: str, corpid: str, secret: str, mcp_token: str,
                display_name: str = "", sync_interval_min: int = 30,
                modules: str = "report,approval,checkin",
                checkin_userids: str = "",
                contact_secret: str = "") -> str:
    """初始化一个租户：写配置 + 建schema。返回 schema_name

    Args:
        secret: 自建应用secret
        contact_secret: 通讯录同步secret（可选，用于自动拉userid喂打卡）
    """
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text(_TENANT_CONFIG_DDL))
        # MySQL 5.7 不支持 ADD COLUMN IF NOT EXISTS；先查 information_schema 再补列
        wanted_cols = {
            "enabled_modules":
                "ADD COLUMN enabled_modules VARCHAR(64) NOT NULL DEFAULT 'report,approval,checkin'",
            "checkin_userids":
                "ADD COLUMN checkin_userids TEXT NULL",
            "contact_secret_encrypted":
                "ADD COLUMN contact_secret_encrypted VARBINARY(512) NULL",
            "trusted_domain":
                "ADD COLUMN trusted_domain VARCHAR(255) NOT NULL DEFAULT ''",
        }
        for col, ddl in wanted_cols.items():
            exists = conn.execute(text(
                """SELECT 1 FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='tenant_config'
                     AND COLUMN_NAME=:c LIMIT 1"""
            ), {"c": col}).fetchone()
            if exists:
                continue
            try:
                conn.execute(text(f"ALTER TABLE tenant_config {ddl}"))
            except Exception:
                pass

    schema_name = f"wbd_{_hash_corpid(corpid)}"
    enc = encrypt_secret(secret)
    contact_enc = encrypt_secret(contact_secret) if contact_secret else None

    sql = text("""
        INSERT INTO tenant_config
            (tenant_id, display_name, corpid, secret_encrypted, mcp_token, schema_name,
             sync_interval_min, enabled_modules, checkin_userids, contact_secret_encrypted, enabled)
        VALUES (:t,:dn,:c,:se,:mt,:sn,:si,:em,:cu,:cs,1)
        ON DUPLICATE KEY UPDATE
            display_name=VALUES(display_name), corpid=VALUES(corpid),
            secret_encrypted=VALUES(secret_encrypted), mcp_token=VALUES(mcp_token),
            schema_name=VALUES(schema_name), sync_interval_min=VALUES(sync_interval_min),
            enabled_modules=VALUES(enabled_modules), checkin_userids=VALUES(checkin_userids),
            contact_secret_encrypted=VALUES(contact_secret_encrypted)
    """)
    with eng.begin() as conn:
        conn.execute(sql, {
            "t": tenant_id, "dn": display_name, "c": corpid,
            "se": enc, "mt": mcp_token, "sn": schema_name, "si": sync_interval_min,
            "em": modules, "cu": checkin_userids or None, "cs": contact_enc,
        })

    ensure_schema(schema_name)
    reload_tenants()
    extra = " +通讯录自动拉userid" if contact_secret else ""
    print(f"[OK] 租户 {tenant_id} 初始化完成: schema={schema_name} corpid={corpid} modules={modules}{extra}")
    return schema_name


def main():
    p = argparse.ArgumentParser(description="初始化企微中转租户")
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--corpid", required=True)
    p.add_argument("--secret", required=True, help="自建应用secret")
    p.add_argument("--token", required=True, help="workbuddy连接用的Bearer Token")
    p.add_argument("--display", default="")
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--modules", default="report,approval,checkin",
                   help="启用模块: report,approval,checkin")
    p.add_argument("--checkin-userids", default="",
                   help="打卡userid,逗号分隔(无通讯录secret时用)")
    p.add_argument("--contact-secret", default="",
                   help="通讯录同步secret(可选,配置后自动拉全企业userid喂打卡)")
    args = p.parse_args()

    init_tenant(args.tenant_id, args.corpid, args.secret, args.token,
                args.display, args.interval, args.modules,
                args.checkin_userids, args.contact_secret)


if __name__ == "__main__":
    main()