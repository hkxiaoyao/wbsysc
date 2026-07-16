from __future__ import annotations

import hashlib
import hmac

from ..config import get_settings
from ..crypto import decrypt_secret, encrypt_secret


def encrypt_credential(plaintext: str) -> bytes:
    """Encrypt a third-party credential with the established credential cipher."""
    return encrypt_secret(plaintext)


def decrypt_credential(ciphertext: bytes) -> str:
    """Decrypt a stored third-party credential for in-memory use only."""
    return decrypt_secret(ciphertext)


def token_hmac(raw_token: str) -> str:
    """Return the keyed digest used to persist an MCP token."""
    if not isinstance(raw_token, str) or not raw_token:
        raise ValueError("raw_token is required")
    key_value = get_settings().mcp_token_hmac_key
    if not key_value:
        raise RuntimeError("MCP_TOKEN_HMAC_KEY must be configured before issuing tokens")
    key = key_value.encode("utf-8")
    return hmac.new(key, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()
