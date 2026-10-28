"""Idempotent sync of the permission catalogue and system roles into the database."""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.permissions import PERMISSION_DESCRIPTIONS, TENANT_ROLES, P
from app.modules.users.models import Permission, Role, RolePermission


async def sync_rbac(session: AsyncSession) -> None:
    """Must run in a platform-mode session (system roles have tenant_id NULL). Caller commits."""
    existing = {p.key: p for p in await session.scalars(select(Permission))}
    for perm in P:
        row = existing.get(perm.value)
        if row is None:
            row = Permission(key=perm.value, description=PERMISSION_DESCRIPTIONS[perm])
            session.add(row)
            existing[perm.value] = row
        else:
            row.description = PERMISSION_DESCRIPTIONS[perm]
    await session.flush()

    for key, (name, perms) in TENANT_ROLES.items():
        role = await session.scalar(select(Role).where(Role.tenant_id.is_(None), Role.key == key))
        if role is None:
            role = Role(tenant_id=None, key=key, name=name, is_system=True)
            session.add(role)
            await session.flush()
        role.name = name
        await session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        session.add_all(RolePermission(role_id=role.id, permission_id=existing[p.value].id) for p in perms)
    await session.flush()
