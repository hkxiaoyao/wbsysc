-- 阶段二多租户：中心配置表（建在主库 websysc）
-- tenant_config 存所有租户的连接信息+企微凭证(AES加密)
-- 各租户业务数据建在独立 schema: wbd_{corpid_hash}
-- 注意：执行前需给 DB_USER 授予 CREATE SCHEMA 权限（见 README）

CREATE TABLE IF NOT EXISTS `tenant_config` (
  `tenant_id`        VARCHAR(64)  NOT NULL COMMENT '租户标识',
  `display_name`     VARCHAR(128) NOT NULL DEFAULT '',
  `corpid`           VARCHAR(64)  NOT NULL COMMENT '企微corpid',
  `secret_encrypted` VARBINARY(512) NOT NULL COMMENT '企微secret(AES-Fernet加密)',
  `mcp_token`        VARCHAR(128) NOT NULL COMMENT 'workbuddy连接用的Bearer Token',
  `schema_name`      VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '该租户独立schema名',
  `sync_interval_min` INT NOT NULL DEFAULT 30 COMMENT '该租户同步间隔(分钟)',
  `enabled_modules`  VARCHAR(64) NOT NULL DEFAULT 'report,approval,checkin',
  `checkin_userids`  TEXT NULL,
  `contact_secret_encrypted` VARBINARY(512) NULL,
  `trusted_domain`   VARCHAR(255) NOT NULL DEFAULT '',
  `data_mode`        VARCHAR(16) NOT NULL DEFAULT 'stored',
  `enabled`          TINYINT NOT NULL DEFAULT 1,
  `created_at`       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`tenant_id`),
  UNIQUE KEY `uk_corpid` (`corpid`),
  UNIQUE KEY `uk_mcp_token` (`mcp_token`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT '租户配置(中心库)';

-- 把现有 .env 的 tenant1 迁移为 DB 记录的初始化数据（secret需运行时加密写入，
-- 不在此明文；首次启动由 init 脚本或管理 API 写入）
