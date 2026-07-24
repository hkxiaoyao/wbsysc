-- Read-only pre-deployment inventory for the legacy MCP service compatibility layer.
-- This file makes no schema or data changes.

SELECT
  service.service_id,
  service.tenant_id,
  service.status,
  COUNT(DISTINCT binding.connection_id) AS connection_count,
  COUNT(DISTINCT binding.binding_id) AS binding_count,
  COUNT(DISTINCT CASE
    WHEN token.revoked_at IS NULL
      AND (token.expires_at IS NULL OR token.expires_at > UTC_TIMESTAMP())
    THEN token.token_id
  END) AS active_token_count,
  MAX(call_log.created_at) AS last_called_at
FROM mcp_service AS service
LEFT JOIN mcp_service_tool_binding AS binding
  ON binding.service_id = service.service_id
LEFT JOIN mcp_service_token AS token
  ON token.service_id = service.service_id
LEFT JOIN mcp_call_log AS call_log
  ON call_log.service_id = service.service_id
GROUP BY service.service_id, service.tenant_id, service.status
ORDER BY service.tenant_id, service.service_id;

-- Any returned row requires an explicit client migration decision. It must not
-- be silently converted into one connection endpoint.
SELECT
  service.service_id,
  service.tenant_id,
  COUNT(DISTINCT binding.connection_id) AS connection_count
FROM mcp_service AS service
JOIN mcp_service_tool_binding AS binding
  ON binding.service_id = service.service_id
GROUP BY service.service_id, service.tenant_id
HAVING COUNT(DISTINCT binding.connection_id) > 1
ORDER BY service.tenant_id, service.service_id;
