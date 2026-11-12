"""An organization's AI and public-source controls (SOW 7, 21, 24), kept in `tenants.settings["ai"]`.

- share_private_data: whether the organization's own data (AVL status, tags, project history) may be included in
  prompts sent to the AI provider. Off by default: answers then use the shared knowledge base and public web only,
  and the organization's history is matched locally and shown beside the answer.
- web_sources: "open" (any public site) or "approved_only" (web searches limited to `approved_domains`).
"""

import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.normalization import registrable_domain
from app.modules.tenants.models import Tenant


class AiControls(BaseModel):
    model_config = ConfigDict(extra="forbid")
    share_private_data: bool = False
    web_sources: Literal["open", "approved_only"] = "open"
    approved_domains: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("approved_domains")
    @classmethod
    def _domains(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for value in values:
            domain = registrable_domain(value.strip()) if value.strip() else None
            if not domain or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
                raise ValueError(f"not a website domain: {value!r}")
            if domain not in out:
                out.append(domain)
        return out

    def allowed_domains(self) -> list[str] | None:
        """Domains to limit AI web searches to, or None for no limit."""
        return (
            self.approved_domains if self.web_sources == "approved_only" and self.approved_domains else None
        )


def controls_from(settings: dict[str, object] | None) -> AiControls:
    raw = (settings or {}).get("ai")
    try:
        return AiControls.model_validate(raw) if isinstance(raw, dict) else AiControls()
    except ValueError:
        return AiControls()


async def ai_controls(session: AsyncSession, tenant_id: uuid.UUID | None) -> AiControls:
    """The organization's controls (defaults for platform staff outside an organization)."""
    if tenant_id is None:
        return AiControls()
    tenant = await session.get(Tenant, tenant_id)
    return controls_from(tenant.settings if tenant else None)
