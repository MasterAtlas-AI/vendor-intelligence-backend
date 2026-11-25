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

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.ports import Citation
from app.ai.service import AINotAvailable, AiService, CallContext
from app.core.exceptions import NotFound
from app.db.session import open_session
from app.domain.enums import AvlStatus, VendorStatus
from app.domain.normalization import normalize_country, normalize_name, normalize_website, registrable_domain
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
    WebVendor,
    WebVendorFound,
)
from app.modules.equipment.documents import DocumentHit, find_documents
from app.modules.equipment.schemas import EquipmentSearch, ProjectEquipmentRead
from app.modules.equipment.service import EquipmentService
from app.modules.search.schemas import SearchFilters, SearchHit, SearchIntent, SearchRequest, SearchResponse
from app.modules.search.service import SearchService, taxonomy
from app.modules.tenants.policy import ai_controls
from app.modules.vendors.models import Vendor, VendorAlias, VendorDomain
from app.modules.vendors.schemas import VendorDetail
from app.modules.vendors.service import VendorQueryService

MAX_SEARCHES = 5  # knowledge-base searches per question
MAX_NAMED = 5
HITS_PER_SEARCH = 50  # every match a search returns (the search's own maximum)
MAX_RECORDS = 99  # every matched vendor is loaded and scored (refs run V1..V99)
AI_RECORDS = 25  # the best-matching of them are sent to the AI in full; the rest are scored in the app only
PARALLEL = 12  # searches / record loads run at once, each on its own connection
WEB_SEARCHES = 4  # web searches the answer may run
HISTORY_MESSAGES = 10
REF = re.compile(r"\[(V\d{1,2})\]")
WEB_REF = re.compile(r"\[(W\d{1,2})\]")
HIST_REF = re.compile(r"\[(H\d{1,2})\]")  # the organization's own equipment lines
DOC_REF = re.compile(r"\[(D\d{1,2})\]")  # and its project documents
DOCUMENTS = 5
HISTORY_LINES = 10
WEB_PAGES = 99  # web pages the answer and the web-supplier list may point at
WEB_VENDORS = 50  # suppliers found on the web, beyond the shortlist
# "approved vendors / suppliers", "AVL", "approved vendor list": the organization's own approved vendors only.
APPROVED = re.compile(
    r"\b(approved\s+(vendors?|suppliers?|manufacturers?|makers?|oems?|sources?|list)|avl)\b",
    re.IGNORECASE,
)
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


# Words in category names that say nothing about the equipment itself.
GENERIC_WORDS = {"equipment", "other", "general", "system", "package", "auxiliarie", "auxiliary", "rotating", "static",
                 "service", "misc", "miscellaneou", "item", "part", "unit", "type"}  # fmt: skip


