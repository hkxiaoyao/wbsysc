-- Move WeCom trusted-domain verification files from tenant ownership to
-- connection-instance ownership. MySQL 5.7 compatible and idempotent.

DELIMITER //
DROP PROCEDURE IF EXISTS `migrate_connection_domain_verify`//
CREATE PROCEDURE `migrate_connection_domain_verify`()
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'domain_verify_file'
      AND COLUMN_NAME = 'connection_id'
  ) THEN
    ALTER TABLE `domain_verify_file`
      ADD COLUMN `connection_id` VARCHAR(64) NULL
      COMMENT '归属连接实例；历史数据可空' AFTER `tenant_id`;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'domain_verify_file'
      AND INDEX_NAME = 'uk_tenant'
  ) THEN
    ALTER TABLE `domain_verify_file` DROP INDEX `uk_tenant`;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'domain_verify_file'
      AND INDEX_NAME = 'idx_domain_verify_tenant'
  ) THEN
    ALTER TABLE `domain_verify_file`
      ADD KEY `idx_domain_verify_tenant` (`tenant_id`);
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'domain_verify_file'
      AND INDEX_NAME = 'uk_domain_verify_connection'
  ) THEN
    ALTER TABLE `domain_verify_file`
      ADD UNIQUE KEY `uk_domain_verify_connection` (`connection_id`);
  END IF;
END//
CALL `migrate_connection_domain_verify`()//
DROP PROCEDURE IF EXISTS `migrate_connection_domain_verify`//
DELIMITER ;

UPDATE `domain_verify_file` AS verify_file
JOIN `connection_instance` AS connection_row
  ON connection_row.`tenant_id` = verify_file.`tenant_id`
 AND connection_row.`connector_key` = 'wecom'
 AND JSON_VALID(connection_row.`public_config_json`)
 AND JSON_UNQUOTE(
       JSON_EXTRACT(connection_row.`public_config_json`, '$.legacy_source')
     ) = 'tenant_config'
SET verify_file.`connection_id` = connection_row.`connection_id`
WHERE verify_file.`connection_id` IS NULL;

