# 需求

诊断租户后台创建 OpenAPI 连接器时出现 `Request failed with status code 401` 的原因。先建立可重复验证信号，确认是会话、CSRF、请求头、路由权限还是外部 OpenAPI 探测导致；未经确认不扩大修改范围。
