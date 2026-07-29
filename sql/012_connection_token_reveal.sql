-- Add management-only reversible storage for connection bearer tokens.
-- Existing token rows intentionally remain NULL and therefore cannot be
-- recovered.  MySQL 5.7 requires an information_schema guard for this DDL.

DELIMITER //
DROP PROCEDURE IF EXISTS `migrate_connection_token_reveal`//
CREATE PROCEDURE `migrate_connection_token_reveal`()
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'connection_token'
      AND COLUMN_NAME = 'encrypted_token'
  ) THEN
    ALTER TABLE `connection_token`
      ADD COLUMN `encrypted_token` VARBINARY(4096) NULL AFTER `token_hmac`;
  END IF;
END//
CALL `migrate_connection_token_reveal`()//
DROP PROCEDURE IF EXISTS `migrate_connection_token_reveal`//
DELIMITER ;
