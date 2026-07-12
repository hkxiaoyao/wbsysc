-- 企微数据中转 MCP Gateway - 数据表初始化脚本
-- 兼容 MySQL 5.7（测试库版本 5.7.44）
-- 多租户：一期单库 + tenant_id 列（PoC）；二期可升级为按 corpid 分 schema
-- 字符集 utf8mb4 支持 emoji 与生僻字

CREATE TABLE IF NOT EXISTS `wecom_report` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `tenant_id`   VARCHAR(64)  NOT NULL COMMENT '租户标识(MCP token绑定)',
  `journaluuid` VARCHAR(128) NOT NULL COMMENT '汇报单号',
  `template_id` VARCHAR(128) NOT NULL DEFAULT '' COMMENT '模板ID',
  `template_name` VARCHAR(128) NOT NULL DEFAULT '' COMMENT '模板名',
  `report_time` BIGINT       NOT NULL DEFAULT 0 COMMENT '汇报提交时间戳',
  `submitter_userid` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '提交人userid',
  `detail_json` JSON         DEFAULT NULL COMMENT '原始详情(全文)',
  `synced_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_tenant_journal` (`tenant_id`,`journaluuid`),
  KEY `idx_tenant_time` (`tenant_id`,`report_time`),
  KEY `idx_tenant_template` (`tenant_id`,`template_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='企微汇报记录';

CREATE TABLE IF NOT EXISTS `wecom_approval` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `tenant_id`   VARCHAR(64)  NOT NULL COMMENT '租户标识',
  `sp_no`       VARCHAR(64)  NOT NULL COMMENT '审批单号',
  `sp_name`     VARCHAR(128) NOT NULL DEFAULT '' COMMENT '审批申请类型名',
  `sp_status`   INT          NOT NULL DEFAULT 0 COMMENT '1审批中 2已通过 3已驳回 4已撤销',
  `template_id` VARCHAR(128) NOT NULL DEFAULT '' COMMENT '模板ID',
  `apply_time`  BIGINT       NOT NULL DEFAULT 0 COMMENT '申请提交时间戳',
  `applyer_userid` VARCHAR(64) NOT NULL DEFAULT '' COMMENT '申请人userid',
  `detail_json` JSON         DEFAULT NULL COMMENT '原始详情(全文)',
  `synced_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_tenant_sp` (`tenant_id`,`sp_no`),
  KEY `idx_tenant_time` (`tenant_id`,`apply_time`),
  KEY `idx_tenant_status` (`tenant_id`,`sp_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='企微审批记录';

CREATE TABLE IF NOT EXISTS `sync_cursor` (
  `tenant_id`    VARCHAR(64)  NOT NULL,
  `data_source`  VARCHAR(32)  NOT NULL COMMENT 'report/approval',
  `filter_key`   VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '过滤标识(如template_id),空为全量',
  `last_value`   VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '游标值(时间戳或id)',
  `last_sync_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`tenant_id`,`data_source`,`filter_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='同步游标';

CREATE TABLE IF NOT EXISTS `audit_log` (
  `id`         BIGINT      NOT NULL AUTO_INCREMENT,
  `tenant_id`  VARCHAR(64) NOT NULL,
  `tool_name`  VARCHAR(64) NOT NULL,
  `target`     VARCHAR(256) NOT NULL DEFAULT '' COMMENT '操作对象(sp_no/journaluuid)',
  `params_summary` VARCHAR(512) NOT NULL DEFAULT '',
  `result_status` VARCHAR(16) NOT NULL DEFAULT '' COMMENT 'ok/error',
  `cost_ms`    INT NOT NULL DEFAULT 0,
  `created_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_tenant_time` (`tenant_id`,`created_at`),
  KEY `idx_tool` (`tool_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='MCP调用审计日志';