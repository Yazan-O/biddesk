"""Ledger and guard-hook tests. No Bedrock call, no Agent invocation.

Imports verified against the installed strands-agents 1.55.1 on this machine:

    python -c "from strands.hooks import BeforeToolCallEvent, AfterToolCallEvent, \
        BeforeModelCallEvent, BeforeInvocationEvent, HookProvider, HookRegistry"
    python -c "from strands.interventions import InterventionHandler, Deny, Proceed"

The hook events are dataclasses that require a live ``agent`` (and for
``AfterToolCallEvent`` a ``selected_tool`` and ``duration``), so building a real one
would mean constructing an Agent with a model. These tests call the hook callbacks
directly with a ``SimpleNamespace`` that carries the exact fields the callbacks read
and write: ``tool_use`` and the writable ``cancel_tool`` / ``result``. The field names
come from the installed dataclasses, not from the docs:

    BeforeToolCallEvent: agent, selected_tool, tool_use, invocation_state, cancel_tool
    AfterToolCallEvent:  agent, selected_tool, tool_use, invocation_state, result,
                         exception, cancel_message, duration, retry
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from biddesk.hooks import (
    NoSubmitGuard,
    NoSubmitIntervention,
    PiiRedactor,
    ProvenanceStamp,
    TierCounter,
    deny_reason,
    redact,
)
from biddesk.ledger import TZ, Ledger
from biddesk.models import LedgerRow


@pytest.fixture()
def ledger(tmp_path) -> Ledger:
    return Ledger("testfirm", path=tmp_path / "testfirm.jsonl")


def _row(action: str = "filed", notice_id: str = "N1", **kw) -> LedgerRow:
    base = dict(ts="", firm_slug="", notice_id=notice_id, action=action,
                why="because", evidence="{}", tier=1)
    base.update(kw)
    return LedgerRow(**base)


def _before_event(name: str, tool_input: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        tool_use={"name": name, "toolUseId": "t1", "input": tool_input or {}},
        cancel_tool=None,
        invocation_state={},
    )


def _after_event(name: str, tool_input: dict, text: str = "body") -> SimpleNamespace:
    return SimpleNamespace(
        tool_use={"name": name, "toolUseId": "t1", "input": tool_input},
        result={"toolUseId": "t1", "status": "success", "content": [{"text": text}]},
        invocation_state={},
    )


# ----------------------------------------------------------------- the ledger


def test_append_and_rows_filter(ledger: Ledger):
    ledger.append(_row("filed", "N1"))
    ledger.append(_row("surfaced", "N2"))
    ledger.append(_row("filed", "N2"))

    assert len(ledger.rows()) == 3
    assert [r.notice_id for r in ledger.rows(notice_id="N2")] == ["N2", "N2"]
    assert [r.action for r in ledger.rows(action="filed")] == ["filed", "filed"]
    assert len(ledger.rows(limit=2)) == 2
    # every appended row got a timestamp and the firm slug
    assert all(r.ts and r.firm_slug == "testfirm" for r in ledger.rows())


def test_append_is_append_only_on_disk(ledger: Ledger):
    first = ledger.append(_row("filed", "N1"))
    ledger.undo(first.ts, "owner vetoed")
    lines = ledger.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["action"] == "filed"          # original untouched
    assert json.loads(lines[1])["action"] == "undone"


def test_undo_references_the_original(ledger: Ledger):
    original = ledger.append(_row("filed", "N7", undo="delete draft 7"))
    undone = ledger.undo(original.ts, "owner said no")

    assert undone.action == "undone"
    assert undone.notice_id == "N7"
    assert undone.why == "owner said no"
    evidence = json.loads(undone.evidence)
    assert evidence["undoes_ts"] == original.ts
    assert evidence["original_action"] == "filed"
    assert evidence["undo_recipe"] == "delete draft 7"
    assert ledger.undone_ts() == {original.ts}


def test_undo_unknown_ts_raises(ledger: Ledger):
    with pytest.raises(KeyError):
        ledger.undo("2026-01-01T00:00:00-06:00", "nope")


def test_pending_vetoes_window_reads_and_undo(ledger: Ledger):
    now = datetime.now(TZ)
    old = (now - timedelta(minutes=30)).isoformat()
    recent = (now - timedelta(minutes=2)).isoformat()
    recent2 = (now - timedelta(minutes=3)).isoformat()
    recent3 = (now - timedelta(minutes=4)).isoformat()

    ledger.append(_row("filed", "OLD", ts=old))              # outside the window
    fresh = ledger.append(_row("filed", "FRESH", ts=recent))  # inside
    ledger.append(_row("read", "READ", ts=recent2))           # reads never sit in a window
    vetoed = ledger.append(_row("filed", "VETOED", ts=recent3))

    pending = ledger.pending_vetoes(now=now, window_minutes=10)
    assert {r.notice_id for r in pending} == {"FRESH", "VETOED"}

    ledger.undo(vetoed.ts, "owner vetoed inside the window")
    pending = ledger.pending_vetoes(now=now, window_minutes=10)
    assert {r.notice_id for r in pending} == {"FRESH"}
    assert fresh.notice_id == "FRESH"


def test_summary_counts(ledger: Ledger):
    ledger.append(_row("filed", "N1", tier=0))
    ledger.append(_row("filed", "N2", tier=2))
    ledger.append(_row("read", "N2", tier=None))

    summary = ledger.summary()
    assert summary["total"] == 3
    assert summary["by_action"] == {"filed": 2, "read": 1}
    assert summary["by_tier"] == {"0": 1, "2": 1, "none": 1}


# ------------------------------------------------------------- NoSubmitGuard


def test_guard_cancels_submit_bid_and_writes_a_ledger_row(ledger: Ledger):
    guard = NoSubmitGuard(firm_slug="testfirm", ledger=ledger)
    event = _before_event("submit_bid", {"notice_id": "N9", "amount": 42000})

    guard.before_tool_call(event)

    assert event.cancel_tool is not None
    assert "Biddesk drafts" in event.cancel_tool
    assert "the owner submits" in event.cancel_tool
    rows = ledger.rows(action="denied_tool")
    assert len(rows) == 1
    assert rows[0].notice_id == "N9"
    assert "submit_bid" in rows[0].why
    assert json.loads(rows[0].evidence)["amount"] == 42000


def test_guard_lets_sam_search_through(ledger: Ledger):
    guard = NoSubmitGuard(firm_slug="testfirm", ledger=ledger)
    event = _before_event("sam_search", {"naics": "236220", "state": "OK"})

    guard.before_tool_call(event)

    assert event.cancel_tool is None
    assert ledger.rows() == []


def test_guard_blocks_a_sam_submit_url_on_an_allowed_tool(ledger: Ledger):
    guard = NoSubmitGuard(firm_slug="testfirm", ledger=ledger)
    event = _before_event(
        "http_post",
        {"notice_id": "N4", "url": "https://sam.gov/opp/N4/submit-offer"},
    )

    guard.before_tool_call(event)

    assert event.cancel_tool is not None
    assert ledger.rows(action="denied_tool")[0].notice_id == "N4"


def test_guard_evidence_hides_keys_but_keeps_identifiers(ledger: Ledger):
    guard = NoSubmitGuard(firm_slug="testfirm", ledger=ledger)
    notice_id = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    guard.before_tool_call(
        _before_event(
            "make_payment",
            {
                "api_key": "abcd1234abcd1234abcd1234abcd",
                "notice_id": notice_id,
                "resource_links": ["123e4567-e89b-12d3-a456-426614174000"],
                "amount": 5,
            },
        )
    )
    evidence = json.loads(ledger.rows(action="denied_tool")[0].evidence)
    assert evidence["api_key"] == "[redacted]"
    # the identifiers the ledger exists to preserve survive the scrub
    assert evidence["notice_id"] == notice_id
    assert evidence["resource_links"] == ["123e4567-e89b-12d3-a456-426614174000"]


def test_deny_reason_matches_every_default_tool():
    for name in ("submit_bid", "submit_offer", "submit_proposal", "submit_quote", "sign",
                 "sign_document", "esign", "send_email", "upload_to_sam", "make_payment",
                 "transfer_funds", "wire_transfer"):
        assert deny_reason(name, {}) is not None
    assert deny_reason("sam_search", {"q": "roof"}) is None
    assert deny_reason("sam_fetch_attachment", {"url": "https://sam.gov/api/prod/opps/v3/x.pdf"}) is None


def test_intervention_form_denies_and_proceeds(ledger: Ledger):
    handler = NoSubmitIntervention(firm_slug="testfirm", ledger=ledger)
    assert handler.on_error == "deny"

    denied = handler.before_tool_call(_before_event("sign_document", {"notice_id": "N2"}))
    assert type(denied).__name__ == "Deny"
    assert "Biddesk drafts" in denied.reason

    allowed = handler.before_tool_call(_before_event("sam_search", {}))
    assert type(allowed).__name__ == "Proceed"


# ------------------------------------------------------------ ProvenanceStamp


def test_provenance_stamps_result_and_logs_a_read(ledger: Ledger):
    stamper = ProvenanceStamp(firm_slug="testfirm", ledger=ledger)
    event = _after_event(
        "extract_requirements",
        {"notice_id": "N5", "source_file": "sow.pdf", "page": 3},
    )

    stamper.after_tool_call(event)

    stamp = json.loads(event.result["content"][-1]["text"])["biddesk_provenance"]
    assert stamp == {"notice_id": "N5", "source_file": "sow.pdf", "page": 3}
    rows = ledger.rows(action="read")
    assert len(rows) == 1 and rows[0].tier == 2 and rows[0].notice_id == "N5"


def test_provenance_ignores_other_tools(ledger: Ledger):
    stamper = ProvenanceStamp(firm_slug="testfirm", ledger=ledger)
    event = _after_event("write_card", {"notice_id": "N5"})

    stamper.after_tool_call(event)

    assert len(event.result["content"]) == 1
    assert ledger.rows() == []


# ---------------------------------------------------------------- TierCounter


def test_tier_counter_counts_and_resets():
    counter = TierCounter()
    counter.before_model_call(SimpleNamespace())
    counter.before_model_call(SimpleNamespace())
    counter.before_tool_call(_before_event("sam_search"))
    counter.before_tool_call(_before_event("sam_search"))
    counter.before_tool_call(_before_event("read_attachment"))

    snap = counter.snapshot()
    assert snap["model_calls"] == 2
    assert snap["tool_calls"] == 3
    assert snap["per_tool"] == {"sam_search": 2, "read_attachment": 1}

    counter.before_invocation(SimpleNamespace())
    assert counter.snapshot()["model_calls"] == 0
    assert counter.snapshot()["tool_calls"] == 0


def test_tier_counter_persist_survives_invocation():
    counter = TierCounter(persist=True)
    counter.before_model_call(SimpleNamespace())
    counter.before_invocation(SimpleNamespace())
    assert counter.snapshot()["model_calls"] == 1


# ----------------------------------------------------------------- PiiRedactor


def test_redact_masks_email_phone_ssn():
    text = "Call (405) 555-0134 or co@gsa.gov, SSN 123-45-6789."
    out = redact(text)
    assert "555-0134" not in out and "co@gsa.gov" not in out and "123-45-6789" not in out
    assert "[PHONE REDACTED]" in out and "[EMAIL REDACTED]" in out and "[SSN REDACTED]" in out


def test_redactor_hook_is_off_by_default():
    event = _after_event("sam_search", {}, text="co@gsa.gov")
    PiiRedactor().after_tool_call(event)
    assert event.result["content"][0]["text"] == "co@gsa.gov"

    event = _after_event("sam_search", {}, text="co@gsa.gov")
    PiiRedactor(redact_outputs=True).after_tool_call(event)
    assert event.result["content"][0]["text"] == "[EMAIL REDACTED]"


def test_guard_ignores_sam_urls_quoted_inside_free_text():
    from biddesk.hooks import deny_reason
    sow = "Offers shall be submitted via https://sam.gov/workspace/submit-offer by the closing date."
    assert deny_reason("extract_requirements", {"notice_id": "N9", "text": sow}) is None
    # but a URL field still trips it
    assert deny_reason("http_request", {"url": "https://sam.gov/workspace/submit-offer"}) is not None
    # and a path merely containing the letters 'sign' does not
    assert deny_reason("http_request", {"url": "https://sam.gov/opp/assignments/design.pdf"}) is None


# ===========================================================================
# Real-Agent stress tests, appended 2026-09-13 by the hooks/ledger review.
#
# These run a REAL strands Agent against a scripted Model double (no Bedrock
# call, no network). Agent.__init__ in strands-agents 1.55.1 accepts BOTH:
#     hooks: list[HookProvider | HookCallback] | None = None
#     interventions: list[InterventionHandler] | None = None
# so both enforcement paths in biddesk.hooks are live and both are covered.
#
# Scratch probes: _runs/2026-09-13_hooks_review/
# ===========================================================================

import threading
from typing import Any, AsyncIterable

from strands import Agent, tool
from strands.models.model import Model

from biddesk.hooks import _evidence
from biddesk.ledger import _parse_ts


def _tool_round(name: str, tool_input: dict, tool_use_id: str = "tu-1") -> list[dict]:
    """Bedrock-shaped stream events for one round that requests a tool."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0,
                               "start": {"toolUse": {"toolUseId": tool_use_id, "name": name}}}},
        {"contentBlockDelta": {"contentBlockIndex": 0,
                               "delta": {"toolUse": {"input": json.dumps(tool_input)}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]


def _text_round(text: str = "done") -> list[dict]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                      "metrics": {"latencyMs": 1}}},
    ]


