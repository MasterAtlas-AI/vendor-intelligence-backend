"""Pure unit tests (no database)."""

import uuid
from datetime import UTC, datetime

import jwt
import pytest

from app.core.config import Settings
from app.core.exceptions import ValidationFailed
from app.core.ids import uuid7
from app.core.pagination import decode_cursor, encode_cursor
from app.core.security import JwtManager, generate_api_key, hash_password, parse_api_key, verify_password
from app.domain.permissions import PLATFORM_ROLES, TENANT_ROLES, P
from app.domain.principal import Principal
from app.modules.auth.service import validate_password_policy

SETTINGS = Settings(
    _env_file=None, environment="test", jwt_secret="unit-test-secret-long-enough-for-hs256-xx"
)  # type: ignore[arg-type]


def test_password_hash_roundtrip() -> None:
    h = hash_password("Correct-Horse-1")
    assert verify_password(h, "Correct-Horse-1") == (True, False)
    assert verify_password(h, "wrong")[0] is False
    assert verify_password("not-a-hash", "x")[0] is False


def test_jwt_roundtrip_and_tampering() -> None:
    mgr = JwtManager(SETTINGS)
    uid, tid = uuid.uuid4(), uuid.uuid4()
    token, ttl = mgr.encode_access_token(
        user_id=uid, tenant_id=tid, roles=["viewer"], platform_roles=[], perms_version=1
    )
    claims = mgr.decode_access_token(token)
    assert claims["sub"] == str(uid) and claims["tid"] == str(tid) and ttl == 900
    with pytest.raises(jwt.PyJWTError):
        mgr.decode_access_token(token[:-2] + "xx")
    other = JwtManager(Settings(environment="test", jwt_secret="a-different-secret-that-is-long-enough!"))  # type: ignore[arg-type]
    with pytest.raises(jwt.PyJWTError):
        other.decode_access_token(token)


def test_api_key_format() -> None:
    k = generate_api_key("mai_live")
    assert parse_api_key(k.key, "mai_live") == k.prefix
    assert parse_api_key("mai_live_short_x", "mai_live") is None
    assert parse_api_key("other_" + k.key, "mai_live") is None


def test_uuid7_is_time_ordered_and_valid() -> None:
    ids = [uuid7() for _ in range(200)]
    assert all(u.version == 7 for u in ids)
    assert [u.bytes[:6] for u in ids] == sorted(u.bytes[:6] for u in ids)


def test_cursor_roundtrip_and_garbage() -> None:
    ts, i = datetime.now(UTC), uuid.uuid4()
    assert decode_cursor(encode_cursor(ts, i)) == (ts, i)
    with pytest.raises(ValidationFailed):
        decode_cursor("!!garbage!!")


@pytest.mark.parametrize(
    ("password", "ok"),
    [
        ("Short1!", False),
        ("alllowercaseletters", False),
        ("Long-Enough-Pass1", True),
        ("jane.doe-Pass123!", False),
        ("Ab-cdefgh-1234", True),  # short local part "a" must not ban the letter a
    ],
)
def test_password_policy(password: str, ok: bool) -> None:
    email = "a@example.com" if password.startswith("Ab-") else "jane.doe@example.com"
    if ok:
        validate_password_policy(password, email=email, min_length=12)
    else:
        with pytest.raises(ValidationFailed):
            validate_password_policy(password, email=email, min_length=12)


def test_role_bundles_are_monotonic() -> None:
    viewer, engineer, manager, admin = (
        TENANT_ROLES[r][1] for r in ("viewer", "engineer", "manager", "tenant_admin")
    )
    assert viewer < engineer < manager < admin
    assert PLATFORM_ROLES["super_admin"] == frozenset(P)
    assert P.TENANT_MANAGE not in admin and P.KNOWLEDGE_APPROVE not in admin


def test_tenant_context_from_principal() -> None:
    tid = uuid.uuid4()
    sa = Principal(
        kind="user", id=uuid.uuid4(), tenant_id=None, permissions=frozenset(), platform_roles=("super_admin",)
    )
    assert sa.tenant_context().is_platform
    curator = Principal(
        kind="user", id=uuid.uuid4(), tenant_id=None, permissions=frozenset(), platform_roles=("curator",)
    )
    assert not curator.tenant_context().is_platform
    acting = Principal(
        kind="user",
        id=uuid.uuid4(),
        tenant_id=tid,
        permissions=frozenset(),
        platform_roles=("super_admin",),
        impersonating=True,
    )
    ctx = acting.tenant_context()
    assert ctx.tenant_id == tid and not ctx.is_platform


def test_production_settings_reject_weak_secrets() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(_env_file=None, environment="prod")  # type: ignore[call-arg]


def test_samesite_none_requires_secure_cookie() -> None:
    with pytest.raises(ValueError, match="SAMESITE"):
        Settings(_env_file=None, refresh_cookie_samesite="none", refresh_cookie_secure=False)  # type: ignore[call-arg]
