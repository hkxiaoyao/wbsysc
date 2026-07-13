-- Gateway production upgrade; compatible with MySQL 5.7 and repeatable.

DROP PROCEDURE IF EXISTS migrate_gateway_central_columns;
DELIMITER //
CREATE PROCEDURE migrate_gateway_central_columns()
BEGIN
  DECLARE done INT DEFAULT 0;
  DECLARE column_value VARCHAR(64);
  DECLARE definition_value VARCHAR(255);
  DECLARE column_exists INT DEFAULT 0;
  DECLARE columns_cursor CURSOR FOR
    SELECT defs.column_name, defs.definition
    FROM (
      SELECT 'enabled_modules' column_name,
             'VARCHAR(64) NOT NULL DEFAULT ''report,approval,checkin''' definition
      UNION ALL SELECT 'checkin_userids', 'TEXT NULL'
      UNION ALL SELECT 'contact_secret_encrypted', 'VARBINARY(512) NULL'
      UNION ALL SELECT 'trusted_domain', 'VARCHAR(255) NOT NULL DEFAULT '''''
      UNION ALL SELECT 'data_mode', 'VARCHAR(16) NOT NULL DEFAULT ''stored'''
    ) defs;
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;

  OPEN columns_cursor;
  central_loop: LOOP
    FETCH columns_cursor INTO column_value, definition_value;
    IF done = 1 THEN
      LEAVE central_loop;
    END IF;
    SELECT COUNT(*) INTO column_exists
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE()
      AND TABLE_NAME='tenant_config'
      AND COLUMN_NAME=column_value;
    IF column_exists = 0 THEN
      SET @ddl = CONCAT(
        'ALTER TABLE `tenant_config` ADD COLUMN `', column_value, '` ', definition_value
      );
      PREPARE stmt FROM @ddl;
      EXECUTE stmt;
      DEALLOCATE PREPARE stmt;
    END IF;
  END LOOP;
  CLOSE columns_cursor;
END//
DELIMITER ;
CALL migrate_gateway_central_columns();
DROP PROCEDURE migrate_gateway_central_columns;