class FakeModel(Model):
    """Replays scripted rounds. Hard-capped so a non-terminating loop fails fast."""

    def __init__(self, rounds: list[list[dict]], max_rounds: int = 4) -> None:
        self.rounds = rounds
        self.max_rounds = max_rounds
        self.calls = 0
        self._config: dict = {}

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> Any:
        return self._config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("fake model")
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs) -> AsyncIterable[dict]:
        self.calls += 1
        if self.calls > self.max_rounds:
            raise RuntimeError(f"fake model called {self.calls} times; agent loop not terminating")
        events = self.rounds[self.calls - 1] if self.calls <= len(self.rounds) else _text_round("stop")
        for event in events:
            yield event


SUBMIT_CALLS: list[str] = []
READ_CALLS: list[str] = []


@tool
def submit_bid(notice_id: str) -> str:
    """Submit a bid for a notice (must never actually run)."""
    SUBMIT_CALLS.append(notice_id)
    return "submitted"


@tool
def read_attachment(notice_id: str, source_file: str = "sow.pdf", page: int = 3) -> str:
    """Read one page of a solicitation attachment."""
    READ_CALLS.append(notice_id)
    return "page text"


@pytest.fixture(autouse=True)
def _clear_sentinels():
    SUBMIT_CALLS.clear()
    READ_CALLS.clear()
    yield


