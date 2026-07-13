-- 可信域名 + 企微校验文件（中心库 websysc）
-- 反代域名接入时：访问 https://domain/xxx.txt 需能拿到校验内容
-- 兼容 MySQL 5.7：不用 ADD COLUMN IF NOT EXISTS（8.0.12+ 才有）

-- 若列已存在会报错，可忽略；应用启动/API 也会自动 information_schema 检测后补列
-- ALTER TABLE `tenant_config`
--   ADD COLUMN `trusted_domain` VARCHAR(255) NOT NULL DEFAULT ''
--   COMMENT '租户可信域名(反代后对外域名，如 mcp.example.com)';

SET @db := DATABASE();
SET @exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=@db AND TABLE_NAME='tenant_config' AND COLUMN_NAME='trusted_domain'
);
SET @sql := IF(@exists=0,
  "ALTER TABLE `tenant_config` ADD COLUMN `trusted_domain` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '租户可信域名(反代后对外域名，如 mcp.example.com)'",
  "SELECT 1"
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS `domain_verify_file` (
  `filename`       VARCHAR(160) NOT NULL COMMENT '根路径文件名，如 WW_verify_xxx.txt',
  `content`        MEDIUMTEXT   NOT NULL,
  `content_type`   VARCHAR(64)  NOT NULL DEFAULT 'text/plain; charset=utf-8',
  `tenant_id`      VARCHAR(64)  NULL COMMENT '归属租户，可空',
  `trusted_domain` VARCHAR(255) NULL COMMENT '绑定可信域名（展示用）',
  `updated_at`     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`filename`),
  UNIQUE KEY `uk_tenant` (`tenant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='可信域名校验文件(中心库)';
