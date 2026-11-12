"""The vendor assistant: plan -> look up -> answer, grounded in the knowledge base.

1. Plan (one small AI call): which searches to run and which named vendors to look up.
2. Look up (no AI): the hybrid search (local query understanding, so no extra AI cost) and vendor records with
   approved facts, sources and the organization's own AVL status.
3. Answer (one AI call, with web search) from those records, citing them as [V1], [V2], plus a separate "From the
   web" part citing the pages found as [W1], [W2]. Citations to records that weren't given, or to pages the search
   didn't return, are removed before the answer is returned.
4. Workspace (no AI): the requirement understood (the searches' merged intent), a shortlist of the records with a
   0-1 score per dimension and flags for gaps, and the steps taken with their timings.

Two AI calls per message (about US$0.01-0.02 with the web search), metered to the organization's AI budget.
Messages that aren't about vendors skip the web search.
The client sends the recent conversation with each question; each question and its answer are also saved to the
user's chat history (`AssistantConversation`), so chats can be reopened, renamed and deleted.
"""

import re
import time
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.ports import Citation
from app.ai.service import AINotAvailable, AiService, CallContext
from app.core.exceptions import NotFound
from app.domain.enums import VendorStatus
from app.domain.normalization import normalize_website, registrable_domain
from app.domain.permissions import P
from app.domain.principal import Principal
from app.modules.assistant.models import AssistantConversation, AssistantMessage
from app.modules.assistant.prompts import ANSWER, PLAN
from app.modules.assistant.schemas import (
    AgentStep,
    AssistantAnswer,
    AssistantPlan,
    ChatAction,
    ChatRequest,
    ChatResponse,
    CitedVendor,
    ConversationDetail,
    DimensionScores,
    DocumentMatch,
    HistoryMatch,
    SavedMessage,
    ShortlistRow,
    SuggestedAction,
    WebPage,
    WebSource,
)
from app.modules.equipment.documents import DocumentHit, find_documents
from app.modules.equipment.schemas import EquipmentSearch, ProjectEquipmentRead
from app.modules.equipment.service import EquipmentService
from app.modules.search.schemas import SearchFilters, SearchHit, SearchIntent, SearchRequest
from app.modules.search.service import SearchService
from app.modules.tenants.policy import ai_controls
from app.modules.vendors.models import Vendor
from app.modules.vendors.schemas import VendorDetail
from app.modules.vendors.service import VendorQueryService

MAX_SEARCHES = 3
MAX_NAMED = 5
HITS_PER_SEARCH = 6
MAX_RECORDS = 12
HISTORY_MESSAGES = 10
REF = re.compile(r"\[(V\d{1,2})\]")
WEB_REF = re.compile(r"\[(W\d{1,2})\]")
HIST_REF = re.compile(r"\[(H\d{1,2})\]")  # the organization's own equipment lines
DOC_REF = re.compile(r"\[(D\d{1,2})\]")  # and its project documents
DOCUMENTS = 5
HISTORY_LINES = 10
# Follow-ups must be the user's next questions, not the assistant offering to do something.
OFFER = re.compile(r"^(do you want|would you like|should i|shall i|can i|may i|want me)\b", re.IGNORECASE)
TITLE_CHARS = 80


