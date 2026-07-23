# 诊断结论

## 根因

生产 Nginx 已将 `X-Forwarded-Proto`、Host 和端口代理头转发给应用，但 Docker
容器内 Uvicorn 未配置可信代理来源。Nginx 经宿主机映射端口访问容器时，应用看到
的来源为 Docker 网关 `172.27.0.1`，不在 Uvicorn 默认仅信任的
`127.0.0.1` 范围内，因此忽略代理头并将 HTTPS 请求判断为 HTTP。

`require_same_origin` 随后比较浏览器的 HTTPS Origin 与应用推导出的 HTTP
`base_url`，需要同源校验的写操作均返回 403。

HTTP 访问无法作为替代方案，因为生产租户会话 Cookie 正确标记为 `Secure`；
浏览器不会通过 HTTP 携带该 Cookie，导致登录后的 `/tenant/session` 返回 401。

## 生产证据

- 容器健康、无重启，管理员租户/连接/MCP 服务/日志读接口均返回 200。
- HTTPS 域名下以有效管理员会话调用同源保护写接口，稳定返回
  `403 {"detail":"请求来源无效"}`。
- 同一租户登录链路在 HTTP + `Secure` Cookie 下为登录 200、会话 401；切换为
  HTTPS 后会话为 200。
- 线上 Nginx 确实发送 `X-Forwarded-Proto $scheme`。
- 容器命令未设置 `--forwarded-allow-ips`，Docker 网关为 `172.27.0.1`。
- 端口 8001 当前绑定 `0.0.0.0` 和 `[::]`，仍可绕过 Nginx 直接访问。

## 修复边界

- 保留 `Secure` Cookie 和严格同源校验，不放宽认证规则。
- 将应用端口仅绑定宿主机回环地址，通过 Nginx 对外提供 HTTPS。
- 配置 Uvicorn 仅信任实际 Docker 代理网段或网关，并验证网络重建后的稳定性。
- 增加 HTTPS 代理头下写操作的回归测试和部署配置测试。

本任务只完成诊断，未修改或重启生产配置，未执行安全测试。
