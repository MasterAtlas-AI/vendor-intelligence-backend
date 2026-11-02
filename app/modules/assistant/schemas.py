import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.search.schemas import SearchIntent

# ---------------------------------------------------------------- API


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    """The conversation so far, newest last (kept by the client; the last message is the user's question)."""

    model_config = ConfigDict(extra="forbid")
    messages: list[ChatMessage] = Field(min_length=1, max_length=20)
    # Vendors the previous answer cited, so follow-ups ("which of them has ISO 9001?") can refer back.
    context_vendor_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    # The saved chat to add this question and its answer to; empty starts a new saved chat.
    conversation_id: uuid.UUID | None = None


class CitedVendor(BaseModel):
    ref: str  # "V1"
    vendor_id: uuid.UUID
    name: str
    country: str | None
    avl_status: str | None  # the organization's own status, if the vendor is on its list
    sources: list[str] = Field(default_factory=list)  # web pages behind the facts used


class WebSource(BaseModel):
    """A web page the answer cites as [W1], [W2]: found by the AI's web search, not verified like knowledge-base facts."""

    ref: str  # "W1"
    title: str
    url: str


class ChatAction(BaseModel):
    """Something the app can do next, shown as a button. Nothing runs until the user clicks (and confirms)."""

    kind: Literal["research_vendor", "discover_vendors"]
    label: str
    vendor_id: uuid.UUID | None = None  # research_vendor
    vendor_name: str | None = None
    website: str | None = None  # research_vendor: a website to use when the vendor has none on record
    brief: str | None = None  # discover_vendors: what to look for (prefills the Discover form)


class AgentStep(BaseModel):
    """One step the assistant took for a message, shown as the agent's plan and in the activity log."""

    key: Literal["understand", "search_kb", "load_records", "answer", "check", "rank"]
    label: str
    detail: str
    ms: int


class DimensionScores(BaseModel):
    """0-1 per dimension; None when the request didn't ask about it (the app then leaves it out of the ranking)."""

    relevance: float | None  # wording and meaning match (full-text / semantic)
    equipment: float | None  # requested equipment types
    standards: float | None  # requested standards and certifications
    industry: float | None  # requested industries
    track_record: float  # project references on record
    data_quality: float  # how complete and verified the vendor's record is
    approved_vendor: float  # the organization's own AVL status (0.3 when not assessed)


class ShortlistRow(BaseModel):
    vendor_id: uuid.UUID
    ref: str | None  # "V1" when the answer could cite it
    name: str
    country: str | None
    city: str | None
    website: str | None
    avl_status: str | None
    scores: DimensionScores
    matched: list[str]  # why it matched ("API 610 BB2 pump: HX-2")
    products: int
    references: int
    fpso_references: int
    certifications: int
    flags: list[str]  # gaps worth checking ("No project references on record")


class ChatResponse(BaseModel):
    answer: str  # plain text with "- " lists; vendors cited as [V1], [V2]
    vendors: list[CitedVendor]
    web_sources: list[WebSource] = Field(default_factory=list)  # pages cited as [W1], [W2]
    follow_ups: list[str]
    actions: list[ChatAction] = Field(default_factory=list)
    searched: list[str]  # what the assistant looked up, for transparency
    removed_refs: int = 0  # citations dropped: vendors outside the records, or pages the search didn't return
    conversation_id: uuid.UUID | None = None  # the saved chat it was added to (none for API keys)
    # The workspace beside the chat: what was understood, the vendors considered (with scores per dimension, re-ranked
    # live by the user's weights in the app), and the steps taken.
    requirement: SearchIntent | None = None
    shortlist: list[ShortlistRow] = Field(default_factory=list)
    steps: list[AgentStep] = Field(default_factory=list)
    started_at: datetime | None = None


# ---------------------------------------------------------------- saved chats


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class SavedMessage(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    reply: ChatResponse | None
    created_at: datetime


class ConversationDetail(ConversationRead):
    messages: list[SavedMessage]


class ConversationRename(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------- AI outputs (flat, for strict Structured Outputs)


class AssistantPlan(BaseModel):
    intent: str  # find_vendors | vendor_details | compare | knowledge_question | out_of_scope
    search_queries: list[str]  # plain-language vendor searches to run (at most 3 are used)
    vendor_names: list[str]  # specific vendors the user names or refers to (at most 5 are used)
    my_vendors_only: bool  # the user asks about their own organization's vendors / AVL


class SuggestedAction(BaseModel):
    kind: str  # research_vendor | discover_vendors
    ref: str  # research_vendor: the vendor's ref (V1); else empty
    website: str  # research_vendor: a website the user gave for it; else empty
    brief: str  # discover_vendors: what to search the web for; else empty


class WebPage(BaseModel):
    ref: str  # W1
    title: str
    url: str


class AssistantAnswer(BaseModel):
    answer: str
    cited_refs: list[str]
    web_sources: list[WebPage]
    follow_ups: list[str]
    actions: list[SuggestedAction]