DROP PROCEDURE IF EXISTS migrate_gateway_business_schema;
DELIMITER //
CREATE PROCEDURE migrate_gateway_business_schema()
BEGIN
  DECLARE done INT DEFAULT 0;
  DECLARE schema_value VARCHAR(64);
  DECLARE table_value VARCHAR(64);
  DECLARE column_value VARCHAR(64);
  DECLARE definition_value VARCHAR(255);
  DECLARE column_exists INT DEFAULT 0;
  DECLARE schemas_cursor CURSOR FOR
    SELECT DISTINCT schema_name
    FROM tenant_config
    WHERE schema_name REGEXP '^wbd_[0-9A-Za-z_]+$';
  DECLARE columns_cursor CURSOR FOR
    SELECT DISTINCT tc.schema_name, defs.table_name, defs.column_name, defs.definition
    FROM tenant_config tc
    JOIN (
      SELECT 'wecom_report' table_name, 'source_window_start' column_name,
             'BIGINT NOT NULL DEFAULT 0' definition
      UNION ALL SELECT 'wecom_report', 'source_window_end', 'BIGINT NOT NULL DEFAULT 0'
      UNION ALL SELECT 'wecom_report', 'is_partial', 'TINYINT NOT NULL DEFAULT 0'
      UNION ALL SELECT 'wecom_approval', 'source_window_start', 'BIGINT NOT NULL DEFAULT 0'
      UNION ALL SELECT 'wecom_approval', 'source_window_end', 'BIGINT NOT NULL DEFAULT 0'
      UNION ALL SELECT 'wecom_approval', 'is_partial', 'TINYINT NOT NULL DEFAULT 0'
    ) defs ON 1=1
    WHERE tc.schema_name REGEXP '^wbd_[0-9A-Za-z_]+$';
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;

  OPEN schemas_cursor;
  schema_loop: LOOP
    FETCH schemas_cursor INTO schema_value;
    IF done = 1 THEN
      LEAVE schema_loop;
    END IF;

    SET @ddl = CONCAT(
      'CREATE DATABASE IF NOT EXISTS `', schema_value, '` CHARACTER SET utf8mb4'
    );
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;

    SET @ddl = CONCAT(
      'CREATE TABLE IF NOT EXISTS `', schema_value, '`.`wecom_report` (',
      'id BIGINT NOT NULL AUTO_INCREMENT, tenant_id VARCHAR(64) NOT NULL, ',
      'journaluuid VARCHAR(128) NOT NULL, template_id VARCHAR(128) NOT NULL DEFAULT '''', ',
      'template_name VARCHAR(128) NOT NULL DEFAULT '''', report_time BIGINT NOT NULL DEFAULT 0, ',
      'submitter_userid VARCHAR(64) NOT NULL DEFAULT '''', detail_json JSON DEFAULT NULL, ',
      'source_window_start BIGINT NOT NULL DEFAULT 0, source_window_end BIGINT NOT NULL DEFAULT 0, ',
      'is_partial TINYINT NOT NULL DEFAULT 0, synced_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, ',
      'PRIMARY KEY(id), UNIQUE KEY uk_tj(tenant_id,journaluuid), ',
      'KEY idx_tt(tenant_id,report_time), KEY idx_tpl(tenant_id,template_id)',
      ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
    );
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;

    SET @ddl = CONCAT(
      'CREATE TABLE IF NOT EXISTS `', schema_value, '`.`wecom_approval` (',
      'id BIGINT NOT NULL AUTO_INCREMENT, tenant_id VARCHAR(64) NOT NULL, ',
      'sp_no VARCHAR(64) NOT NULL, sp_name VARCHAR(128) NOT NULL DEFAULT '''', ',
      'sp_status INT NOT NULL DEFAULT 0, template_id VARCHAR(128) NOT NULL DEFAULT '''', ',
      'apply_time BIGINT NOT NULL DEFAULT 0, applyer_userid VARCHAR(64) NOT NULL DEFAULT '''', ',
      'detail_json JSON DEFAULT NULL, source_window_start BIGINT NOT NULL DEFAULT 0, ',
      'source_window_end BIGINT NOT NULL DEFAULT 0, is_partial TINYINT NOT NULL DEFAULT 0, ',
      'synced_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(id), ',
      'UNIQUE KEY uk_ts(tenant_id,sp_no), KEY idx_tt(tenant_id,apply_time), ',
      'KEY idx_ts(tenant_id,sp_status)',
      ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
    );
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;

    SET @ddl = CONCAT(
      'CREATE TABLE IF NOT EXISTS `', schema_value, '`.`wecom_checkin` (',
      'id BIGINT NOT NULL AUTO_INCREMENT, tenant_id VARCHAR(64) NOT NULL, ',
      'userid VARCHAR(64) NOT NULL, checkin_type VARCHAR(32) NOT NULL DEFAULT '''', ',
      'checkin_time BIGINT NOT NULL DEFAULT 0, exception_type VARCHAR(128) NOT NULL DEFAULT '''', ',
      'location_title VARCHAR(256) NOT NULL DEFAULT '''', group_name VARCHAR(128) NOT NULL DEFAULT '''', ',
      'detail_json JSON DEFAULT NULL, synced_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, ',
      'PRIMARY KEY(id), UNIQUE KEY uk_user_time(userid,checkin_time,checkin_type), ',
      'KEY idx_time(checkin_time), KEY idx_user_time(userid,checkin_time)',
      ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
    );
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;

    SET @ddl = CONCAT(
      'CREATE TABLE IF NOT EXISTS `', schema_value, '`.`sync_cursor` (',
      'tenant_id VARCHAR(64) NOT NULL, data_source VARCHAR(32) NOT NULL, ',
      'filter_key VARCHAR(64) NOT NULL DEFAULT '''', last_value VARCHAR(64) NOT NULL DEFAULT '''', ',
      'last_sync_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, ',
      'PRIMARY KEY(tenant_id,data_source,filter_key)',
      ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
    );
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;

    SET @ddl = CONCAT(
      'CREATE TABLE IF NOT EXISTS `', schema_value, '`.`audit_log` (',
      'id BIGINT NOT NULL AUTO_INCREMENT, tenant_id VARCHAR(64) NOT NULL, ',
      'tool_name VARCHAR(64) NOT NULL, target VARCHAR(256) NOT NULL DEFAULT '''', ',
      'params_summary VARCHAR(512) NOT NULL DEFAULT '''', ',
      'result_status VARCHAR(16) NOT NULL DEFAULT '''', cost_ms INT NOT NULL DEFAULT 0, ',
      'created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(id), ',
      'KEY idx_tt(tenant_id,created_at), KEY idx_tool(tool_name)',
      ') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
    );
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
  END LOOP;
  CLOSE schemas_cursor;

  SET done = 0;
  OPEN columns_cursor;
  column_loop: LOOP
    FETCH columns_cursor INTO schema_value, table_value, column_value, definition_value;
    IF done = 1 THEN
      LEAVE column_loop;
    END IF;
    SELECT COUNT(*) INTO column_exists
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=schema_value
      AND TABLE_NAME=table_value
      AND COLUMN_NAME=column_value;
    IF column_exists = 0 THEN
      SET @ddl = CONCAT(
        'ALTER TABLE `', schema_value, '`.`', table_value,
        '` ADD COLUMN `', column_value, '` ', definition_value
      );
      PREPARE stmt FROM @ddl;
      EXECUTE stmt;
      DEALLOCATE PREPARE stmt;
    END IF;
  END LOOP;
  CLOSE columns_cursor;
END//
DELIMITER ;
CALL migrate_gateway_business_schema();
DROP PROCEDURE migrate_gateway_business_schema;
