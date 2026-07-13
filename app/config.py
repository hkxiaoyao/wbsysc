"""
企微数据中转 MCP Gateway - 配置中心
基于 pydantic-settings，所有敏感值走 .env，禁止硬编码
"""
from __future__ import annotations

from functools import lru_cache
from typing import Dict

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


EXAMPLE_PASSWORDS = {"CHANGE_ME", "<强密码，与开发库不同>", "<强密码，登录管理后台用>"}


class Settings(BaseSettings):
    # 优先从环境变量读（docker compose env_file:已注入），避免容器内读 .env 文件的权限问题。
    # 本地开发时若有 .env 文件也顺带读（仅当文件可读时）。
    model_config = SettingsConfigDict(
        env_file=None, env_file_encoding="utf-8", extra="ignore"
    )
    # 本地开发兜底：若 .env 文件存在且可读，手动加载（容器内只走环境变量）
    try:
        from dotenv import dotenv_values as _dv
        _env_vals = _dv(".env")
        if _env_vals:
            for _k, _v in _env_vals.items():
                if _v is not None and _k not in __import__("os").environ:
                    __import__("os").environ[_k] = _v
    except Exception:
        pass

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
    # MCP DNS 重绑定保护允许的 Host（逗号分隔）。空=关闭 Host 校验（适合反代+Bearer）
    # 例: wbsysc.hacka.cn,mcp.example.com
    mcp_allowed_hosts: str = ""

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

    @model_validator(mode="after")
    def validate_production(self):
        if self.app_env.lower() != "prod":
            return self
        errors = []
        credential_key = self.credential_key.strip()
        if (
            credential_key == "<强随机串>"
            or len(credential_key.encode("utf-8")) < 32
        ):
            errors.append(
                "CREDENTIAL_KEY must be a non-example value of at least 32 UTF-8 bytes in production"
            )
        if not self.admin_password or self.admin_password in EXAMPLE_PASSWORDS:
            errors.append("ADMIN_PASSWORD must be a non-example value in production")
        if not self.db_password or self.db_password in EXAMPLE_PASSWORDS:
            errors.append("DB_PASSWORD must be a non-example value in production")
        if self.wecom_use_mock:
            errors.append("WECOM_USE_MOCK must be false in production")
        if errors:
            raise ValueError("; ".join(errors))
        return self

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
    def db_url(self) -> URL:
        return URL.create(
            "mysql+pymysql",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"charset": "utf8mb4"},
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
