"""
凭证加解密 - AES (Fernet 对称加密)
- 主密钥存 .env 的 CREDENTIAL_KEY（一期PoC；生产改 KMS）
- 加密 tenant 的企微 secret
- 红线：不硬编码密钥，不输出明文到日志
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet

from .config import get_settings


@lru_cache
def _get_fernet() -> Fernet:
    """从 .env 取主密钥；开发环境可用固定兜底，生产配置校验会阻止该路径。"""
    key = get_settings().credential_key  # 见 config.py 新增
    if not key:
        # PoC 兜底：用固定测试密钥（仅本地，绝不生产用）
        key = "PoC_DEFAULT_KEY_DO_NOT_USE_IN_PRODUCTION_32bytes!"
    # Fernet 要 32 字节 base64 url-safe key
    digest = base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest())
    return Fernet(digest)


def encrypt_secret(plaintext: str) -> bytes:
    """加密企微 secret → bytes 存 tenant_config.secret_encrypted"""
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_secret(ciphertext: bytes) -> str:
    """解密 → 明文 secret（仅运行时用，不落日志）"""
    return _get_fernet().decrypt(ciphertext).decode("utf-8")