# -------------------------------------------------- 1. real Agent enforcement


def test_agent_init_accepts_both_hooks_and_interventions():
    import inspect
    params = inspect.signature(Agent.__init__).parameters
    assert "hooks" in params and "interventions" in params


def test_real_agent_hook_blocks_submit_bid_and_writes_ledger(ledger):
    model = FakeModel([_tool_round("submit_bid", {"notice_id": "N1"}), _text_round("ok")])
    guard = NoSubmitGuard(ledger=ledger)
    agent = Agent(model=model, tools=[submit_bid], hooks=[guard])
    result = agent("submit it")

    assert SUBMIT_CALLS == []                       # the tool function never ran
    rows = ledger.rows()
    assert [r.action for r in rows] == ["denied_tool"]
    assert rows[0].tier == 0
    assert "no-submit deny list" in rows[0].why
    assert guard.denials == [{"tool": "submit_bid",
                              "reason": "tool 'submit_bid' is on the no-submit deny list"}]
    # a cancelled tool does not halt the loop: the model gets a second round
    assert model.calls == 2
    assert "ok" in str(result)


def test_real_agent_intervention_blocks_submit_bid_and_writes_ledger(ledger):
    model = FakeModel([_tool_round("submit_bid", {"notice_id": "N1"}), _text_round("ok")])
    agent = Agent(model=model, tools=[submit_bid],
                  interventions=[NoSubmitIntervention(ledger=ledger)])
    agent("submit it")

    assert SUBMIT_CALLS == []
    rows = ledger.rows()
    assert [r.action for r in rows] == ["denied_tool"]
    assert rows[0].tier == 0