def _title(question: str) -> str:
    """A saved chat's first title: its first question, shortened at a word (no AI call; the user can rename it)."""
    text = " ".join(question.split())
    if len(text) <= TITLE_CHARS:
        return text
    return text[:TITLE_CHARS].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def _record(
    ref: str, v: VendorDetail, private: bool = False, history: tuple[int, int] | None = None
) -> dict[str, Any]:
    """What the answer model sees about one vendor: approved facts only, compact. The organization's own data (AVL
    status, tags, project history) is included only when it allows sharing private data with the AI provider."""
    record = {
        "ref": ref,
        "name": v.display_name,
        "legal_name": v.legal_name if v.legal_name != v.display_name else None,
        "country": v.hq_country,
        "city": v.hq_city,
        "website": v.website,
        "description": v.description[:600] or None,
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
    if private:
        record["avl_status"] = v.tenant.avl_status.value if v.tenant else None
        record["tags"] = v.tenant.tags if v.tenant else []
        if history and history[0]:
            record["our_history"] = f"used on {history[0]} of our projects ({history[1]} equipment lines)"
    return record


AVL_SCORE = {"approved": 1.0, "conditional": 0.6, "unassessed": 0.3, "not_approved": 0.0, "blacklisted": 0.0}
REFERENCES_FOR_FULL_SCORE = 5
OUR_PROJECTS_FOR_FULL_SCORE = (
    3  # used on this many of the organization's own projects = full experience score
)


def _plan_summary(plan: AssistantPlan, previous: list[VendorDetail]) -> str:
    kind = {
        "find_vendors": "Find vendors",
        "vendor_details": "Vendor details",
        "compare": "Compare vendors",
        "knowledge_question": "Question",
        "out_of_scope": "Not about vendors",
    }.get(plan.intent, plan.intent.replace("_", " ").capitalize())
    parts = [kind]
    if plan.vendor_names:
        parts.append("named: " + ", ".join(plan.vendor_names[:MAX_NAMED]))
    if previous:
        parts.append(f"{len(previous)} vendor{'s' if len(previous) != 1 else ''} from the previous answer")
    if plan.my_vendors_only:
        parts.append("your vendors only")
    return "; ".join(parts)


def _merge_intents(intents: list[SearchIntent]) -> SearchIntent | None:
    """One requirement from the searches run: every equipment type, standard, country... any of them asked for."""
    if not intents:
        return None

    def union(field: str) -> list[str]:
        return list(dict.fromkeys(x for i in intents for x in getattr(i, field)))

    return SearchIntent(
        equipment_categories=union("equipment_categories"),
        service_categories=union("service_categories"),
        industries=union("industries"),
        certifications=union("certifications"),
        standards=union("standards"),
        countries=union("countries"),
        keywords=" · ".join(dict.fromkeys(i.keywords.strip() for i in intents if i.keywords.strip()))[:300],
    )


def _shortlist_row(
    ref: str,
    v: VendorDetail,
    hit: SearchHit | None,
    req: SearchIntent | None,
    history: tuple[int, int] | None = None,
    has_history: bool = False,
) -> ShortlistRow:
    s = hit.scores if hit else None
    relevance = [x for x in (s.semantic, s.text) if x is not None] if s else []
    references = len(v.projects)
    fpso = sum(1 for p in v.projects if p.facility_type and "fpso" in p.facility_type.lower())
    avl = v.tenant.avl_status.value if v.tenant else None
    data_quality = s.data_quality if s else float(v.data_quality_score or 0)

    flags: list[str] = []
    if avl == "blacklisted":
        flags.append("Blacklisted by your organization")
    elif avl == "not_approved":
        flags.append("Not approved on your vendor list")
    if req and req.standards:
        have = {x.lower().replace(" ", "") for e in v.equipment for x in e.standards}
        have |= {c.certification.name.lower().replace(" ", "") for c in v.certifications}
        missing = [x for x in req.standards if not any(x.lower().replace(" ", "") in h for h in have)]
        if missing:
            flags.append(f"{', '.join(missing)} not on record")
    if not references:
        flags.append("No project references on record")
    if not v.certifications:
        flags.append("No certifications on record")
    elif any(c.valid_to and c.valid_to < date.today() for c in v.certifications):
        flags.append("A certificate has expired")
    if not (v.website or v.domains):
        flags.append("No website on record")
    if data_quality < 0.4:
        flags.append("Sparse record")

    def clamp(x: float | None) -> float | None:
        return None if x is None else round(max(0.0, min(1.0, x)), 3)

    return ShortlistRow(
        vendor_id=v.id,
        ref=ref,
        name=v.display_name,
        country=v.hq_country,
        city=v.hq_city,
        website=v.website,
        avl_status=avl,
        scores=DimensionScores(
            relevance=clamp(max(relevance)) if relevance else None,
            equipment=clamp(s.equipment) if s else None,
            standards=clamp(s.standards_certifications) if s else None,
            industry=clamp(s.industries) if s else None,
            track_record=round(min(1.0, (references + fpso) / REFERENCES_FOR_FULL_SCORE), 3),
            data_quality=clamp(data_quality) or 0.0,
            approved_vendor=AVL_SCORE.get(avl or "unassessed", 0.3),
            our_experience=round(min(1.0, (history or (0, 0))[0] / OUR_PROJECTS_FOR_FULL_SCORE), 3)
            if has_history
            else None,
        ),
        matched=[r.label[:160] for r in (hit.reasons if hit else [])][:4],
        products=len(v.equipment),
        references=references,
        fpso_references=fpso,
        certifications=len(v.certifications),
        our_projects=(history or (0, 0))[0],
        our_equipment=(history or (0, 0))[1],
        flags=flags,
    )


def _history_record(ref: str, e: ProjectEquipmentRead) -> dict[str, Any]:
    """One of the organization's own equipment lines, as the answer model sees it (only when sharing is allowed)."""
    return {
        "ref": ref,
        "equipment": e.name,
        "tag": e.tag or None,
        "category": e.category_name,
        "vendor": e.vendor_name or None,
        "model": e.model or None,
        "project": f"{e.project.name} ({e.project.year})" if e.project.year else e.project.name,
        "attributes": e.attributes,
        "verification": e.verification,
    }


def _document_record(ref: str, d: DocumentHit) -> dict[str, Any]:
    """One of the organization's documents as the answer model sees it: the matching passage, not the file."""
    return {
        "ref": ref,
        "project": d.project_name,
        "type": d.doc_type,
        "number": d.doc_number,
        "title": d.title,
        "date": d.doc_date.isoformat() if d.doc_date else None,
        "equipment": d.equipment_label or None,
        "vendor": d.vendor_name or None,
        "passage": d.snippet.replace("[[", "").replace("]]", "") or None,
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
        # Checked before any AI call, so a stale or foreign chat id costs nothing.
        saved = await self._owned(req.conversation_id) if req.conversation_id else None
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
        started_at = datetime.now(UTC)
        steps: list[AgentStep] = []
        clock = time.perf_counter()

        def step(key: str, label: str, detail: str) -> None:
            nonlocal clock
            now = time.perf_counter()
            steps.append(AgentStep(key=key, label=label, detail=detail, ms=round((now - clock) * 1000)))
            clock = now

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
        step("understand", "Understand the request", _plan_summary(plan, previous))

        ordered: list[uuid.UUID] = [v.id for v in previous]
        ordered += [v for v in await self._vendors_named(plan.vendor_names) if v not in ordered]
        searched: list[str] = []
        hits: dict[uuid.UUID, SearchHit] = {}
        intents: list[SearchIntent] = []
        search = SearchService(self.s, self.principal, self.ai)
        filters = SearchFilters(my_vendors_only=plan.my_vendors_only and self.principal.tenant_id is not None)
        for query in [q.strip() for q in plan.search_queries if q.strip()][:MAX_SEARCHES]:
            searched.append(query)
            result = await search.search(
                SearchRequest(query=query, parser="local", filters=filters, limit=HITS_PER_SEARCH)
            )
            intents.append(result.intent)
            for h in result.results:
                if h.vendor.id not in hits or h.confidence > hits[h.vendor.id].confidence:
                    hits[h.vendor.id] = h
            ordered += [h.vendor.id for h in result.results if h.vendor.id not in ordered]
        if searched:
            step(
                "search_kb",
                "Search the knowledge base",
                f"{len(searched)} search{'es' if len(searched) > 1 else ''}, {len(hits)} vendor"
                f"{'s' if len(hits) != 1 else ''} matched",
            )

        details: list[VendorDetail] = []
        for vid in ordered[:MAX_RECORDS]:
            try:
                details.append(await vendors_q.detail(vid))
            except NotFound:
                continue
        refs = {f"V{i}": v for i, v in enumerate(details, start=1)}
        step(
            "load_records",
            "Load vendor records",
            f"{len(details)} record{'s' if len(details) != 1 else ''} with approved facts and sources",
        )

        # The organization's own history is matched here, locally; it reaches the AI only if the organization allows.
        controls = await ai_controls(self.s, self.principal.tenant_id)
        has_history = self.principal.tenant_id is not None and self.principal.has(P.EQUIPMENT_READ)
        our_history = (
            await EquipmentService(self.s, self.principal).history_counts([v.id for v in details])
            if has_history
            else {}
        )
        # SOW 10: internal knowledge first. Equipment lines from the organization's own projects that match the request,
        # found locally; shown beside the answer, and sent to the AI only when the organization allows it.
        requirement = _merge_intents(intents)
        lines = await self._history_lines(requirement, details, our_history) if has_history else []
        hist_refs = {f"H{i}": e for i, e in enumerate(lines, start=1)}
        if has_history:
            step(
                "history",
                "Search your project history",
                f"{len(lines)} matching equipment line{'s' if len(lines) != 1 else ''} in your projects"
                + ("" if controls.share_private_data or not lines else " (kept on the server)"),
            )
        # SOW 25.4: the organization's documents (reports, letters, datasheets) whose words match, searched locally.
        found_docs: list[DocumentHit] = []
        if has_history:
            words = " ".join(searched) or req.messages[-1].content
            found_docs, _ = await find_documents(self.s, words[:400], any_word=True, limit=DOCUMENTS)
            step(
                "documents",
                "Search your project documents",
                f"{len(found_docs)} matching document{'s' if len(found_docs) != 1 else ''}"
                + ("" if controls.share_private_data or not found_docs else " (kept on the server)"),
            )
        doc_refs = {f"D{i}": d for i, d in enumerate(found_docs, start=1)}
        shared = hist_refs if controls.share_private_data else {}
        shared_docs = doc_refs if controls.share_private_data else {}
        web = plan.intent != "out_of_scope"
        answered = await self.ai.run(
            ANSWER,
            AssistantAnswer,
            self._call,
            web_search=web,
            allowed_domains=controls.allowed_domains(),
            conversation=conversation,
            records=[
                _record(ref, v, controls.share_private_data, our_history.get(v.id)) for ref, v in refs.items()
            ],
            history=[_history_record(ref, e) for ref, e in shared.items()],
            documents=[_document_record(ref, d) for ref, d in shared_docs.items()],
        )
        answer = answered.output
        pages = self._web_pages(answer.web_sources, answered.citations)
        text, removed = self._ground(answer.answer, refs, pages, {**shared, **shared_docs})
        cited = [r for r in dict.fromkeys(REF.findall(text)) if r in refs]
        web_cited = [pages[r] for r in dict.fromkeys(WEB_REF.findall(text)) if r in pages]
        step(
            "answer",
            "Search the web and write the answer" if web else "Write the answer",
            (f"{len(web_cited)} web page{'s' if len(web_cited) != 1 else ''} cited, " if web else "")
            + f"{len(cited)} vendor record{'s' if len(cited) != 1 else ''} cited",
        )
        step(
            "check",
            "Check citations",
            f"Removed {removed} unsupported citation{'s' if removed != 1 else ''}"
            if removed
            else "Every citation points at a record or a page the search returned",
        )
        shortlist = [
            _shortlist_row(ref, v, hits.get(v.id), requirement, our_history.get(v.id), has_history)
            for ref, v in refs.items()
        ]
        if shortlist:
            step("rank", "Rank the shortlist", f"{len(shortlist)} vendors scored on up to 7 dimensions")
        reply = ChatResponse(
            answer=text,
            web_sources=web_cited,
            vendors=[self._cited(r, refs[r]) for r in cited],
            follow_ups=[
                f.strip()[:200] for f in answer.follow_ups if f.strip() and not OFFER.match(f.strip())
            ][:3],
            actions=self._actions(answer.actions, refs),
            searched=searched,
            removed_refs=removed,
            requirement=requirement,
            shortlist=shortlist,
            steps=steps,
            started_at=started_at,
            private_data_shared=controls.share_private_data,
            web_limited_to=controls.allowed_domains() or [],
            history_matches=[
                HistoryMatch(
                    ref=ref,
                    equipment_id=e.id,
                    tag=e.tag,
                    name=e.name,
                    category_name=e.category_name,
                    project_name=e.project.name,
                    project_year=e.project.year,
                    vendor_id=e.vendor_id,
                    vendor_name=e.vendor_name,
                    verification=e.verification,
                )
                for ref, e in hist_refs.items()
            ],
            document_matches=[
                DocumentMatch(
                    ref=ref,
                    document_id=d.id,
                    project_id=d.project_id,
                    project_name=d.project_name,
                    doc_type=d.doc_type,
                    doc_number=d.doc_number,
                    title=d.title,
                    snippet=d.snippet,
                )
                for ref, d in doc_refs.items()
            ],
        )
        return await self._save(saved, req.messages[-1].content, reply)

    async def _history_lines(
        self,
        requirement: SearchIntent | None,
        details: list[VendorDetail],
        counts: dict[uuid.UUID, tuple[int, int]],
    ) -> list[ProjectEquipmentRead]:
        """The organization's equipment lines for the requested categories, else for the shortlisted vendors it used."""
        svc = EquipmentService(self.s, self.principal)
        found: dict[uuid.UUID, ProjectEquipmentRead] = {}
        for code in (requirement.equipment_categories if requirement else [])[:3]:
            for e in (await svc.search(EquipmentSearch(category_code=code, limit=HISTORY_LINES))).items:
                found.setdefault(e.id, e)
        if not found:
            for v in [v for v in details if counts.get(v.id, (0, 0))[0]][:3]:
                for e in (await svc.search(EquipmentSearch(vendor_id=v.id, limit=4))).items:
                    found.setdefault(e.id, e)
        return list(found.values())[:HISTORY_LINES]

    # ------------------------------------------------------------------ saved chats

    async def _owned(self, conversation_id: uuid.UUID) -> AssistantConversation:
        row = await self.s.get(AssistantConversation, conversation_id)
        if row is None or self.principal.user_id is None or row.user_id != self.principal.user_id:
            raise NotFound("Chat not found")
        return row

    async def _save(
        self, saved: AssistantConversation | None, question: str, reply: ChatResponse
    ) -> ChatResponse:
        """Adds the question and its answer to the user's chat (a new one if none was given). API keys have no
        user, so their chats aren't saved."""
        if self.principal.user_id is None:
            return reply
        if saved is None:
            saved = AssistantConversation(
                tenant_id=self.principal.tenant_id, user_id=self.principal.user_id, title=_title(question)
            )
            self.s.add(saved)
            await self.s.flush()
        reply.conversation_id = saved.id
        self.s.add_all([
            AssistantMessage(conversation_id=saved.id, tenant_id=saved.tenant_id, role="user", content=question),
            AssistantMessage(conversation_id=saved.id, tenant_id=saved.tenant_id, role="assistant",
                             content=reply.answer, reply=reply.model_dump(mode="json")),
        ])  # fmt: skip
        saved.updated_at = datetime.now(UTC)  # most recently used chats first
        await self.s.commit()
        return reply

    async def conversations(self, limit: int) -> list[AssistantConversation]:
        return list(
            await self.s.scalars(
                select(AssistantConversation)
                .where(AssistantConversation.user_id == self.principal.user_id)
                .order_by(AssistantConversation.updated_at.desc())
                .limit(limit)
            )
        )

    async def conversation(self, conversation_id: uuid.UUID) -> ConversationDetail:
        row = await self._owned(conversation_id)
        messages = await self.s.scalars(
            select(AssistantMessage)
            .where(AssistantMessage.conversation_id == row.id)
            .order_by(AssistantMessage.created_at, AssistantMessage.id)
        )
        return ConversationDetail(
            id=row.id,
            title=row.title,
            created_at=row.created_at,
            updated_at=row.updated_at,
            messages=[SavedMessage.model_validate(m) for m in messages],
        )

    async def rename(self, conversation_id: uuid.UUID, title: str) -> AssistantConversation:
        row = await self._owned(conversation_id)
        row.title = " ".join(title.split())[:200] or row.title
        await self.s.commit()
        await self.s.refresh(row)
        return row

    async def delete(self, conversation_id: uuid.UUID) -> None:
        await self.s.delete(await self._owned(conversation_id))
        await self.s.commit()

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
                        label="Add new suppliers to the knowledge base",
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
    def _web_pages(listed: list[WebPage], citations: list[Citation]) -> dict[str, WebSource]:
        """The web pages the answer may cite: only those on a site the search actually returned."""
        searched = {registrable_domain(c.url) for c in citations} - {None}
        pages: dict[str, WebSource] = {}
        for p in listed[:10]:
            ref, url = p.ref.strip().strip("[]"), p.url.strip()
            if (
                not WEB_REF.fullmatch(f"[{ref}]")
                or not url.startswith(("http://", "https://"))
                or ref in pages
            ):
                continue
            if registrable_domain(url) in searched:
                title = p.title.strip() or next((c.title for c in citations if c.url == url), "") or url
                pages[ref] = WebSource(ref=ref, title=title[:200], url=url[:1000])
        return pages

    @staticmethod
    def _ground(
        text: str,
        refs: dict[str, VendorDetail],
        pages: dict[str, WebSource] | None = None,
        history: dict[str, Any] | None = None,
    ) -> tuple[str, int]:
        """Drops citations to records that weren't provided and to pages the search didn't return (the model must
        not point at anything else)."""
        removed = 0
        known = set(refs) | set(pages or {}) | set(history or {})

        def keep(m: re.Match[str]) -> str:
            nonlocal removed
            if m.group(1) in known:
                return m.group(0)
            removed += 1
            return ""

        text = DOC_REF.sub(keep, HIST_REF.sub(keep, WEB_REF.sub(keep, REF.sub(keep, text))))
        return re.sub(r"[ \t]{2,}", " ", text).strip(), removed
