-- Tenant-scoped multi-provider connection platform. MySQL 5.7 compatible.
-- Tokens are stored only as keyed HMAC digests; credential values are encrypted.

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
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`connection_id`),
  KEY `idx_connection_instance_tenant` (`tenant_id`, `status`, `connector_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `connection_credential` (
  `connection_id` VARCHAR(64) NOT NULL,
  `credential_key` VARCHAR(64) NOT NULL,
  `encrypted_value` VARBINARY(4096) NOT NULL,
  `metadata_json` TEXT NOT NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`connection_id`, `credential_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `connection_tool_policy` (
  `connection_id` VARCHAR(64) NOT NULL,
  `tool_name` VARCHAR(128) NOT NULL,
  `enabled` TINYINT NOT NULL DEFAULT 1,
  `policy_json` TEXT NOT NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`connection_id`, `tool_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `connection_sync_state` (
  `connection_id` VARCHAR(64) NOT NULL,
  `state_key` VARCHAR(64) NOT NULL,
  `state_json` TEXT NOT NULL,
  `last_success_at` DATETIME NULL,
  `last_error` VARCHAR(512) NOT NULL DEFAULT '',
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`connection_id`, `state_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `declarative_spec_revision` (
  `spec_id` VARCHAR(64) NOT NULL,
  `revision` INT NOT NULL,
  `tenant_id` VARCHAR(64) NOT NULL,
  `connection_id` VARCHAR(64) NOT NULL,
  `status` VARCHAR(16) NOT NULL DEFAULT 'draft',
  `spec_json` MEDIUMTEXT NOT NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`spec_id`, `revision`),
  KEY `idx_declarative_spec_tenant` (`tenant_id`, `connection_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `declarative_spec_operation` (
  `operation_id` VARCHAR(64) NOT NULL,
  `spec_id` VARCHAR(64) NOT NULL,
  `revision` INT NOT NULL,
  `connection_id` VARCHAR(64) NOT NULL,
  `operation_key` VARCHAR(128) NOT NULL,
  `operation_json` MEDIUMTEXT NOT NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`operation_id`),
  UNIQUE KEY `uk_declarative_spec_operation` (`spec_id`, `revision`, `operation_key`),
  KEY `idx_declarative_operation_connection` (`connection_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- CREATE TABLE IF NOT EXISTS does not widen columns on an existing install.
-- MySQL 5.7 has no ALTER ... IF EXISTS/IF NOT EXISTS for column types, so use
-- information_schema plus prepared DDL to make the TEXT -> MEDIUMTEXT upgrade
-- repeatable without rebuilding an already-upgraded table on every deploy.
SET @spec_json_type := (
  SELECT LOWER(`DATA_TYPE`)
  FROM information_schema.columns
  WHERE `TABLE_SCHEMA` = DATABASE()
    AND `TABLE_NAME` = 'declarative_spec_revision'
    AND `COLUMN_NAME` = 'spec_json'
  LIMIT 1
);
SET @spec_json_ddl := IF(
  @spec_json_type = 'mediumtext',
  'SELECT 1',
  'ALTER TABLE `declarative_spec_revision` MODIFY COLUMN `spec_json` MEDIUMTEXT NOT NULL'
);
PREPARE spec_json_migration FROM @spec_json_ddl;
EXECUTE spec_json_migration;
DEALLOCATE PREPARE spec_json_migration;

SET @operation_json_type := (
  SELECT LOWER(`DATA_TYPE`)
  FROM information_schema.columns
  WHERE `TABLE_SCHEMA` = DATABASE()
    AND `TABLE_NAME` = 'declarative_spec_operation'
    AND `COLUMN_NAME` = 'operation_json'
  LIMIT 1
);
SET @operation_json_ddl := IF(
  @operation_json_type = 'mediumtext',
  'SELECT 1',
  'ALTER TABLE `declarative_spec_operation` MODIFY COLUMN `operation_json` MEDIUMTEXT NOT NULL'
);
PREPARE operation_json_migration FROM @operation_json_ddl;
EXECUTE operation_json_migration;
DEALLOCATE PREPARE operation_json_migration;