def test_guard_fires_even_when_the_tool_is_not_registered(ledger):
    """BeforeToolCallEvent fires before name resolution, so the deny list still logs."""
    model = FakeModel([_tool_round("submit_bid", {"notice_id": "N1"}), _text_round("ok")])
    guard = NoSubmitGuard(ledger=ledger)
    Agent(model=model, tools=[], hooks=[guard])("submit it")
    assert len(guard.denials) == 1
    assert len(ledger.rows(action="denied_tool")) == 1


def test_real_agent_allows_a_non_denied_tool(ledger):
    model = FakeModel([_tool_round("read_attachment", {"notice_id": "N7"}), _text_round("ok")])
    guard = NoSubmitGuard(ledger=ledger)
    Agent(model=model, tools=[read_attachment], hooks=[guard])("read it")
    assert READ_CALLS == ["N7"]
    assert guard.denials == []


# ------------------------------------------------------------- 2. bypass table


BYPASS_CASES = [
    ("upper case tool name",        "SUBMIT_BID", {}, True),
    ("padded tool name",            " submit_bid ", {}, True),
    ("cyrillic lookalike s",        "Ñ•ubmit_bid", {}, False),
    ("versioned tool name",         "submit_bid_v2", {}, True),
    ("url nested three deep",       "http_request",
     {"a": {"b": {"c": {"url": "https://sam.gov/api/submit"}}}}, True),
    ("percent-encoded submit",      "http_request",
     {"url": "https://sam.gov/api/%73ubmit"}, True),
    ("sam.gov in a non-sam path",   "http_request",
     {"url": "https://evil.com/sam.gov/submit"}, True),
    ("sam.gov.evil.com host",       "http_request",
     {"url": "https://sam.gov.evil.com/submit"}, True),
    ("url as a list element",       "http_request",
     {"urls": ["https://sam.gov/api/submit"]}, True),
    ("non-sam submit endpoint",     "http_request",
     {"url": "https://evil.com/submit-offer"}, False),
    ("upper-case path segment",     "http_request",
     {"url": "https://sam.gov/api/SUBMIT"}, True),
    ("submit in a query param",     "http_request",
     {"url": "https://sam.gov/api/x?action=submit"}, True),
    ("url embedded in free text",   "http_request",
     {"url": "see https://sam.gov/api/submit now"}, False),
]


