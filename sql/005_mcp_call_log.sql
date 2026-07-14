-- Central MCP call logs and gateway settings; MySQL 5.7 compatible and repeatable.

CREATE TABLE IF NOT EXISTS `mcp_call_log` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `tenant_id` VARCHAR(64) NOT NULL DEFAULT '',
  `category` VARCHAR(16) NOT NULL,
  `event_name` VARCHAR(96) NOT NULL,
  `target` VARCHAR(256) NOT NULL DEFAULT '',
  `params_summary` VARCHAR(512) NOT NULL DEFAULT '',
  `result_status` VARCHAR(16) NOT NULL,
  `error_code` VARCHAR(64) NOT NULL DEFAULT '',
  `error_summary` VARCHAR(256) NOT NULL DEFAULT '',
  `cost_ms` INT NOT NULL DEFAULT 0,
  `request_id` VARCHAR(64) NOT NULL DEFAULT '',
  `client_ip` VARCHAR(64) NOT NULL DEFAULT '',
  `http_method` VARCHAR(16) NOT NULL DEFAULT '',
  `http_status` SMALLINT NOT NULL DEFAULT 0,
  `legacy_schema` VARCHAR(64) NULL,
  `legacy_id` BIGINT NULL,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  KEY `idx_mcp_log_tenant_created` (`tenant_id`, `created_at`, `id`),
  KEY `idx_mcp_log_created` (`created_at`, `id`),
  KEY `idx_mcp_log_event` (`category`, `event_name`, `created_at`),
  KEY `idx_mcp_log_status` (`result_status`, `created_at`),
  KEY `idx_mcp_log_request` (`request_id`),
  KEY `idx_mcp_log_ip_created` (`client_ip`, `created_at`),
  KEY `idx_mcp_log_cost_created` (`cost_ms`, `created_at`),
  UNIQUE KEY `uk_mcp_log_legacy` (`legacy_schema`, `legacy_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `gateway_setting` (
  `setting_key` VARCHAR(64) NOT NULL,
  `setting_value` VARCHAR(255) NOT NULL,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`setting_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
