from __future__ import annotations

import base64
import hashlib
import hmac
from functools import lru_cache

from cryptography.fernet import Fernet

from app.config import get_settings


def token_hmac(raw_token: str) -> str:
    """Return the keyed digest used only for service-token authentication."""
    if not isinstance(raw_token, str) or not raw_token:
        raise ValueError("raw_token is required")
    key_value = get_settings().mcp_token_hmac_key
    if not key_value:
        raise RuntimeError("MCP_TOKEN_HMAC_KEY must be configured before issuing tokens")
    return hmac.new(
        key_value.encode("utf-8"),
        raw_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@lru_cache
def _fernet() -> Fernet:
    key_value = get_settings().mcp_token_plaintext_key
    if not key_value:
        raise RuntimeError(
            "MCP_TOKEN_PLAINTEXT_KEY must be configured before issuing tokens"
        )
    key = base64.urlsafe_b64encode(
        hashlib.sha256(key_value.encode("utf-8")).digest()
    )
    return Fernet(key)


def encrypt_token(raw_token: str) -> bytes:
    if not isinstance(raw_token, str) or not raw_token:
        raise ValueError("raw_token is required")
    return _fernet().encrypt(raw_token.encode("utf-8"))


def decrypt_token(ciphertext: bytes) -> str:
    if not isinstance(ciphertext, bytes) or not ciphertext:
        raise ValueError("ciphertext is required")
    return _fernet().decrypt(ciphertext).decode("utf-8")