@pytest.mark.parametrize("label,name,tool_input,denied", BYPASS_CASES,
                         ids=[c[0] for c in BYPASS_CASES])
def test_bypass_table(label, name, tool_input, denied):
    assert (deny_reason(name, tool_input) is not None) is denied


def test_unicode_lookalike_name_cannot_reach_a_real_tool(ledger):
    """The lookalike passes deny_reason, but no tool by that name exists, so nothing runs."""
    model = FakeModel([_tool_round("Ñ•ubmit_bid", {"notice_id": "N1"}), _text_round("ok")])
    guard = NoSubmitGuard(ledger=ledger)
    Agent(model=model, tools=[submit_bid], hooks=[guard])("submit it")
    assert SUBMIT_CALLS == []
    assert guard.denials == []          # not denied, merely unresolvable


# -------------------------------------------------------- 3. ProvenanceStamp


def test_provenance_stamp_through_a_real_agent(ledger):
    model = FakeModel([_tool_round("read_attachment",
                                   {"notice_id": "N7", "source_file": "sow.pdf", "page": 3}),
                       _text_round("ok")])
    agent = Agent(model=model, tools=[read_attachment], hooks=[ProvenanceStamp(ledger=ledger)])
    agent("read it")

    tool_results = [c["toolResult"] for m in agent.messages for c in m.get("content", [])
                    if "toolResult" in c]
    assert len(tool_results) == 1
    texts = [b["text"] for b in tool_results[0]["content"] if "text" in b]
    stamped = json.loads(texts[-1])["biddesk_provenance"]
    assert stamped == {"notice_id": "N7", "source_file": "sow.pdf", "page": 3}
    assert texts[0] == "page text"          # the original result survives intact
    assert model.calls == 2                 # the agent parsed it and kept going
    assert [r.action for r in ledger.rows()] == ["read"]


