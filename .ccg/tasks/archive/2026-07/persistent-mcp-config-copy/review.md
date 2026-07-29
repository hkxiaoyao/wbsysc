# 审查结果

## 结果

- Critical：无。
- Warning：2 项，均已修复并补测。
  - 补充连接 Token reveal 的 429、`no-store` 和 denied 审计测试。
  - 补充历史/撤销 Token 的 `revealable=false` 测试。
- Info：按 Token 维度限流而非管理员全局限流；剪贴板 Promise 已调用后无法取消。两项均不影响当前权限边界与正确性。

## 安全与兼容性确认

- MCP 鉴权路径只比较 HMAC，不读取或解密密文。
- reveal 要求已认证会话、同源请求、租户归属、有效 Token、限流和成功审计。
- reveal 成功审计未被接受时 fail-closed，不返回原文。
- 撤销、轮换和租户注销均清除连接 Token 密文。
- 历史 NULL 密文 Token 继续可用于鉴权，但不可 reveal；前端明确提示重新签发。
- 原文不进入列表响应、URL、localStorage 或日志。
- 迁移 012 兼容 MySQL 5.7，并在发布脚本启动新镜像前执行。

## 验证

- 后端全量：1424 passed，1 skipped，1 个既有 StarletteDeprecationWarning。
- 前端全量：124/124 passed。
- 前端生产构建：passed（保留既有大 chunk warning）。
- Python compileall：passed。
- git diff --check：passed。
- Claude：无 Critical，2 项 Warning 已修复。
- Gemini：环境缺少 `GEMINI_API_KEY`，未能运行。
