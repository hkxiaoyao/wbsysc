SET @db := DATABASE();
SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=@db AND TABLE_NAME='tenant_config' AND COLUMN_NAME='data_mode'
);
SET @sql := IF(
  @exists=0,
  "ALTER TABLE tenant_config ADD COLUMN data_mode VARCHAR(16) NOT NULL DEFAULT 'stored' COMMENT 'stored=MySQL缓存,direct=企微实时'",
  "SELECT 1"
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

DROP PROCEDURE IF EXISTS migrate_gateway_business_columns;
DELIMITER //
CREATE PROCEDURE migrate_gateway_business_columns()
BEGIN
  DECLARE done INT DEFAULT 0;
  DECLARE schema_value VARCHAR(64);
  DECLARE table_value VARCHAR(64);
  DECLARE column_value VARCHAR(64);
  DECLARE definition_value VARCHAR(255);
  DECLARE column_exists INT DEFAULT 0;
  DECLARE columns_cursor CURSOR FOR
    SELECT DISTINCT tc.schema_name, defs.table_name, defs.column_name, defs.definition
    FROM tenant_config tc
    JOIN (
      SELECT 'wecom_report' table_name, 'source_window_start' column_name, 'BIGINT NOT NULL DEFAULT 0' definition
      UNION ALL SELECT 'wecom_report', 'source_window_end', 'BIGINT NOT NULL DEFAULT 0'
      UNION ALL SELECT 'wecom_report', 'is_partial', 'TINYINT NOT NULL DEFAULT 0'
      UNION ALL SELECT 'wecom_approval', 'source_window_start', 'BIGINT NOT NULL DEFAULT 0'
      UNION ALL SELECT 'wecom_approval', 'source_window_end', 'BIGINT NOT NULL DEFAULT 0'
      UNION ALL SELECT 'wecom_approval', 'is_partial', 'TINYINT NOT NULL DEFAULT 0'
    ) defs ON 1=1
    WHERE tc.schema_name REGEXP '^wbd_[0-9A-Za-z_]+$';
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;

  OPEN columns_cursor;
  migration_loop: LOOP
    FETCH columns_cursor INTO schema_value, table_value, column_value, definition_value;
    IF done = 1 THEN
      LEAVE migration_loop;
    END IF;
    SELECT COUNT(*) INTO column_exists
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=schema_value AND TABLE_NAME=table_value AND COLUMN_NAME=column_value;
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
CALL migrate_gateway_business_columns();
DROP PROCEDURE migrate_gateway_business_columns;
