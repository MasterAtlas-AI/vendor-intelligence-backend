import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class CitedVendor(BaseModel):
    ref: str  # "V1"
    vendor_id: uuid.UUID
    name: str
    country: str | None
    avl_status: str | None  # the organization's own status, if the vendor is on its list
    sources: list[str] = Field(default_factory=list)  # web pages behind the facts used


class ChatAction(BaseModel):
    """Something the app can do next, shown as a button. Nothing runs until the user clicks (and confirms)."""

    kind: Literal["research_vendor", "discover_vendors"]
    label: str
    vendor_id: uuid.UUID | None = None  # research_vendor
    vendor_name: str | None = None
    website: str | None = None  # research_vendor: a website to use when the vendor has none on record
    brief: str | None = None  # discover_vendors: what to look for (prefills the Discover form)


class ChatResponse(BaseModel):
    answer: str  # plain text with "- " lists; vendors cited as [V1], [V2]
    vendors: list[CitedVendor]
    follow_ups: list[str]
    actions: list[ChatAction] = Field(default_factory=list)
    searched: list[str]  # what the assistant looked up, for transparency
    removed_refs: int = 0  # citations dropped because they pointed at vendors outside the retrieved data


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


class AssistantAnswer(BaseModel):
    answer: str
    cited_refs: list[str]
    follow_ups: list[str]
    actions: list[SuggestedAction]
