-- 可信域名 + 企微校验文件（中心库 websysc）
-- 反代域名接入时：访问 https://domain/xxx.txt 需能拿到校验内容

ALTER TABLE `tenant_config`
  ADD COLUMN IF NOT EXISTS `trusted_domain` VARCHAR(255) NOT NULL DEFAULT ''
  COMMENT '租户可信域名(反代后对外域名，如 mcp.example.com)';

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
