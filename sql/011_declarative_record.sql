-- Central storage for declarative-connector stored mode.
--
-- Declarative connections have no allocated per-connection MySQL schema (their
-- public config carries only spec_id/revision and sync settings), so the synced
-- projection lives in the central database alongside connection_sync_state and
-- connection_tool_policy.  Isolation is by connection_id, which the server
-- always supplies; a tenant can never widen it from a request.
--
-- payload_json holds ONLY the fields named by the revision's sync_spec
-- field_mappings.  A raw upstream response body must never be written here.
--
-- MySQL 5.7 compatible and idempotent.

CREATE TABLE IF NOT EXISTS `declarative_record` (
  `connection_id` VARCHAR(64) NOT NULL COMMENT '归属连接实例',
  `resource_key` VARCHAR(128) NOT NULL COMMENT 'sync_spec 声明的资源键',
  `record_key` VARCHAR(255) NOT NULL COMMENT 'primary_key_pointer 提取的主键',
  `payload_json` TEXT NOT NULL COMMENT '仅 field_mappings 投影后的字段',
  `synced_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`connection_id`, `resource_key`, `record_key`),
  KEY `idx_declarative_record_resource`
    (`connection_id`, `resource_key`, `synced_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