def test_provenance_stamp_accepts_a_real_tool_result_typeddict(ledger):
    """ToolResult is a TypedDict, so at runtime it is a plain dict and isinstance holds."""
    from strands.types.tools import ToolResult
    result: ToolResult = {"toolUseId": "t1", "status": "success", "content": [{"text": "x"}]}
    event = SimpleNamespace(
        tool_use={"name": "read_attachment", "toolUseId": "t1", "input": {"notice_id": "N1"}},
        result=result, exception=None)
    ProvenanceStamp(ledger=ledger).after_tool_call(event)
    assert json.loads(result["content"][-1]["text"])["biddesk_provenance"]["notice_id"] == "N1"


def test_provenance_stamp_result_none_is_not_stamped_but_is_still_logged(ledger):
    event = SimpleNamespace(
        tool_use={"name": "read_attachment", "toolUseId": "t1", "input": {"notice_id": "N1"}},
        result=None, exception=None)
    ProvenanceStamp(ledger=ledger).after_tool_call(event)
    assert [r.action for r in ledger.rows()] == ["read"]


def test_provenance_stamp_should_not_log_a_read_for_a_failed_tool(ledger):
    event = SimpleNamespace(
        tool_use={"name": "read_attachment", "toolUseId": "t1", "input": {"notice_id": "N1"}},
        result={"toolUseId": "t1", "status": "error", "content": [{"text": "file not found"}]},
        exception=ValueError("boom"))
    ProvenanceStamp(ledger=ledger).after_tool_call(event)
    assert ledger.rows() == []


def test_provenance_stamp_should_survive_a_non_dict_tool_input(ledger):
    event = SimpleNamespace(
        tool_use={"name": "read_attachment", "toolUseId": "t1", "input": "a bare string"},
        result={"toolUseId": "t1", "status": "success", "content": [{"text": "x"}]},
        exception=None)
    ProvenanceStamp(ledger=ledger).after_tool_call(event)


# ------------------------------------------------------------ 4. TierCounter


def test_tier_counter_counts_one_model_call_per_round_with_a_real_agent():
    counter = TierCounter()
    model = FakeModel([_tool_round("read_attachment", {"notice_id": "N7"}), _text_round("ok")])
    agent = Agent(model=model, tools=[read_attachment], hooks=[counter])
    agent("read it")
    snap = counter.snapshot()
    assert snap["model_calls"] == model.calls == 2
    assert snap["tool_calls"] == 1
    assert snap["per_tool"] == {"read_attachment": 1}


