# 交叉核对

- Claude 分析确认症状收敛于 Uvicorn 未信任 Docker 代理来源，导致
  `request.base_url.scheme` 被判断为 HTTP。
- Gemini 因环境未配置 API key 无法运行。
- Claude 基于仓库示例 Nginx 配置提出“代理头可能未转发”；已用线上
  `nginx -T` 证伪该分支，线上实际配置已转发代理头。
- 最终根因由生产请求、容器命令、Docker 网络和 HTTP/HTTPS 差分反馈回路共同确认。
