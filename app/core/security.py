"""Password hashing, opaque tokens, API keys and JWT handling."""

import hashlib
import hmac
import secrets
import string
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings

_hasher = PasswordHasher()  # Argon2id with library defaults (RFC 9106 low-memory profile)
_BASE62 = string.ascii_letters + string.digits

# Verified against when the user does not exist, so login timing doesn't reveal valid emails.
DUMMY_PASSWORD_HASH = _hasher.hash("dummy-password-for-timing-equalisation")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> tuple[bool, bool]:
    """Returns (is_valid, needs_rehash)."""
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    return True, _hasher.check_needs_rehash(password_hash)


def generate_opaque_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _random_base62(length: int) -> str:
    return "".join(secrets.choice(_BASE62) for _ in range(length))


@dataclass(frozen=True)
class GeneratedApiKey:
    prefix: str  # public identifier, stored in clear and shown in the UI
    key: str  # full secret, shown to the user exactly once
    key_hash: str


def generate_api_key(namespace: str) -> GeneratedApiKey:
    prefix = _random_base62(10)
    key = f"{namespace}_{prefix}_{_random_base62(40)}"
    return GeneratedApiKey(prefix=prefix, key=key, key_hash=sha256_hex(key))


def parse_api_key(raw: str, namespace: str) -> str | None:
    """Returns the key prefix, or None if the value isn't shaped like one of our keys."""
    if not raw.startswith(namespace + "_"):
        return None
    parts = raw[len(namespace) + 1 :].split("_")
    if len(parts) != 2 or len(parts[0]) != 10 or len(parts[1]) != 40:
        return None
    return parts[0]


class JwtManager:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        if settings.jwt_algorithm == "HS256":
            self._signing_key: Any = settings.jwt_secret.get_secret_value()
            self._verify_key: Any = self._signing_key
        else:
            assert settings.jwt_private_key and settings.jwt_public_key
            self._signing_key = settings.jwt_private_key.get_secret_value()
            self._verify_key = settings.jwt_public_key

    def encode_access_token(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        roles: list[str],
        platform_roles: list[str],
        perms_version: int,
    ) -> tuple[str, int]:
        now = datetime.now(UTC)
        ttl = self._s.access_token_ttl_seconds
        claims = {
            "sub": str(user_id),
            "tid": str(tenant_id) if tenant_id else None,
            "roles": roles,
            "prm": platform_roles,
            "pv": perms_version,
            "jti": secrets.token_hex(8),
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl)).timestamp()),
            "iss": self._s.jwt_issuer,
            "aud": self._s.jwt_audience,
            "typ": "access",
        }
        token = jwt.encode(
            claims, self._signing_key, algorithm=self._s.jwt_algorithm, headers={"kid": self._s.jwt_key_id}
        )
        return token, ttl

    def decode_access_token(self, token: str) -> dict[str, Any]:
        """Raises jwt.PyJWTError on any problem (expired, bad signature, wrong audience...)."""
        claims: dict[str, Any] = jwt.decode(
            token,
            self._verify_key,
            algorithms=[self._s.jwt_algorithm],
            audience=self._s.jwt_audience,
            issuer=self._s.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
        if claims.get("typ") != "access":
            raise jwt.InvalidTokenError("not an access token")
        return claims
