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