def test_tier_counter_resets_between_real_invocations():
    counter = TierCounter()
    agent = Agent(model=FakeModel([_text_round("a"), _text_round("b")]), hooks=[counter])
    agent("one")
    assert counter.snapshot()["model_calls"] == 1
    agent("two")
    assert counter.snapshot()["model_calls"] == 1      # reset, not 2


def test_tier_counter_persist_accumulates_across_real_invocations():
    counter = TierCounter(persist=True)
    agent = Agent(model=FakeModel([_text_round("a"), _text_round("b")]), hooks=[counter])
    agent("one")
    agent("two")
    assert counter.snapshot()["model_calls"] == 2


# ----------------------------------------------------------------- 5. ledger


# LEDGER-01 fixed: a byte-range lock on Windows, O_APPEND on POSIX.
def test_ledger_append_is_safe_under_two_threads(tmp_path):
    path = tmp_path / "conc.jsonl"
    led = Ledger("f", path=path)

    def worker(tag):
        for i in range(500):
            led.write(notice_id=f"{tag}-{i}", action="filed", why="w", evidence="{}", tier=1)

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = path.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines]      # every line must parse
    assert len(parsed) == 1000
    assert {row["notice_id"] for row in parsed} == {f"{t}-{i}" for t in ("a", "b")
                                                    for i in range(500)}


def test_ledger_concurrent_rows_that_survive_are_all_well_formed(tmp_path):
    """Whatever survives the race is valid JSONL with unique timestamps."""
    path = tmp_path / "conc2.jsonl"
    led = Ledger("f", path=path)

    def worker(tag):
        for i in range(200):
            led.write(notice_id=f"{tag}-{i}", action="filed", why="w", evidence="{}", tier=1)

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = led.rows()
    assert rows, "no rows survived at all"
    stamps = [r.ts for r in rows]
    assert len(set(stamps)) == len(stamps), "duplicate ts would make undo() impossible"


def test_one_corrupt_line_does_not_destroy_the_ledger(ledger):
    good = ledger.write(notice_id="N1", action="filed", why="w", evidence="{}", tier=1)
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-01-01T00:00:00", "firm_s\n')
    ledger.write(notice_id="N2", action="filed", why="w", evidence="{}", tier=1)
    assert [r.notice_id for r in ledger.rows()] == ["N1", "N2"]
    assert ledger.summary()["total"] == 2
    ledger.undo(good.ts, "still undoable")


def test_a_corrupt_line_is_skipped_and_counted_on_every_read_path(ledger):
    """LEDGER-02 fixed: a truncated write is skipped and counted, never fatal."""
    good = ledger.write(notice_id="N1", action="filed", why="w", evidence="{}", tier=1)
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-01-01T00:00:00", "firm_s\n')
    for call in (ledger.rows, ledger.summary, ledger.pending_vetoes, ledger.undone_ts):
        call()
        assert ledger.skipped == 1
    ledger.undo(good.ts, "x")


def test_a_valid_json_line_missing_a_field_is_also_skipped(ledger):
    ledger.write(notice_id="N1", action="filed", why="w", evidence="{}")
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts":"2026-01-01T00:00:00","firm_slug":"f"}\n')
    assert [r.notice_id for r in ledger.rows()] == ["N1"]
    assert ledger.skipped == 1


def test_undoing_an_already_undone_row_is_refused(ledger):
    """LEDGER-03 fixed: a row is undone once; an undo row is not itself undoable."""
    row = ledger.write(notice_id="N1", action="filed", why="w", evidence="{}", tier=1)
    first = ledger.undo(row.ts, "mistake")
    with pytest.raises(ValueError):
        ledger.undo(row.ts, "again")
    with pytest.raises(ValueError):
        ledger.undo(first.ts, "undo the undo")
    assert len(ledger.rows(action="undone")) == 1
    assert ledger.pending_vetoes(now=datetime.now(TZ)) == []


