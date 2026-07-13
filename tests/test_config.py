import pytest
from pydantic import ValidationError
from sqlalchemy.engine import URL

from app.config import Settings


def prod_settings(**overrides):
    values = {
        "app_env": "prod",
        "credential_key": "k" * 32,
        "admin_password": "admin-password-123",
        "db_password": "db-password-123",
        "wecom_use_mock": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("credential_key", "", "CREDENTIAL_KEY"),
        ("admin_password", "CHANGE_ME", "ADMIN_PASSWORD"),
        ("db_password", "", "DB_PASSWORD"),
        ("wecom_use_mock", True, "WECOM_USE_MOCK"),
    ],
)
def test_prod_rejects_unsafe_values(field, value, message):
    with pytest.raises(ValidationError, match=message):
        prod_settings(**{field: value})


def test_dev_keeps_mock_and_empty_key_fallback():
    settings = Settings(app_env="dev", wecom_use_mock=True, credential_key="")
    assert settings.wecom_use_mock is True


def test_db_url_preserves_special_password_characters():
    settings = Settings(app_env="dev", db_password="p@ss:#/word")
    assert isinstance(settings.db_url, URL)
    assert settings.db_url.password == "p@ss:#/word"
