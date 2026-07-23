-- Tenant-scoped MCP services and materialized tool aliases. MySQL 5.7 compatible.

SET @connection_alias_column := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'connection_instance'
    AND COLUMN_NAME = 'connection_alias'
);
SET @connection_alias_column_ddl := IF(
  @connection_alias_column = 0,
  'ALTER TABLE `connection_instance` ADD COLUMN `connection_alias` VARCHAR(64) NULL AFTER `tenant_id`',
  'SELECT 1'
);
PREPARE connection_alias_column_migration FROM @connection_alias_column_ddl;
EXECUTE connection_alias_column_migration;
DEALLOCATE PREPARE connection_alias_column_migration;

UPDATE `connection_instance`
SET `connection_alias` = CONCAT('conn_', LEFT(SHA1(`connection_id`), 40))
WHERE `connection_alias` IS NULL OR `connection_alias` = '';

ALTER TABLE `connection_instance`
  MODIFY COLUMN `connection_alias` VARCHAR(64) NOT NULL;

SET @connection_alias_index_shape := (
  SELECT CONCAT(
    MIN(`NON_UNIQUE`), '|',
    GROUP_CONCAT(`COLUMN_NAME` ORDER BY `SEQ_IN_INDEX` SEPARATOR ',')
  )
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'connection_instance'
    AND INDEX_NAME = 'uk_connection_instance_tenant_alias'
);
SET @connection_alias_index_drop := IF(
  @connection_alias_index_shape IS NOT NULL
    AND @connection_alias_index_shape <> '0|tenant_id,connection_alias',
  'ALTER TABLE `connection_instance` DROP INDEX `uk_connection_instance_tenant_alias`',
  'SELECT 1'
);
PREPARE connection_alias_index_repair FROM @connection_alias_index_drop;
EXECUTE connection_alias_index_repair;
DEALLOCATE PREPARE connection_alias_index_repair;

SET @connection_alias_index_add := IF(
  @connection_alias_index_shape = '0|tenant_id,connection_alias',
  'SELECT 1',
  'ALTER TABLE `connection_instance` ADD UNIQUE KEY `uk_connection_instance_tenant_alias` (`tenant_id`, `connection_alias`)'
);
PREPARE connection_alias_index_create FROM @connection_alias_index_add;
EXECUTE connection_alias_index_create;
DEALLOCATE PREPARE connection_alias_index_create;

CREATE TABLE IF NOT EXISTS `mcp_service` (
  `service_id` VARCHAR(64) NOT NULL,
  `tenant_id` VARCHAR(64) NOT NULL,
  `display_name` VARCHAR(128) NOT NULL,
  `service_key` VARCHAR(64) NOT NULL,
  `status` VARCHAR(16) NOT NULL DEFAULT 'draft',
  `config_version` INT NOT NULL DEFAULT 1,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`service_id`),
  UNIQUE KEY `uk_mcp_service_tenant_key` (`tenant_id`, `service_key`),
  KEY `idx_mcp_service_tenant_status` (`tenant_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `mcp_service_tool_binding` (
  `binding_id` VARCHAR(64) NOT NULL,
  `service_id` VARCHAR(64) NOT NULL,
  `connection_id` VARCHAR(64) NOT NULL,
  `source_tool_key` VARCHAR(128) NOT NULL,
  `tool_alias` VARCHAR(128) NOT NULL,
  `binding_status` VARCHAR(16) NOT NULL DEFAULT 'active',
  `policy_json` TEXT NOT NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`binding_id`),
  UNIQUE KEY `uk_service_tool_alias` (`service_id`, `tool_alias`),
  UNIQUE KEY `uk_service_source_tool` (`service_id`, `connection_id`, `source_tool_key`),
  KEY `idx_service_binding_connection` (`connection_id`, `service_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `mcp_service_token` (
  `token_id` VARCHAR(64) NOT NULL,
  `service_id` VARCHAR(64) NOT NULL,
  `token_hmac` CHAR(64) NOT NULL,
  `encrypted_token` VARBINARY(4096) NULL,
  `token_prefix` VARCHAR(32) NOT NULL,
  `token_label` VARCHAR(128) NOT NULL DEFAULT '',
  `expires_at` DATETIME NULL,
  `revoked_at` DATETIME NULL,
  `last_used_at` DATETIME NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`token_id`),
  UNIQUE KEY `uk_mcp_service_token_hmac` (`token_hmac`),
  KEY `idx_mcp_service_token_service` (`service_id`, `revoked_at`, `expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET @mcp_log_service_column := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'mcp_call_log'
    AND COLUMN_NAME = 'service_id'
);
SET @mcp_log_service_column_ddl := IF(
  @mcp_log_service_column = 0,
  'ALTER TABLE `mcp_call_log` ADD COLUMN `service_id` VARCHAR(64) NULL AFTER `tenant_id`',
  'SELECT 1'
);
PREPARE mcp_log_service_column_migration FROM @mcp_log_service_column_ddl;
EXECUTE mcp_log_service_column_migration;
DEALLOCATE PREPARE mcp_log_service_column_migration;

SET @mcp_log_alias_column := (
  SELECT COUNT(*)
  FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'mcp_call_log'
    AND COLUMN_NAME = 'tool_alias'
);
SET @mcp_log_alias_column_ddl := IF(
  @mcp_log_alias_column = 0,
  'ALTER TABLE `mcp_call_log` ADD COLUMN `tool_alias` VARCHAR(128) NULL AFTER `tool_key`',
  'SELECT 1'
);
PREPARE mcp_log_alias_column_migration FROM @mcp_log_alias_column_ddl;
EXECUTE mcp_log_alias_column_migration;
DEALLOCATE PREPARE mcp_log_alias_column_migration;

SET @mcp_log_service_index := (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'mcp_call_log'
    AND INDEX_NAME = 'idx_mcp_log_service_created'
);
SET @mcp_log_service_index_ddl := IF(
  @mcp_log_service_index = 0,
  'ALTER TABLE `mcp_call_log` ADD KEY `idx_mcp_log_service_created` (`tenant_id`, `service_id`, `created_at`, `id`)',
  'SELECT 1'
);
PREPARE mcp_log_service_index_migration FROM @mcp_log_service_index_ddl;
EXECUTE mcp_log_service_index_migration;
DEALLOCATE PREPARE mcp_log_service_index_migration;

SET @mcp_log_alias_index := (
  SELECT COUNT(*)
  FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'mcp_call_log'
    AND INDEX_NAME = 'idx_mcp_log_alias_created'
);
SET @mcp_log_alias_index_ddl := IF(
  @mcp_log_alias_index = 0,
  'ALTER TABLE `mcp_call_log` ADD KEY `idx_mcp_log_alias_created` (`service_id`, `tool_alias`, `created_at`, `id`)',
  'SELECT 1'
);
PREPARE mcp_log_alias_index_migration FROM @mcp_log_alias_index_ddl;
EXECUTE mcp_log_alias_index_migration;
DEALLOCATE PREPARE mcp_log_alias_index_migration;
