-- Separate tenant identity from legacy WeCom connection columns.
-- MySQL 5.7 compatible and idempotent: only NOT NULL constraints are relaxed.

DROP PROCEDURE IF EXISTS `migrate_tenant_identity_boundary`;
DELIMITER //
CREATE PROCEDURE `migrate_tenant_identity_boundary`()
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'tenant_config'
      AND COLUMN_NAME = 'corpid'
      AND IS_NULLABLE = 'NO'
  ) THEN
    ALTER TABLE `tenant_config`
      MODIFY COLUMN `corpid` VARCHAR(64) NULL COMMENT '企微corpid';
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'tenant_config'
      AND COLUMN_NAME = 'secret_encrypted'
      AND IS_NULLABLE = 'NO'
  ) THEN
    ALTER TABLE `tenant_config`
      MODIFY COLUMN `secret_encrypted` VARBINARY(512) NULL
      COMMENT '企微secret(AES-Fernet加密)';
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'tenant_config'
      AND COLUMN_NAME = 'mcp_token'
      AND IS_NULLABLE = 'NO'
  ) THEN
    ALTER TABLE `tenant_config`
      MODIFY COLUMN `mcp_token` VARCHAR(128) NULL
      COMMENT 'workbuddy连接用的Bearer Token';
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'tenant_config'
      AND COLUMN_NAME = 'schema_name'
      AND IS_NULLABLE = 'NO'
  ) THEN
    ALTER TABLE `tenant_config`
      MODIFY COLUMN `schema_name` VARCHAR(64) NULL DEFAULT ''
      COMMENT '该租户独立schema名';
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'tenant_config'
      AND COLUMN_NAME = 'sync_interval_min'
      AND IS_NULLABLE = 'NO'
  ) THEN
    ALTER TABLE `tenant_config`
      MODIFY COLUMN `sync_interval_min` INT NULL DEFAULT 30
      COMMENT '该租户同步间隔(分钟)';
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'tenant_config'
      AND COLUMN_NAME = 'enabled_modules'
      AND IS_NULLABLE = 'NO'
  ) THEN
    ALTER TABLE `tenant_config`
      MODIFY COLUMN `enabled_modules` VARCHAR(64) NULL
      DEFAULT 'report,approval,checkin';
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'tenant_config'
      AND COLUMN_NAME = 'trusted_domain'
      AND IS_NULLABLE = 'NO'
  ) THEN
    ALTER TABLE `tenant_config`
      MODIFY COLUMN `trusted_domain` VARCHAR(255) NULL DEFAULT ''
      ;
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'tenant_config'
      AND COLUMN_NAME = 'data_mode'
      AND IS_NULLABLE = 'NO'
  ) THEN
    ALTER TABLE `tenant_config`
      MODIFY COLUMN `data_mode` VARCHAR(16) NULL DEFAULT 'stored'
      ;
  END IF;
END//
DELIMITER ;

CALL `migrate_tenant_identity_boundary`();
DROP PROCEDURE IF EXISTS `migrate_tenant_identity_boundary`;
