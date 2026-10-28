"""Operational commands. Run `python -m app.cli --help` for the list."""

import asyncio
import getpass

import typer
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.exceptions import ValidationFailed
from app.core.security import hash_password
from app.db.roles import ensure_app_role, generate_db_password, runtime_database_url
from app.db.session import build_engine, build_sessionmaker, open_session
from app.domain.enums import UserStatus
from app.domain.permissions import PLATFORM_ROLES
from app.domain.principal import TenantContext
from app.modules.auth.service import validate_password_policy
from app.modules.users.models import User
from app.modules.users.rbac import sync_rbac

cli = typer.Typer(no_args_is_help=True)

AUDIT_MONTHS_AHEAD = 3


@cli.command("create-app-role")
def create_app_role(role: str = typer.Option(None, help="Defaults to APP_DB_ROLE")) -> None:
    """Create the RLS-restricted runtime role (or rotate its password). Uses MIGRATION_DATABASE_URL.

    Prints the DATABASE_URL to put in .env / the service environment.
    """
    settings = get_settings()
    role = role or settings.app_db_role
    password = generate_db_password()
    asyncio.run(ensure_app_role(settings, role, password))
    typer.echo(f"Role {role} is ready. Set this in your environment (shown once):\n")
    typer.echo(f"DATABASE_URL={runtime_database_url(settings, role, password)}")


async def _bootstrap() -> None:
    engine = build_engine(get_settings(), null_pool=True)
    try:
        async with open_session(build_sessionmaker(engine), TenantContext.platform()) as s:
            await sync_rbac(s)
            await s.execute(
                text(
                    "SELECT ensure_audit_partition((now() + (n || ' month')::interval)::date) "
                    "FROM generate_series(0, :n) AS n"
                ),
                {"n": AUDIT_MONTHS_AHEAD},
            )
            await s.commit()
    finally:
        await engine.dispose()


@cli.command()
def bootstrap() -> None:
    """Sync permissions/system roles and pre-create audit partitions. Idempotent; run after every migration."""
    asyncio.run(_bootstrap())
    typer.echo("RBAC catalogue synchronised; audit partitions ensured.")


async def _create_platform_user(email: str, name: str, password: str, role: str) -> None:
    engine = build_engine(get_settings(), null_pool=True)
    try:
        async with open_session(build_sessionmaker(engine), TenantContext.platform()) as s:
            user = await s.scalar(select(User).where(User.email == email))
            if user is not None and user.tenant_id is not None:
                raise typer.BadParameter(f"{email} belongs to a tenant; platform users must not")
            if user is None:
                user = User(email=email, full_name=name, tenant_id=None)
                s.add(user)
            user.platform_roles = sorted({*(user.platform_roles or []), role})
            user.password_hash = hash_password(password)
            user.status = UserStatus.ACTIVE
            user.email_verified = True
            await s.commit()
    finally:
        await engine.dispose()


@cli.command("create-platform-user")
def create_platform_user(
    email: str = typer.Option(...),
    name: str = typer.Option(...),
    role: str = typer.Option("super_admin", help=f"One of: {', '.join(PLATFORM_ROLES)}"),
) -> None:
    """Create (or promote) a platform user: super_admin or curator. Prompts for the password."""
    if role not in PLATFORM_ROLES:
        raise typer.BadParameter(f"role must be one of {list(PLATFORM_ROLES)}")
    try:
        # Same validation as the login endpoint, so the account can actually sign in.
        email = TypeAdapter(EmailStr).validate_python(email)
    except ValidationError as exc:
        raise typer.BadParameter(f"invalid email: {exc.errors()[0]['msg']}") from exc
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        raise typer.BadParameter("Passwords do not match")
    try:
        validate_password_policy(password, email=email, min_length=get_settings().password_min_length)
    except ValidationFailed as exc:
        raise typer.BadParameter("; ".join(e["message"] for e in exc.errors or [])) from exc
    asyncio.run(_create_platform_user(email, name, password, role))
    typer.echo(f"Platform user {email} ready with role {role}.")


if __name__ == "__main__":
    cli()
