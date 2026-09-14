"""Tests for the bid desk. No Bedrock call is made anywhere in this file.

Import probes run against the installed ``strands-agents`` 1.55.1 on this machine::

    python -c "from strands.multiagent.base import MultiAgentBase, MultiAgentResult, NodeResult, Status; print('ok')"
    python -c "from strands.multiagent.graph import GraphBuilder, GraphState; print('ok')"
    python -c "from strands.models.model import Model; print('ok')"
    python -c "from strands.tools.structured_output.structured_output_tool import StructuredOutputTool; \
               from biddesk.models import DecisionCard; print(StructuredOutputTool(DecisionCard).tool_name)"

The last probe is why :func:`structured_round` names its tool use after the pydantic
class: since 1.55.x, ``structured_output_model=`` is implemented as a forced tool call
whose tool name is the model class name, so a fake model produces structured output by
emitting that tool use.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, AsyncIterable

import pytest
from strands import Agent
from strands.agent.agent_result import AgentResult
from strands.models.model import Model

from biddesk import config, desk
from biddesk.hooks import NoSubmitGuard, TierCounter
from biddesk.ledger import Ledger
from biddesk.models import (
    CalendarEntry,
    DeadlineFindings,
    DecisionCard,
    Drafts,
    FitAssessment,
    KeyDates,
    MatrixRow,
    Notice,
    ReaderFindings,
    Requirement,
)

# --------------------------------------------------------------------- fake model


def tool_round(name: str, tool_input: dict, tool_use_id: str = "tu-1") -> list[dict]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0,
                               "start": {"toolUse": {"toolUseId": tool_use_id, "name": name}}}},
        {"contentBlockDelta": {"contentBlockIndex": 0,
                               "delta": {"toolUse": {"input": json.dumps(tool_input)}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                      "metrics": {"latencyMs": 1}}},
    ]


def text_round(text: str = "done") -> list[dict]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                      "metrics": {"latencyMs": 1}}},
    ]


def structured_round(obj, tool_use_id: str = "so-1") -> list[dict]:
    """A round that satisfies ``structured_output_model=<type(obj)>``."""
    return tool_round(type(obj).__name__, json.loads(obj.model_dump_json()), tool_use_id)


class FakeModel(Model):
    """Replays a fixed list of rounds; each round is a list of StreamEvent dicts."""

    def __init__(self, rounds: list[list[dict]], max_rounds: int = 6) -> None:
        self.rounds = rounds
        self.max_rounds = max_rounds
        self.calls = 0
        self.seen_messages: list[Any] = []
        self._config: dict = {}

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> Any:
        return self._config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("structured output goes through the tool path")
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs) -> AsyncIterable[dict]:
        self.calls += 1
        self.seen_messages.append(messages)
        if self.calls > self.max_rounds:
            raise RuntimeError(f"fake model called {self.calls} times; loop not terminating")
        events = self.rounds[self.calls - 1] if self.calls <= len(self.rounds) else text_round("stop")
        for ev in events:
            yield ev


# ----------------------------------------------------------------------- fixtures


def req(rid: str, text: str, category: str = "scope", page: int = 1,
        source_file: str = "a.pdf") -> Requirement:
    return Requirement(id=rid, text=text, source_file=source_file, page=page, category=category)


def row(rid: str, quote: str, page: int = 1, source_file: str = "a.pdf",
        status: str = "meets") -> MatrixRow:
    return MatrixRow(requirement_id=rid, quote=quote, source_file=source_file, page=page,
                     firm_answer="the firm does this", status=status, evidence="capabilities line 1")


@pytest.fixture
def notice() -> Notice:
    return Notice(
        notice_id="n" * 32,
        title="Tree removal at the depot",
        type="Combined Synopsis/Solicitation",
        base_type="Combined Synopsis/Solicitation",
        posted="2026-09-01",
        response_deadline="2026-09-30T17:00:00-05:00",
        ui_link="https://sam.gov/opp/nnn/view",
        description_text="The contractor shall remove sixteen hazard trees. Work is in Norman, Oklahoma.",
        fetched_at="2026-09-13T20:00:00-05:00",
    )


@pytest.fixture
def firm():
    from biddesk import profiles
    return profiles.PROFILES["red-cedar"]


# --------------------------------------------------------------------- build_task


def test_build_task_caps_requirements_and_states_the_cap(notice, firm):
    reqs = [req(f"r{i}", f"Requirement number {i} shall apply.") for i in range(150)]
    task = desk.build_task(notice, firm, reqs, days_to_close=12.5)
    assert "120 of 150 requirements shown" in task
    assert task.count("Requirement number") == desk.MAX_REQUIREMENTS
    assert "days to close (computed in code, do not recompute): 12.5" in task


def test_build_task_orders_by_category_then_document_order(notice, firm):
    reqs = [
        req("r-scope-1", "Scope one.", "scope"),
        req("r-sub-1", "Submission one.", "submission"),
        req("r-eval-1", "Evaluation one.", "evaluation"),
        req("r-sub-2", "Submission two.", "submission"),
        req("r-other-1", "Other one.", "other"),
    ]
    task = desk.build_task(notice, firm, reqs)
    order = [task.index(f"[{r.id}]") for r in reqs]
    by_id = dict(zip([r.id for r in reqs], order))
    assert by_id["r-sub-1"] < by_id["r-sub-2"] < by_id["r-eval-1"] < by_id["r-scope-1"] < by_id["r-other-1"]


def test_build_task_caps_the_description(notice, firm):
    long_notice = notice.model_copy(update={"description_text": "word " * 4000})
    task = desk.build_task(long_notice, firm, [])
    assert f"{desk.MAX_DESCRIPTION} of " in task
    assert len(task) < 20000


def test_build_task_quotes_the_owner_rules_verbatim(notice, firm):
    task = desk.build_task(notice, firm, [])
    for rule in firm.rules:
        assert rule.text in task


# ----------------------------------------------------------------- validate_matrix


def test_validate_matrix_keeps_a_verbatim_quote():
    reqs = [req("r1", "The contractor shall remove sixteen hazard trees.")]
    kept, dropped = desk.validate_matrix([row("r1", "remove sixteen hazard trees")], reqs)
    assert len(kept) == 1 and dropped == []


def test_validate_matrix_drops_a_paraphrase():
    reqs = [req("r1", "The contractor shall remove sixteen hazard trees.")]
    kept, dropped = desk.validate_matrix([row("r1", "remove 16 hazardous trees")], reqs)
    assert kept == [] and len(dropped) == 1


def test_validate_matrix_drops_a_wrong_page_or_file():
    reqs = [req("r1", "The contractor shall remove sixteen hazard trees.", page=3, source_file="sow.pdf")]
    quote = "remove sixteen hazard trees"
    kept, dropped = desk.validate_matrix([row("r1", quote, page=4, source_file="sow.pdf")], reqs)
    assert kept == [] and len(dropped) == 1
    kept, dropped = desk.validate_matrix([row("r1", quote, page=3, source_file="other.pdf")], reqs)
    assert kept == [] and len(dropped) == 1
    kept, dropped = desk.validate_matrix([row("r1", quote, page=3, source_file="sow.pdf")], reqs)
    assert len(kept) == 1 and dropped == []


def test_validate_matrix_drops_an_unknown_requirement_id():
    reqs = [req("r1", "The contractor shall remove sixteen hazard trees.")]
    kept, dropped = desk.validate_matrix([row("r9", "remove sixteen hazard trees")], reqs)
    assert kept == [] and len(dropped) == 1


def test_validate_matrix_normalizes_whitespace_but_not_case():
    reqs = [req("r1", "The contractor shall\n  remove sixteen\thazard trees.")]
    kept, _ = desk.validate_matrix([row("r1", "remove sixteen hazard   trees")], reqs)
    assert len(kept) == 1
    kept, dropped = desk.validate_matrix([row("r1", "Remove Sixteen Hazard Trees")], reqs)
    assert kept == [] and len(dropped) == 1


def test_matrix_summary_counts_statuses():
    rows = [row("r1", "a", status="meets"), row("r2", "b", status="partial"),
            row("r3", "c", status="gap")]
    assert desk.matrix_summary(rows) == "1 of 3 requirements met, 1 partial, 1 gap, 0 unknown"
    assert "0 requirements checked" in desk.matrix_summary([])


# ----------------------------------------------------------------- validate_quotes


def test_validate_quotes_against_the_corpus(notice):
    reqs = [req("r1", "Offers are due at 5:00 p.m. Central on 30 September 2026.")]
    corpus = desk.QuoteCorpus(notice, reqs)
    kept, dropped = desk.validate_quotes(
        ["Offers are due at 5:00 p.m. Central", "remove sixteen hazard trees",
         "Offers are due next Tuesday", ""],
        corpus,
    )
    assert kept == ["Offers are due at 5:00 p.m. Central", "remove sixteen hazard trees"]
    assert dropped == ["Offers are due next Tuesday", ""]


def test_quote_corpus_normalizes_whitespace(notice):
    corpus = desk.QuoteCorpus(notice, [req("r1", "The  contractor\nshall   remove")])
    assert corpus.contains("The contractor shall remove")


# ------------------------------------------------------------- graph and merging


class _FakeAgent:
    """The smallest thing SpecialistNode needs: invoke_async and a metrics object."""

    def __init__(self, output=None, error: Exception | None = None) -> None:
        self.output = output
        self.error = error

    async def invoke_async(self, task, **kwargs):
        if self.error is not None:
            raise self.error
        return AgentResult(
            stop_reason="end_turn",
            message={"role": "assistant", "content": [{"text": "ok"}]},
            metrics=SimpleNamespace(accumulated_usage={"inputTokens": 7, "outputTokens": 3,
                                                       "totalTokens": 10}),
            state={},
            structured_output=self.output,
        )


def _specialists(fail: str | None = None) -> dict[str, desk.SpecialistNode]:
    outputs = {
        "reader": ReaderFindings(notice_id="n1", key_requirement_ids=["r1"], scope_summary="s",
                                 evaluation_basis="lpta", incumbent_or_history="none found", quotes=[]),
        "fit": FitAssessment(notice_id="n1", fit_score=0.5, matched_past_performance=[],
                             strengths=[], gaps=[], matrix=[]),
        "deadlines": DeadlineFindings(notice_id="n1", key_dates=KeyDates(), days_to_close=3.0,
                                      set_aside_note="ok", amendment_note="no amendment",
                                      calendar_entries=[]),
    }
    nodes = {}
    for name, out in outputs.items():
        if name == fail:
            nodes[name] = desk.SpecialistNode(name, _FakeAgent(error=RuntimeError("throttled")))
        else:
            nodes[name] = desk.SpecialistNode(name, _FakeAgent(output=out))
    return nodes


def test_graph_merges_three_specialists():
    nodes = _specialists()
    merge = desk.DeskMerge(nodes)
    graph = desk.build_graph(nodes, merge)
    result = graph("task text")
    assert set(merge.collected) == {"reader", "fit", "deadlines"}
    assert merge.errors == {}
    assert result.results["desk_merge"].status.name == "COMPLETED"


def test_graph_survives_one_failed_specialist_and_still_merges():
    nodes = _specialists(fail="fit")
    merge = desk.DeskMerge(nodes)
    graph = desk.build_graph(nodes, merge)
    graph("task text")
    assert set(merge.collected) == {"reader", "deadlines"}
    assert "fit" in merge.errors and "RuntimeError" in merge.errors["fit"]
    # the node reported COMPLETED so the documented AND condition still fired
    assert nodes["fit"].output is None


def test_desk_merge_publishes_on_the_invocation_state():
    nodes = _specialists(fail="reader")
    merge = desk.DeskMerge(nodes)
    graph = desk.build_graph(nodes, merge)
    graph("task text")
    state: dict = {}
    import asyncio
    asyncio.run(merge.invoke_async("task", state))
    assert set(state["specialists"]) == {"fit", "deadlines"}
    assert "reader" in state["specialist_errors"]


def test_unavailable_placeholders_never_invent_content():
    rdr = desk._unavailable_reader("n1")
    assert rdr.scope_summary == "unavailable" and rdr.quotes == []
    fit = desk._unavailable_fit("n1")
    assert fit.matrix == [] and fit.fit_score == 0.0
    dls = desk._unavailable_deadlines("n1", 4.0, "no amendment")
    assert dls.key_dates.proposal_due is None and dls.calendar_entries == []


# ------------------------------------------------------------------ refusal scene


def test_refusal_scene_denies_the_tool_and_never_runs_its_body(tmp_path, monkeypatch):
    ledger = Ledger("plains-med", path=tmp_path / "plains-med.jsonl")
    fake = FakeModel([
        tool_round("sam_submit_offer", {"notice_id": "abc", "offer_text": "we offer"}),
        text_round("I cannot file on your behalf; here is the draft instead."),
    ])
    monkeypatch.setattr(desk, "make_model", lambda model_id, max_tokens: fake)
    before = len(desk.SUBMIT_CALLS)

    out = desk.run_refusal_scene("plains-med", ledger=ledger)

    assert out["tool_body_calls"] == 0
    assert len(desk.SUBMIT_CALLS) == before
    denied = ledger.rows(action="denied_tool")
    assert len(denied) == 1
    assert "sam_submit_offer" in denied[0].why
    assert out["denied_row"]["action"] == "denied_tool"
    assert "cannot file" in out["agent_text"] or "draft" in out["agent_text"]


def test_the_submit_tool_is_matched_by_the_default_deny_list():
    guard = NoSubmitGuard(firm_slug="x", ledger=Ledger("x", path=config.DATA / "ledger" / "_unused.jsonl"))
    from biddesk.hooks import deny_reason
    assert deny_reason("sam_submit_offer", {}, guard.deny_tools) is not None


# ---------------------------------------------------------------- fallback path


def test_run_case_falls_back_when_the_primary_raises(monkeypatch, notice, tmp_path):
    notice = notice.model_copy(update={"description_text": "The contractor shall mow. " * 40})
    calls: list[tuple[str, bool]] = []

    def fake_run_once(slug, n, profile, requirements, model_id, fallback_used, allow_degraded,
                      ledger, counter=None, forced_model=None, extracted_at=None):
        calls.append((model_id, allow_degraded))
        if not fallback_used:
            raise desk.DeskError("specialist fit failed: ThrottlingException")
        return SimpleNamespace(model_used=model_id, fallback_used=True)

    monkeypatch.setattr(desk, "_run_once", fake_run_once)
    monkeypatch.setattr(desk, "load_requirements", lambda n: [])
    result = desk.run_case("red-cedar", notice, model="sonnet",
                           ledger=Ledger("red-cedar", path=tmp_path / "l.jsonl"))
    assert result.fallback_used is True
    assert [c[0] for c in calls] == [config.MODEL_PRIMARY, config.MODEL_FALLBACK]
    assert calls[0][1] is False and calls[1][1] is True


def test_run_case_haiku_flag_goes_straight_to_the_fallback(monkeypatch, notice, tmp_path):
    """--model haiku pins the model and says so; it is not a fallback (D6)."""
    notice = notice.model_copy(update={"description_text": "The contractor shall mow. " * 40})
    seen: list[str] = []

    def fake_run_once(slug, n, profile, requirements, model_id, fallback_used, allow_degraded,
                      ledger, counter=None, forced_model=None, extracted_at=None):
        seen.append(model_id)
        return SimpleNamespace(model_used=model_id, fallback_used=fallback_used,
                               forced_model=forced_model)

    monkeypatch.setattr(desk, "_run_once", fake_run_once)
    monkeypatch.setattr(desk, "load_requirements", lambda n: [])
    result = desk.run_case("red-cedar", notice, model="haiku",
                           ledger=Ledger("red-cedar", path=tmp_path / "l.jsonl"))
    assert seen == [config.MODEL_FALLBACK]
    assert result.fallback_used is False and result.forced_model == "haiku"


def test_a_notice_with_no_readable_evidence_is_skipped_not_surfaced(monkeypatch, notice, tmp_path):
    """D4: no requirement and a description too short to reason from means a skipped row."""
    monkeypatch.setattr(desk, "load_requirements", lambda n: [])
    monkeypatch.setattr(desk, "_run_once", lambda *a, **k: pytest.fail("the desk should not run"))
    thin = notice.model_copy(update={"description_text": "Sources sought."})
    ledger = Ledger("red-cedar", path=tmp_path / "l.jsonl")
    assert desk.run_case("red-cedar", thin, ledger=ledger) is None
    rows = ledger.rows(action="skipped")
    assert len(rows) == 1 and rows[0].why == "no readable evidence"


def test_a_long_description_alone_is_enough_to_surface(monkeypatch, notice, tmp_path):
    monkeypatch.setattr(desk, "load_requirements", lambda n: [])
    monkeypatch.setattr(desk, "_run_once",
                        lambda *a, **k: SimpleNamespace(fallback_used=False, forced_model=None))
    fat = notice.model_copy(update={"description_text": "The contractor shall mow. " * 40})
    assert desk.run_case("red-cedar", fat,
                         ledger=Ledger("red-cedar", path=tmp_path / "l2.jsonl")) is not None


def test_fit_from_matrix_scores_in_code_and_excludes_unknown():
    """D3/D10: the number on the card is a function of the validated rows, not of the model."""
    assert desk.fit_from_matrix([]) is None
    assert desk.fit_from_matrix([row("r1", "q", status="unknown")]) is None
    assert desk.fit_from_matrix([row("r1", "q", status="meets"),
                                 row("r2", "q", status="gap")]) == 0.5
    assert desk.fit_from_matrix([row("r1", "q", status="meets"),
                                 row("r2", "q", status="partial"),
                                 row("r3", "q", status="unknown")]) == 0.75


def test_validate_matrix_labels_every_drop_with_a_reason():
    reqs = [req("r1", "The contractor shall remove sixteen hazard trees.", page=3, source_file="s.pdf")]
    cases = {
        "empty_quote": row("r1", "", page=3, source_file="s.pdf"),
        "unknown_requirement": row("r9", "remove sixteen hazard trees", page=3, source_file="s.pdf"),
        "not_verbatim": row("r1", "remove 16 trees", page=3, source_file="s.pdf"),
        "wrong_source": row("r1", "remove sixteen hazard trees", page=4, source_file="s.pdf"),
    }
    for reason, bad in cases.items():
        kept, dropped = desk.validate_matrix([bad], reqs)
        assert kept == [] and [d.reason for d in dropped] == [reason]


# ------------------------------------------------------------------ small helpers


def test_minus_business_days_skips_the_weekend():
    # 2026-09-30 is a Wednesday; two business days back is Monday 2026-09-28
    assert desk.minus_business_days("2026-09-30T17:00:00-05:00", 2) == "2026-09-28"
    # 2026-09-29 is a Tuesday; two business days back is Friday 2026-09-25
    assert desk.minus_business_days("2026-09-29", 2) == "2026-09-25"
    assert desk.minus_business_days(None) is None


def test_amendment_note_skips_the_network_when_nothing_changed(notice, monkeypatch):
    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("the desk fetched the network for a notice with no amendment")

    monkeypatch.setattr(desk.sam_client, "fetch_notice_public", boom)
    assert desk.amendment_note(notice) == "no amendment"


def test_amendment_note_is_offline_safe(notice, monkeypatch):
    amended = notice.model_copy(update={"base_type": "Presolicitation"})

    def raise_sam(*a, **k):
        raise desk.sam_client.SamError("offline")

    monkeypatch.setattr(desk.sam_client, "fetch_notice_public", raise_sam)
    assert desk.amendment_note(amended) == "amendment history unavailable offline"


# ------------------------------------------------------------------- the gallery


def _case_result(slug: str, notice_id: str) -> dict:
    card = DecisionCard(notice_id=notice_id, firm_slug=slug, situation="two lines",
                        recommendation="bid", reasons_for=["a [r1]"], reasons_against=["b [r1]"],
                        default=desk.DEFAULT_TEXT, decide_by="2026-09-28", key_dates=KeyDates(),
                        fit_score=0.7, matrix_summary="1 of 1 requirements met, 0 partial, 0 gap, 0 unknown",
                        evidence_link="https://sam.gov/opp/x/view")
    drafts = Drafts(capability_statement="short", questions_for_co=["q1"],
                    calendar_entries=[CalendarEntry(title="Proposal due", date="2026-09-30",
                                                    note="n", source_quote="Offers are due")])
    return {
        "notice_id": notice_id, "firm_slug": slug, "model_used": "test-model", "fallback_used": False,
        "card": json.loads(card.model_dump_json()), "matrix": [json.loads(row("r1", "q").model_dump_json())],
        "matrix_dropped": 2, "drafts": json.loads(drafts.model_dump_json()),
        "reader": json.loads(desk._unavailable_reader(notice_id).model_dump_json()),
        "fit": json.loads(desk._unavailable_fit(notice_id).model_dump_json()),
        "deadlines": json.loads(desk._unavailable_deadlines(notice_id, 3.0, "no amendment").model_dump_json()),
        "model_calls": 5, "tokens_in": 100, "tokens_out": 50, "seconds": 12.0,
        "produced_at": "2026-09-13T21:00:00-05:00",
    }


def test_gallery_assembly_shape(tmp_path, monkeypatch):
    from biddesk import ledger as ledger_mod
    monkeypatch.setattr(config, "GALLERY", tmp_path / "gallery")
    monkeypatch.setattr(desk.config, "GALLERY", tmp_path / "gallery")
    monkeypatch.setattr(ledger_mod, "LEDGER_DIR", tmp_path / "ledger")

    notice_id = "a" * 32
    path = tmp_path / "gallery" / "cases" / "red-cedar" / f"{notice_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_case_result("red-cedar", notice_id)), encoding="utf-8")
    Ledger("red-cedar").write(notice_id=notice_id, action="surfaced", why="bid",
                              evidence="{}", tier=2, undo="mark card as filed")

    Ledger("red-cedar").write(notice_id=notice_id, action="surfaced", why="bid (re-run)",
                              evidence="{}", tier=2, undo="mark card as filed")

    index = desk.build_gallery()

    assert index["case_count"] == 1
    assert (tmp_path / "gallery" / "index.json").exists()
    assert "produced_at" in index and isinstance(index["sources"], list)
    firms = {f["slug"]: f for f in index["firms"]}
    assert set(firms) == {"red-cedar", "sooner-systems", "plains-med"}
    rc = firms["red-cedar"]
    assert rc["fictional"] is True and rc["name"] and rc["city"] and rc["state"] and rc["what"]
    assert rc["rules"] and all("text" in r for r in rc["rules"])
    assert rc["tiers"] == desk.tier_block("red-cedar")
    assert rc["tiers"]["tier2"] >= 1
    case = rc["cases"][0]
    assert {"title", "type", "agency", "deadline", "days_to_close", "ui_link"} <= set(case)
    assert case["card"]["recommendation"] == "bid"
    assert case["matrix_kept"] == 1 and case["matrix_dropped"] == 2
    # two surfaced rows on file, one on the card: the run it ships (D5)
    assert [r["action"] for r in case["ledger_rows"]] == ["surfaced"]
    assert case["ledger_rows"][0]["why"] == "bid (re-run)"
    assert rc["ledger_summary"]["total"] == 2
    assert firms["plains-med"]["cases"] == []


def test_every_gallery_source_name_is_verbatim_in_sources_md():
    text = (config.DATA / "SOURCES.md").read_text(encoding="utf-8")
    sources = desk.read_sources()
    assert sources
    for entry in sources:
        assert entry["name"] in text


# ------------------------------------------------------- structured output plumbing


def test_fake_model_drives_a_structured_output_agent():
    """Guards the test double itself: the tool-name contract of structured_output_model."""
    card = DecisionCard(notice_id="n1", firm_slug="red-cedar", situation="two lines",
                        recommendation="no-bid", reasons_for=["a [r1]"], reasons_against=["b [r1]"],
                        default=desk.DEFAULT_TEXT, decide_by=None, key_dates=KeyDates(),
                        fit_score=0.2, matrix_summary="0 requirements checked",
                        evidence_link="https://sam.gov/opp/x/view")
    agent = Agent(model=FakeModel([structured_round(card)]), callback_handler=None,
                  hooks=[TierCounter(persist=True)])
    result = agent("decide", structured_output_model=DecisionCard)
    assert result.structured_output.recommendation == "no-bid"
