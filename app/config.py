"""
企微数据中转 MCP Gateway - 配置中心
基于 pydantic-settings，所有敏感值走 .env，禁止硬编码
"""
from __future__ import annotations

from functools import lru_cache
from typing import Dict

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 运行环境
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # MySQL（一期单实例多 schema）
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = "websysc"
    db_user: str = "websysc"
    db_password: str = ""
    db_pool_size: int = 5

    # MCP 鉴权：PoC 用环境变量映射 token->租户标识
    # 格式 "tokenA:tenantA,tokenB:tenantB"
    mcp_tokens: str = ""
    mcp_base_url: str = "http://localhost:8000"

    # 企微
    wecom_use_mock: bool = True
    wecom_corpid: str = ""
    wecom_secret: str = ""

    # Redis（可选，PoC 内存兜底）
    redis_url: str = ""

    # 凭证加密主密钥（PoC 可空走兜底；生产必须配强随机串）
    credential_key: str = ""

    # 管理后台密码（单密码登录，session token 鉴权）
    admin_password: str = ""
    admin_session_ttl_min: int = 480   # session 有效期(分钟), 默认 8 小时

    # 同步间隔（分钟）
    sync_interval_report_min: int = 30
    sync_interval_approval_min: int = 30
    sync_interval_smarttable_min: int = 60

    @property
    def token_map(self) -> Dict[str, str]:
        """解析 token -> 租户标识 映射"""
        mapping: Dict[str, str] = {}
        if not self.mcp_tokens:
            return mapping
        for pair in self.mcp_tokens.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            token, tenant = pair.split(":", 1)
            if token and tenant:
                mapping[token.strip()] = tenant.strip()
        return mapping

    @property
    def db_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()