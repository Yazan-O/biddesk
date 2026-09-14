"""Typed records shared by every stage. Every claim that reaches a human carries its source."""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field


class OwnerRule(BaseModel):
    """A rule the owner wrote in plain English, enforced in code before any model call."""
    id: str
    text: str                       # what the owner said
    kind: Literal["max_distance_miles", "min_base_months", "min_days_to_close",
                  "min_past_performance_matches", "set_aside_eligible", "notice_types"]
    value: float | int | list[str] | None = None


class PastPerformance(BaseModel):
    customer: str
    title: str
    naics: str
    value_usd: int
    period: str
    keywords: list[str]


class FirmProfile(BaseModel):
    slug: str
    name: str
    city: str
    state: str
    lat: float
    lon: float
    what: str
    naics: list[str]
    set_asides: list[str]           # SAM codes the firm can claim: WOSB, SDVOSBC, 8A, SBA
    capabilities: str
    past_performance: list[PastPerformance]
    rules: list[OwnerRule]
    fictional: bool = True


class Notice(BaseModel):
    """One SAM.gov opportunity, normalized from the v2 search response."""
    notice_id: str
    title: str
    solicitation_number: Optional[str] = None
    type: str                       # Solicitation, Presolicitation, Combined Synopsis/Solicitation, Award Notice, Sources Sought, ...
    base_type: Optional[str] = None
    naics: Optional[str] = None
    naics_all: list[str] = []
    set_aside_code: Optional[str] = None
    set_aside_desc: Optional[str] = None
    posted: str                     # YYYY-MM-DD
    response_deadline: Optional[str] = None   # ISO with tz as SAM gives it
    archive_date: Optional[str] = None
    agency_path: Optional[str] = None
    office_city: Optional[str] = None
    office_state: Optional[str] = None
    pop_city: Optional[str] = None
    pop_state: Optional[str] = None
    pop_zip: Optional[str] = None
    pop_country: Optional[str] = None
    description_url: Optional[str] = None
    resource_links: list[str] = []
    ui_link: str
    active: bool = True
    award: Optional[dict] = None
    description_text: Optional[str] = None    # filled by sam_fetch_description
    fetched_at: str                 # ISO timestamp, America/Chicago


class Requirement(BaseModel):
    """One 'shall' statement (or equivalent) lifted from an attachment, with its page."""
    id: str
    text: str                       # verbatim quote from the document
    source_file: str
    page: int
    category: Literal["scope", "staffing", "schedule", "compliance", "submission", "evaluation", "other"] = "other"


class MatrixRow(BaseModel):
    requirement_id: str
    quote: str                      # must be a verbatim substring of the requirement text
    source_file: str
    page: int
    firm_answer: str                # how the firm meets it, from the profile
    status: Literal["meets", "partial", "gap", "unknown"]
    evidence: str                   # which past performance / capability line supports the answer


class DroppedRow(BaseModel):
    """A matrix row validate_matrix() rejected, kept so the validator's work can be inspected and evaluated."""
    requirement_id: str
    quote: str
    source_file: str = ""
    page: int = 0
    reason: str                     # unknown_requirement | not_verbatim | wrong_source | empty_quote


class KeyDates(BaseModel):
    questions_due: Optional[str] = None
    proposal_due: Optional[str] = None
    site_visit: Optional[str] = None
    period_of_performance: Optional[str] = None
    source_quotes: list[str] = []


class DecisionCard(BaseModel):
    notice_id: str
    firm_slug: str
    situation: str                  # two lines
    recommendation: Literal["bid", "no-bid"]
    reasons_for: list[str]
    reasons_against: list[str]
    default: str                    # what happens if the owner says nothing by the deadline
    decide_by: Optional[str]
    key_dates: KeyDates
    fit_score: Optional[float]      # 0..1, computed in code from the validated matrix; None when no row scored
    matrix_summary: str             # "12 of 15 requirements met, 2 partial, 1 gap"
    evidence_link: str              # SAM.gov uiLink


class CalendarEntry(BaseModel):
    title: str
    date: str                       # YYYY-MM-DD or ISO datetime, America/Chicago
    note: str
    source_quote: str               # the solicitation text the date came from


class Drafts(BaseModel):
    capability_statement: str
    questions_for_co: list[str]
    calendar_entries: list[CalendarEntry]


# ---- specialist outputs (each Graph node returns one of these; every claim quotes its source)

class ReaderFindings(BaseModel):
    """Attachment-reader specialist: the requirements that matter, grouped, each quoted verbatim."""
    notice_id: str
    key_requirement_ids: list[str] = Field(description="ids of the requirements a bid/no-bid turns on (at most 25)")
    scope_summary: str = Field(description="what the government is buying, three sentences, from the text")
    evaluation_basis: str = Field(description="how offers are evaluated, quoted or 'not stated'")
    incumbent_or_history: str = Field(description="incumbent, prior award or recompete facts, quoted or 'none found'")
    quotes: list[str] = Field(description="verbatim sentences that support scope, evaluation and history")


class FitAssessment(BaseModel):
    """Fit-scorer specialist: profile against requirements, past-performance matching."""
    notice_id: str
    fit_score: float = Field(ge=0, le=1)
    matched_past_performance: list[str] = Field(description="titles of past-performance records that match")
    strengths: list[str]
    gaps: list[str]
    matrix: list[MatrixRow]


class DeadlineFindings(BaseModel):
    """Deadline-and-history specialist: every date, quoted, plus amendment and set-aside facts."""
    notice_id: str
    key_dates: KeyDates
    days_to_close: Optional[float] = None
    set_aside_note: str
    amendment_note: str = Field(description="what changed versus the previous version, or 'no amendment'")
    calendar_entries: list[CalendarEntry]


class DeskResult(BaseModel):
    """Everything the desk produced for one notice; the gallery case and the eval unit."""
    notice_id: str
    firm_slug: str
    model_used: str
    fallback_used: bool = False
    forced_model: Optional[str] = None   # set when the operator pinned a model (--model haiku); not a fallback
    extracted_at: Optional[str] = None   # extracted_at of the extracted.json this case was rendered against
    evidence_basis: Optional[str] = None # set when the card rests on the description alone
    days_to_close: Optional[float] = None  # computed in code at run time; negative means the notice already closed
    closed: bool = False                   # True when days_to_close is negative at run time
    response_kind: Optional[str] = None   # computed in code from notice.type: a proposal, or an RFI capability statement
    card: DecisionCard
    matrix: list[MatrixRow]
    matrix_dropped: int = 0          # rows validate_matrix() removed for not quoting the source
    matrix_dropped_rows: list[DroppedRow] = []   # the rejected rows themselves, with the reason
    fit_score_code: Optional[float] = None       # computed in code from the validated matrix: meets=1, partial=0.5, gap=0; unknown excluded
    drafts: Drafts
    reader: ReaderFindings
    fit: FitAssessment
    deadlines: DeadlineFindings
    model_calls: int
    tokens_in: int
    tokens_out: int
    seconds: float
    produced_at: str


class LedgerRow(BaseModel):
    ts: str
    firm_slug: str
    notice_id: str
    action: str                     # filed | surfaced | denied_tool | card_answered | rule_learned | deadline_moved
    tier: Optional[int] = None
    why: str
    evidence: str
    undo: Optional[str] = None
