from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


_HASHER = PasswordHasher()
_MIN_PASSWORD_LENGTH = 12
_MAX_PASSWORD_LENGTH = 256


def validate_password(raw: str) -> None:
    if (
        not isinstance(raw, str)
        or raw != raw.strip()
        or not _MIN_PASSWORD_LENGTH <= len(raw) <= _MAX_PASSWORD_LENGTH
        or "password" in raw.lower()
    ):
        raise ValueError("password does not meet requirements")


def hash_password(raw: str) -> str:
    validate_password(raw)
    return _HASHER.hash(raw)


def verify_password(hash_value: str, raw: str) -> bool:
    if not isinstance(hash_value, str) or not isinstance(raw, str):
        return False
    try:
        return _HASHER.verify(hash_value, raw)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False
