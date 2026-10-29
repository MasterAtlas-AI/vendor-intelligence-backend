from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRET = "change-me"  # noqa: S105 - sentinel that production startup rejects


class Settings(BaseSettings):
    """All runtime configuration. Values come from environment variables or `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["local", "dev", "staging", "prod", "test"] = "local"
    app_name: str = "MasterAtlas AI"
    app_version: str = "0.1.0"
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    log_json: bool = True

    # Database. The app role is subject to RLS; the migration role owns the schema.
    database_url: str = "postgresql+asyncpg://app_rw:app_rw@localhost:5432/masteratlas"
    migration_database_url: str = "postgresql+asyncpg://app_owner:app_owner@localhost:5432/masteratlas"
    app_db_role: str = "app_rw"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # Background jobs (Postgres-backed queue, see app.workers.runner)
    job_runner_enabled: bool = True  # run jobs inside the API process; false when using `app.cli run-worker`
    job_concurrency: int = 2
    job_poll_seconds: float = 1.0
    job_lease_seconds: int = 120
    maintenance_interval_minutes: int = 360

    # Imports. Uploaded files are stored in Postgres (uploads.data), so keep them modest.
    upload_max_bytes: int = 10 * 1024 * 1024
    import_max_rows: int = 5000
    pdf_max_pages: int = 300

    # Auth
    jwt_algorithm: Literal["HS256", "EdDSA", "RS256"] = "HS256"
    jwt_secret: SecretStr = SecretStr(_INSECURE_SECRET)
    jwt_private_key: SecretStr | None = None
    jwt_public_key: str | None = None
    jwt_key_id: str = "local-1"
    jwt_issuer: str = "masteratlas"
    jwt_audience: str = "masteratlas-api"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_days: int = 14
    refresh_cookie_name: str = "mai_rt"
    refresh_cookie_secure: bool = True
    refresh_cookie_domain: str | None = None
    # "strict" works when frontend and API share a registrable domain (app.x.com + api.x.com).
    # Use "none" (requires secure) when they don't, e.g. two *.onrender.com hosts (onrender.com is a public suffix).
    refresh_cookie_samesite: Literal["strict", "lax", "none"] = "strict"
    # A rotated refresh token replayed within this window is treated as a concurrent refresh (two tabs),
    # not as theft: the request fails, but the session family is not revoked.
    refresh_reuse_grace_seconds: int = 10
    invite_token_ttl_hours: int = 72
    password_reset_ttl_minutes: int = 30
    password_min_length: int = 12
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    login_rate_limit_per_minute: int = 10
    api_key_prefix: str = "mai_live"

    cors_origins: list[str] = ["http://localhost:3000"]
    frontend_url: str = "http://localhost:3000"

    email_backend: Literal["console", "smtp"] = "console"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    smtp_from: str = "MasterAtlas AI <no-reply@masteratlas.local>"

    @property
    def is_production(self) -> bool:
        return self.environment in {"staging", "prod"}

    @property
    def refresh_cookie_path(self) -> str:
        return f"{self.api_prefix}/auth"

    @model_validator(mode="after")
    def _check_production_safety(self) -> "Settings":
        if self.refresh_cookie_samesite == "none" and not self.refresh_cookie_secure:
            raise ValueError("REFRESH_COOKIE_SAMESITE=none requires REFRESH_COOKIE_SECURE=true")
        if not self.is_production:
            return self
        if self.jwt_algorithm == "HS256" and (
            self.jwt_secret.get_secret_value() == _INSECURE_SECRET
            or len(self.jwt_secret.get_secret_value()) < 32
        ):
            raise ValueError("JWT_SECRET must be set to a strong value (>= 32 chars) outside local/dev")
        if self.jwt_algorithm != "HS256" and not (self.jwt_private_key and self.jwt_public_key):
            raise ValueError("JWT_PRIVATE_KEY and JWT_PUBLIC_KEY are required for asymmetric JWT algorithms")
        if not self.refresh_cookie_secure:
            raise ValueError("REFRESH_COOKIE_SECURE must be true outside local/dev")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
