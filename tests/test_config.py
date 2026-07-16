import os
import runpy
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import URL

from app.config import Settings


def prod_settings(**overrides):
    values = {
        "app_env": "prod",
        "credential_key": "k" * 32,
        "mcp_token_hmac_key": "h" * 32,
        "admin_password": "admin-password-123",
        "db_password": "db-password-123",
        "wecom_use_mock": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("admin_password", "CHANGE_ME", "ADMIN_PASSWORD"),
        ("admin_password", "<强密码，登录管理后台用>", "ADMIN_PASSWORD"),
        ("db_password", "", "DB_PASSWORD"),
        ("db_password", "<强密码，与开发库不同>", "DB_PASSWORD"),
        ("wecom_use_mock", True, "WECOM_USE_MOCK"),
    ],
)
def test_prod_rejects_unsafe_values(field, value, message):
    with pytest.raises(ValidationError, match=message):
        prod_settings(**{field: value})


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "<强随机串>",
        "k" * 31,
        "密" * 10,
    ],
)
def test_prod_rejects_unsafe_credential_key(value):
    with pytest.raises(ValidationError, match="CREDENTIAL_KEY"):
        prod_settings(credential_key=value)


def test_prod_accepts_credential_key_with_at_least_32_utf8_bytes():
    settings = prod_settings(credential_key="密" * 11)
    assert len(settings.credential_key.encode("utf-8")) == 33


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "CHANGE_ME",
        "<强随机串>",
        "PoC_DEFAULT_KEY_DO_NOT_USE_IN_PRODUCTION_32bytes!",
        "h" * 31,
        "密" * 10,
    ],
)
def test_prod_rejects_unsafe_mcp_token_hmac_key(value):
    with pytest.raises(ValidationError, match="MCP_TOKEN_HMAC_KEY"):
        prod_settings(mcp_token_hmac_key=value)


def test_prod_accepts_mcp_token_hmac_key_with_at_least_32_utf8_bytes():
    settings = prod_settings(mcp_token_hmac_key="密" * 11)
    assert len(settings.mcp_token_hmac_key.encode("utf-8")) == 33


def test_prod_rejects_mcp_hmac_key_that_reuses_the_credential_key():
    with pytest.raises(ValidationError, match="MCP_TOKEN_HMAC_KEY"):
        prod_settings(mcp_token_hmac_key="k" * 32)


def test_production_template_and_deployer_manage_a_distinct_hmac_key():
    root = Path(__file__).resolve().parents[1]
    template = (root / ".env.prod.example").read_text(encoding="utf-8")
    deployer = (root / "deploy" / "server_deploy.sh").read_text(encoding="utf-8")

    assert "MCP_TOKEN_HMAC_KEY=replace_with_hmac_key" in template
    assert 'MCP_TOKEN_HMAC_KEY="$(read_env_value MCP_TOKEN_HMAC_KEY)"' in deployer
    assert "已自动生成 MCP_TOKEN_HMAC_KEY" in deployer
    assert "MCP_TOKEN_HMAC_KEY 必须为非示例值且至少 32 UTF-8 字节" in deployer
    assert '"$MCP_TOKEN_HMAC_KEY" = "$CREDENTIAL_KEY"' in deployer
    assert "unset ADMIN_PASSWORD CREDENTIAL_KEY MCP_TOKEN_HMAC_KEY" in deployer


def test_dev_keeps_mock_and_empty_key_fallback():
    settings = Settings(app_env="dev", wecom_use_mock=True, credential_key="")
    assert settings.wecom_use_mock is True


def test_db_url_preserves_special_password_characters():
    settings = Settings(app_env="dev", db_password="p@ss:#/word")
    assert isinstance(settings.db_url, URL)
    assert settings.db_url.password == "p@ss:#/word"


def test_smoke_import_does_not_mutate_proxy_environment(monkeypatch):
    proxy_values = {
        "HTTP_PROXY": "http://upper-http.test",
        "HTTPS_PROXY": "http://upper-https.test",
        "http_proxy": "http://lower-http.test",
        "https_proxy": "http://lower-https.test",
        "NO_PROXY": "example.test",
    }
    for key, value in proxy_values.items():
        monkeypatch.setenv(key, value)
    before_import = {key: os.environ.get(key) for key in proxy_values}

    smoke_path = Path(__file__).with_name("test_smoke_client.py")
    runpy.run_path(str(smoke_path), run_name="smoke_import_test")

    assert {key: os.environ.get(key) for key in proxy_values} == before_import
