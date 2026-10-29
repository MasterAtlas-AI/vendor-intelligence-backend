"""The vendor assistant: plan -> look up -> answer, grounded in the knowledge base.

1. Plan (one small AI call): which searches to run and which named vendors to look up.
2. Look up (no AI): the hybrid search (local query understanding, so no extra AI cost) and vendor records with
   approved facts, sources and the organization's own AVL status.
3. Answer (one AI call) from those records only, citing them as [V1], [V2]. Citations to anything outside the
   records are removed before the answer is returned, so a cited vendor is always one the user can open.

Two AI calls per message (about US$0.003), metered to the organization's AI budget like any other call.
The conversation is kept by the client; nothing is stored server-side.
"""

import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.service import AINotAvailable, AiService, CallContext
from app.core.exceptions import NotFound
from app.domain.enums import VendorStatus
from app.domain.normalization import normalize_website
from app.domain.principal import Principal
from app.modules.assistant.prompts import ANSWER, PLAN
from app.modules.assistant.schemas import (
    AssistantAnswer,
    AssistantPlan,
    ChatAction,
    ChatRequest,
    ChatResponse,
    CitedVendor,
    SuggestedAction,
)
from app.modules.search.schemas import SearchFilters, SearchRequest
from app.modules.search.service import SearchService
from app.modules.vendors.models import Vendor
from app.modules.vendors.schemas import VendorDetail
from app.modules.vendors.service import VendorQueryService

MAX_SEARCHES = 3
MAX_NAMED = 5
HITS_PER_SEARCH = 6
MAX_RECORDS = 12
HISTORY_MESSAGES = 10
REF = re.compile(r"\[(V\d{1,2})\]")


def _record(ref: str, v: VendorDetail) -> dict[str, Any]:
    """What the answer model sees about one vendor: approved facts only, compact."""
    return {
        "ref": ref,
        "name": v.display_name,
        "legal_name": v.legal_name if v.legal_name != v.display_name else None,
        "country": v.hq_country,
        "city": v.hq_city,
        "website": v.website,
        "description": v.description[:600] or None,
        "avl_status": v.tenant.avl_status.value if v.tenant else None,
        "tags": v.tenant.tags if v.tenant else [],
        "equipment": [
            " - ".join(
                x
                for x in (
                    e.category.name,
                    e.name if e.name != e.category.name else "",
                    e.model_series if e.model_series and e.model_series != e.name else "",
                    ", ".join(e.standards),
                )
                if x
            )
            for e in v.equipment[:25]
        ],
        "services": [
            s.category.name + (f" - {s.name}" if s.name != s.category.name else "") for s in v.services[:15]
        ],
        "certifications": [
            c.certification.name
            + (f" (no. {c.certificate_number})" if c.certificate_number else "")
            + (f", valid to {c.valid_to}" if c.valid_to else "")
            for c in v.certifications[:15]
        ],
        "industries": [i.name for i in v.industries],
        "projects": [
            ", ".join(str(x) for x in (p.project_name, p.facility_type, p.client, p.country, p.year) if x)
            for p in v.projects[:10]
        ],
    }


