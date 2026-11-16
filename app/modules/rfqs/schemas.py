import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.vendors.schemas import VendorSummary

RfqStatus = Literal["draft", "issued", "evaluating", "awarded", "closed", "cancelled"]
RfqVendorStatus = Literal["shortlisted", "invited", "quoted", "declined", "awarded", "lost"]


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    mandatory: bool = True


class RfqCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    rfq_number: str | None = Field(default=None, max_length=40)  # default: RFQ-<year>-<n>
    project_id: uuid.UUID | None = None
    from_shortlist: bool = False  # add the project's shortlisted and selected vendors
    description: str = Field(default="", max_length=5000)
    equipment_categories: list[str] = Field(default_factory=list, max_length=10)
    requirements: list[Requirement] = Field(default_factory=list, max_length=50)
    due_date: date | None = None


class RfqUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: int
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    equipment_categories: list[str] | None = Field(default=None, max_length=10)
    requirements: list[Requirement] | None = Field(default=None, max_length=50)
    due_date: date | None = None


class AwardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    vendor_id: uuid.UUID
    note: str = Field(default="", max_length=2000)


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    note: str = Field(default="", max_length=2000)


class RfqRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    rfq_number: str
    title: str
    description: str
    project_id: uuid.UUID | None
    project_name: str | None = None
    equipment_categories: list[str]
    requirements: list[Requirement]
    due_date: date | None
    status: RfqStatus
    awarded_vendor_id: uuid.UUID | None
    awarded_vendor_name: str | None = None
    issued_at: datetime | None
    awarded_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
    vendor_counts: dict[str, int] = Field(default_factory=dict)
    can_edit: bool = False


class RfqVendorAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_id: uuid.UUID


class RfqVendorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["invited", "quoted", "declined"] | None = None  # awarded/lost are set by the award
    quote_amount: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    lead_time_weeks: int | None = Field(default=None, ge=0, le=520)
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not v.isalpha():
            raise ValueError("use a 3-letter currency code, e.g. USD")
        return v.upper()


class RfqVendorRead(BaseModel):
    vendor: VendorSummary
    status: RfqVendorStatus
    quote_amount: Decimal | None
    currency: str | None
    lead_time_weeks: int | None
    quote_received_at: datetime | None
    notes: str
    scorecard_id: uuid.UUID | None = None  # the vendor's latest scorecard on the RFQ's project
    score: Decimal | None = None
    emailed_at: datetime | None = None  # the RFQ was e-mailed to the vendor from the app (latest send)
    emailed_to: str | None = None
    test_recipient: str | None = None  # testing: where an RFQ e-mail goes when the vendor has no address


class EmailRecipient(BaseModel):
    vendor_id: uuid.UUID
    name: str
    email: str | None  # where the RFQ goes: the vendor's main company email, or the test address (see below)
    test_address: bool = False  # the vendor has no email; the server's test address receives it instead
    status: RfqVendorStatus
    emailed_at: datetime | None


class EmailPreview(BaseModel):
    configured: bool  # the server can send e-mail (SMTP set up); otherwise messages are only logged
    sender: str  # the From address vendors see
    reply_to: str | None  # replies go to the user sending the RFQ
    subject: str
    body: str  # plain text of the message (the note is added where it says so)
    recipients: list[EmailRecipient]


class EmailSend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vendor_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    note: str = Field(default="", max_length=3000)  # a personal message above the RFQ details
    test_to_me: bool = False  # send one copy to the sender's own address instead of the vendors


class EmailSendResult(BaseModel):
    sent: list[str]  # addresses the RFQ went to
    skipped: list[str]  # vendors with no email, with the reason
    failed: list[str]  # addresses the mail server refused
    issued: bool  # a draft RFQ was issued by the send (vendors became invited)