def test_pending_vetoes_is_inclusive_at_exactly_the_window_edge(ledger):
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=TZ)
    for minutes, tag in ((10, "exactly10"), (10.5, "just_over"), (9, "inside"), (-1, "future")):
        ledger.append(LedgerRow(ts=(now - timedelta(minutes=minutes)).isoformat(), firm_slug="f",
                                notice_id=tag, action="filed", why="w", evidence="{}", tier=1))
    pending = [r.notice_id for r in ledger.pending_vetoes(now=now)]
    # LEDGER-04: a row at exactly the window edge is still pending (cutoff <= ts),
    # and a future-dated row is never pending and therefore never finalizes.
    assert pending == ["exactly10", "inside"]


def test_naive_timestamps_are_read_as_central_and_undo_by_exact_string(ledger):
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=TZ)
    naive = (now - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    ledger.append(LedgerRow(ts=naive, firm_slug="f", notice_id="naive", action="filed",
                            why="w", evidence="{}"))
    assert [r.notice_id for r in ledger.pending_vetoes(now=now)] == ["naive"]
    ledger.undo(naive, "undo naive")
    assert ledger.undone_ts() == {naive}
    assert ledger.pending_vetoes(now=now) == []


def test_undo_matches_the_instant_not_the_timestamp_string(ledger):
    """LEDGER-04 fixed: two spellings of the same instant name the same row."""
    now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=TZ)
    aware = (now - timedelta(minutes=5)).isoformat()
    ledger.append(LedgerRow(ts=aware, firm_slug="f", notice_id="aware", action="filed",
                            why="w", evidence="{}"))
    alternate = aware.replace("-05:00", "-0500")
    assert _parse_ts(alternate) == _parse_ts(aware)
    undone = ledger.undo(alternate, "same instant, other spelling")
    assert undone.action == "undone" and aware in undone.evidence


# ------------------------------------------------- 6. no credential can leak


def test_neither_module_reads_prints_or_logs_a_credential():
    import inspect as _inspect
    import biddesk.hooks as hooks_mod
    import biddesk.ledger as ledger_mod
    for mod in (hooks_mod, ledger_mod):
        src = _inspect.getsource(mod)
        # Credential names only. A future legitimate logger in these modules must not
        # fail the build; what matters is that no key is read, printed, or logged.
        for needle in ("SAM_API_KEY", "os.environ", "getenv", "print("):
            assert needle not in src, f"{mod.__name__} mentions {needle}"


def test_secret_named_fields_are_redacted_before_they_reach_the_ledger(ledger):
    guard = NoSubmitGuard(ledger=ledger)
    event = _before_event("submit_bid", {
        "notice_id": "N1",
        "api_key": "SAMKEY-abcdef123456",
        "headers": {"Authorization": "Bearer abcdef"},
        "nested": [{"secret_token": "shh"}],
    })
    guard.before_tool_call(event)
    evidence = ledger.rows()[0].evidence
    assert "SAMKEY-abcdef123456" not in evidence
    assert "Bearer abcdef" not in evidence
    assert "shh" not in evidence
    assert "N1" in evidence


def test_evidence_never_raises_on_an_unserializable_input():
    class Opaque:
        def __repr__(self):
            return "Opaque()"
    out = _evidence({"notice_id": "N1", "obj": Opaque()})
    assert json.loads(out)["obj"] == "Opaque()"


def test_a_key_hidden_in_a_url_query_string_is_redacted(ledger):
    """HOOKS-06 fixed: a credential carried in a query-string value is redacted;
    the rest of the URL and the notice id survive."""
    guard = NoSubmitGuard(ledger=ledger)
    event = _before_event("submit_bid",
                          {"url": "https://api.sam.gov/opportunities?api_key=LIVEKEY123"})
    guard.before_tool_call(event)
    ev = ledger.rows()[0].evidence
    assert "LIVEKEY123" not in ev and "api_key=[redacted]" in ev and "api.sam.gov/opportunities" in ev