class AssistantService:
    def __init__(self, session: AsyncSession, principal: Principal, ai: AiService) -> None:
        self.s = session
        self.principal = principal
        self.ai = ai

    @property
    def _call(self) -> CallContext:
        return CallContext(self.principal.tenant_id, self.principal.user_id)

    async def _vendors_named(self, names: list[str]) -> list[uuid.UUID]:
        ids: list[uuid.UUID] = []
        for name in names[:MAX_NAMED]:
            name = name.strip()
            if len(name) < 2:
                continue
            vid = await self.s.scalar(
                select(Vendor.id)
                .where(
                    Vendor.status == VendorStatus.ACTIVE,
                    func.lower(Vendor.display_name).contains(name.lower()),
                )
                .order_by(func.length(Vendor.display_name))
                .limit(1)
            ) or await self.s.scalar(
                select(Vendor.id)
                .where(Vendor.status == VendorStatus.ACTIVE, func.similarity(Vendor.display_name, name) > 0.4)
                .order_by(func.similarity(Vendor.display_name, name).desc())
                .limit(1)
            )
            if vid and vid not in ids:
                ids.append(vid)
        return ids

    async def chat(self, req: ChatRequest) -> ChatResponse:
        if not self.ai.configured:
            raise AINotAvailable("AI is not configured on this server (set OPENAI_API_KEY)")
        history = req.messages[-HISTORY_MESSAGES:]
        # Earlier answers are shortened: they're context, and repeating them in full invites repetitive replies.
        conversation = [
            {
                "role": m.role,
                "content": m.content if m.role == "user" or m is history[-1] else m.content[:600],
            }
            for m in history
        ]
        vendors_q = VendorQueryService(self.s, self.principal)

        previous: list[VendorDetail] = []
        for vid in req.context_vendor_ids:
            try:
                previous.append(await vendors_q.detail(vid))
            except NotFound:
                continue

        plan = (
            await self.ai.run(
                PLAN,
                AssistantPlan,
                self._call,
                conversation=conversation,
                previous=[v.display_name for v in previous],
            )
        ).output

        ordered: list[uuid.UUID] = [v.id for v in previous]
        ordered += [v for v in await self._vendors_named(plan.vendor_names) if v not in ordered]
        searched: list[str] = []
        search = SearchService(self.s, self.principal, self.ai)
        filters = SearchFilters(my_vendors_only=plan.my_vendors_only and self.principal.tenant_id is not None)
        for query in [q.strip() for q in plan.search_queries if q.strip()][:MAX_SEARCHES]:
            searched.append(query)
            result = await search.search(
                SearchRequest(query=query, parser="local", filters=filters, limit=HITS_PER_SEARCH)
            )
            ordered += [h.vendor.id for h in result.results if h.vendor.id not in ordered]

        details: list[VendorDetail] = []
        for vid in ordered[:MAX_RECORDS]:
            try:
                details.append(await vendors_q.detail(vid))
            except NotFound:
                continue
        refs = {f"V{i}": v for i, v in enumerate(details, start=1)}

        answer = (
            await self.ai.run(
                ANSWER,
                AssistantAnswer,
                self._call,
                conversation=conversation,
                records=[_record(ref, v) for ref, v in refs.items()],
            )
        ).output
        text, removed = self._ground(answer.answer, refs)
        cited = [r for r in dict.fromkeys(REF.findall(text)) if r in refs]
        return ChatResponse(
            answer=text,
            vendors=[self._cited(r, refs[r]) for r in cited],
            follow_ups=[f.strip()[:200] for f in answer.follow_ups if f.strip()][:3],
            actions=self._actions(answer.actions, refs),
            searched=searched,
            removed_refs=removed,
        )

    @staticmethod
    def _actions(suggested: list[SuggestedAction], refs: dict[str, VendorDetail]) -> list[ChatAction]:
        """Only actions the app can perform, on vendors that were in the records; at most one of each target."""
        out: list[ChatAction] = []
        seen: set[str] = set()
        for a in suggested[:4]:
            if a.kind == "research_vendor" and (v := refs.get(a.ref.strip())) is not None:
                website = normalize_website(a.website) if a.website.strip() else None
                has_site = bool(v.website or v.domains)
                if (not has_site and website is None) or f"r:{v.id}" in seen:
                    continue  # nothing to research
                seen.add(f"r:{v.id}")
                out.append(
                    ChatAction(
                        kind="research_vendor",
                        label=f"Research {v.display_name}'s website",
                        vendor_id=v.id,
                        vendor_name=v.display_name,
                        website=None if has_site else website,
                    )
                )
            elif a.kind == "discover_vendors" and a.brief.strip() and "d" not in seen:
                seen.add("d")
                out.append(
                    ChatAction(
                        kind="discover_vendors",
                        label="Discover vendors on the web",
                        brief=a.brief.strip()[:1000],
                    )
                )
        return out

    @staticmethod
    def _cited(ref: str, v: VendorDetail) -> CitedVendor:
        return CitedVendor(
            ref=ref,
            vendor_id=v.id,
            name=v.display_name,
            country=v.hq_country,
            avl_status=v.tenant.avl_status.value if v.tenant else None,
            sources=[s.url for s in v.sources if s.url][:3],
        )

    @staticmethod
    def _ground(text: str, refs: dict[str, VendorDetail]) -> tuple[str, int]:
        """Drops citations to records that weren't provided (the model must not point at anything else)."""
        removed = 0

        def keep(m: re.Match[str]) -> str:
            nonlocal removed
            if m.group(1) in refs:
                return m.group(0)
            removed += 1
            return ""

        return re.sub(r"[ \t]{2,}", " ", REF.sub(keep, text)).strip(), removed