def _mentions(v: VendorDetail, words: re.Pattern[str] | None) -> bool:
    """Whether a vendor with no product filed under the asked equipment still says it supplies it: in its
    description, or in a product's or service's own name. A vendor whose filed products are all something else
    (a piping stockist for "ball valves") doesn't count, whatever its description says."""
    if words is None:
        return True  # no equipment asked for: nothing to check against
    if v.equipment:
        return any(words.search(e.name or "") for e in v.equipment)
    return bool(words.search(v.description or "")) or any(words.search(x.name or "") for x in v.services)


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
    def __init__(
        self,
        session: AsyncSession,
        principal: Principal,
        ai: AiService,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.s = session
        self.principal = principal
        self.ai = ai
        self.sm = sessionmaker  # lets look-ups run side by side; without it they run one by one

    async def _each[X, R](self, items: Sequence[X], fn: Callable[[AsyncSession, X], Awaitable[R]]) -> list[R]:
        """fn(session, item) for every item, results in order. Up to PARALLEL run at once, each on its own session
        bound to the caller's tenant: every look-up is several database round trips, so one-by-one adds up."""
        if self.sm is None or len(items) < 2:
            return [await fn(self.s, x) for x in items]
        sm, gate = self.sm, asyncio.Semaphore(PARALLEL)

        async def one(x: X) -> R:
            async with gate, open_session(sm, self.principal.tenant_context(), self.principal.user_id) as s:
                return await fn(s, x)

        return list(await asyncio.gather(*(one(x) for x in items)))

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
        hits: dict[uuid.UUID, SearchHit] = {}
        intents: list[SearchIntent] = []
        # What the question itself asks for, read locally so it holds even when the planned searches leave it out
        # ("ball valves approved vendors in Singapore" -> searches for "ball valves"): its countries, and whether it
        # asks for the organization's approved vendors only.
        question = req.messages[-1].content
        asked = (await SearchService(self.s, self.principal, self.ai).parse(question, "local")).intent
        countries = list(dict.fromkeys(asked.countries))
        approved_only = self.principal.tenant_id is not None and bool(APPROVED.search(question))
        own_only = self.principal.tenant_id is not None and (plan.my_vendors_only or approved_only)
        filters = SearchFilters(
            my_vendors_only=own_only,
            avl_status=[AvlStatus.APPROVED] if approved_only else [],
            countries=countries,
        )
        searched = [q.strip() for q in plan.search_queries if q.strip()][:MAX_SEARCHES]

        async def run_search(s: AsyncSession, query: str) -> SearchResponse:
            return await SearchService(s, self.principal, self.ai).search(
                SearchRequest(query=query, parser="local", filters=filters, limit=HITS_PER_SEARCH)
            )

        equipment_fit: dict[uuid.UUID, float | None] = {}
        for result in await self._each(searched, run_search):
            intents.append(result.intent)
            for h in result.results:
                fit = h.scores.equipment
                was = equipment_fit.get(h.vendor.id)
                equipment_fit[h.vendor.id] = fit if was is None else max(was, fit if fit is not None else was)
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

        async def load(s: AsyncSession, vid: uuid.UUID) -> VendorDetail | None:
            try:
                return await VendorQueryService(s, self.principal).detail(vid)
            except NotFound:
                return None

        details = [d for d in await self._each(ordered[:MAX_RECORDS], load) if d is not None]
        if countries:
            # A country asked for: vendors headquartered there. A sales office there doesn't count ("ball valve
            # vendors in Singapore" shouldn't list an Italian maker with a Singapore office); a site counts only
            # when the headquarters country isn't on record. Vendors named in the question are kept.
            keep = set(countries)
            named = {v.id for v in previous} | set(await self._vendors_named(plan.vendor_names))
            details = [
                d
                for d in details
                if d.id in named
                or d.hq_country in keep
                or (not d.hq_country and any(loc.country in keep for loc in d.locations))
            ]
        named_ids = {v.id for v in previous} | set(await self._vendors_named(plan.vendor_names))
        equipment_words = await self._equipment_words(intents, asked)
        off_topic = [
            d
            for d in details
            if d.id not in named_ids and equipment_fit.get(d.id) == 0 and not _mentions(d, equipment_words)
        ]
        if off_topic and len(off_topic) < len(details):
            details = [d for d in details if d not in off_topic]
        else:
            off_topic = []  # nothing would be left: keep them rather than answer with no vendors
        refs = {f"V{i}": v for i, v in enumerate(details, start=1)}
        scope = [f"in {', '.join(countries)}"] if countries else []
        if off_topic:
            scope.append(f"{len(off_topic)} without a matching product left out")
        scope += (
            ["your approved vendors only"] if approved_only else ["your vendors only"] if own_only else []
        )
        step(
            "load_records",
            "Load vendor records",
            f"{len(details)} record{'s' if len(details) != 1 else ''} with approved facts and sources"
            + (f" ({'; '.join(scope)})" if scope else ""),
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
        if requirement is not None:
            requirement.countries = list(dict.fromkeys(requirement.countries + countries))
            requirement.standards = list(dict.fromkeys(asked.standards))
            requirement.certifications = list(dict.fromkeys(asked.certifications))
            requirement.equipment_categories = list(
                dict.fromkeys(asked.equipment_categories + requirement.equipment_categories)
            )
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
        # The web can't know the organization's own vendor list: questions about it are answered from the records only.
        web = plan.intent != "out_of_scope" and not own_only
        asked_for = [f"countries: {', '.join(countries)}"] if countries else []
        if own_only:
            asked_for.append(f"the organization's {'approved' if approved_only else 'own'} vendors only")
        asked_for.append(f"web search: {'on' if web else 'off'}")
        answered = await self.ai.run(
            ANSWER,
            AssistantAnswer,
            self._call,
            web_search=web,
            max_searches=WEB_SEARCHES,
            attempts=2,
            allowed_domains=controls.allowed_domains(),
            conversation=conversation,
            scope="; ".join(asked_for),
            records=[
                _record(ref, v, controls.share_private_data, our_history.get(v.id))
                for ref, v in list(refs.items())[:AI_RECORDS]
            ],
            history=[_history_record(ref, e) for ref, e in shared.items()],
            documents=[_document_record(ref, d) for ref, d in shared_docs.items()],
        )
        answer = answered.output
        pages = self._web_pages(answer.web_sources, answered.citations)
        web_vendors = (
            await self._web_vendors(answer.web_vendors, pages, answered.citations, refs, countries)
            if web and plan.intent in ("find_vendors", "compare", "knowledge_question")
            else []
        )
        text, removed = self._ground(answer.answer, refs, pages, {**shared, **shared_docs})
        cited = [r for r in dict.fromkeys(REF.findall(text)) if r in refs]
        web_cited = [pages[r] for r in dict.fromkeys(WEB_REF.findall(text)) if r in pages]
        step(
            "answer",
            "Search the web and write the answer" if web else "Write the answer",
            (f"{len(web_cited)} web page{'s' if len(web_cited) != 1 else ''} cited, " if web else "")
            + (
                f"{len(web_vendors)} more supplier{'s' if len(web_vendors) != 1 else ''} found on the web, "
                if web_vendors
                else ""
            )
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
            web_vendors=web_vendors,
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

    async def _web_vendors(
        self,
        found: list[WebVendorFound],
        pages: dict[str, WebSource],
        citations: list[Citation],
        refs: dict[str, VendorDetail],
        countries: list[str] | None = None,
    ) -> list[WebVendor]:
        """Suppliers the web search found beyond the shortlist. Each must point at a page the search returned (or have
        its own site among them); ones already shortlisted are dropped, ones already in the knowledge base are linked."""
        searched = {registrable_domain(c.url) for c in citations} - {None}
        shortlisted_names = {normalize_name(v.display_name) for v in refs.values()}
        shortlisted_domains = {registrable_domain(v.website) for v in refs.values() if v.website} - {None}
        out: list[WebVendor] = []
        seen: set[str] = set()
        for f in found[: WEB_VENDORS * 2]:
            name = " ".join(f.name.split())[:200]
            key = normalize_name(name)
            if len(key) < 2 or key in seen or key in shortlisted_names:
                continue
            site = normalize_website(f.website.strip()) if f.website.strip() else None
            domain = registrable_domain(site) if site else None
            if domain and domain in shortlisted_domains:
                continue
            page = pages.get(f.source_ref.strip().strip("[]"))
            if page is None:
                if not domain or domain not in searched:
                    continue  # nothing the search returned shows this supplier
                page = WebSource(ref="", title=name, url=site or f"https://{domain}")
            country = (normalize_country(f.country) or f.country.strip()[:60]) if f.country.strip() else None
            if countries and country and country not in countries:
                continue  # based elsewhere than the country asked for
            known = await self._known_vendor(key, domain)
            if known and known[0] in {v.id for v in refs.values()}:
                continue
            seen.add(key)
            fit = f.fit.strip().lower()
            out.append(
                WebVendor(
                    name=name,
                    website=site,
                    country=country,
                    supplies=" ".join(f.supplies.split())[:200],
                    fit=fit if fit in ("high", "medium", "low") else "medium",
                    source=page,
                    known_vendor_id=known[0] if known else None,
                    known_vendor_name=known[1] if known else None,
                )
            )
            if len(out) >= WEB_VENDORS:
                break
        order = {"high": 0, "medium": 1, "low": 2}
        return sorted(out, key=lambda w: order[w.fit])

    async def _equipment_words(
        self, intents: list[SearchIntent], asked: SearchIntent
    ) -> re.Pattern[str] | None:
        """The asked equipment as words to look for in a vendor's description ("Ball valves" -> ball, valve), from
        the names of the requested categories and their parents."""
        codes = {c for i in [asked, *intents] for c in i.equipment_categories}
        if not codes:
            return None
        tax = await taxonomy(self.s)
        names: set[str] = set()
        for code in codes:
            parts = code.split(".")
            for n in range(1, len(parts) + 1):
                names.add(tax.names.get(("equipment", ".".join(parts[:n])), ""))
        words = {
            w.lower().removesuffix("es" if w.lower().endswith(("sses", "xes")) else "s")
            for name in names
            for w in re.findall(r"[A-Za-z]{4,}", name)
        } - GENERIC_WORDS
        if not words:
            return None
        return re.compile(r"\b(" + "|".join(sorted(map(re.escape, words))) + r")(e?s)?\b", re.IGNORECASE)

    async def _known_vendor(self, key: str, domain: str | None) -> tuple[uuid.UUID, str] | None:
        """A knowledge-base vendor with this website domain or this (normalized) name or alias."""
        active = Vendor.status == VendorStatus.ACTIVE
        if domain:
            row = (
                await self.s.execute(
                    select(Vendor.id, Vendor.display_name)
                    .join(VendorDomain, VendorDomain.vendor_id == Vendor.id)
                    .where(active, VendorDomain.domain == domain)
                    .limit(1)
                )
            ).first()
            if row:
                return row[0], row[1]
        row = (
            await self.s.execute(
                select(Vendor.id, Vendor.display_name)
                .outerjoin(VendorAlias, VendorAlias.vendor_id == Vendor.id)
                .where(active, or_(Vendor.normalized_name == key, VendorAlias.normalized_alias == key))
                .limit(1)
            )
        ).first()
        return (row[0], row[1]) if row else None

    @staticmethod
    def _web_pages(listed: list[WebPage], citations: list[Citation]) -> dict[str, WebSource]:
        """The web pages the answer may cite: only those on a site the search actually returned."""
        searched = {registrable_domain(c.url) for c in citations} - {None}
        pages: dict[str, WebSource] = {}
        for p in listed[:WEB_PAGES]:
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
