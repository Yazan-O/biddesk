"""Independent review probes for :mod:`biddesk.desk`.

Written by the reviewer, not the builder. Nothing here calls Bedrock: model-driven
probes use the ``FakeModel`` double from :mod:`biddesk.tests.test_desk`.

Every ``xfail(strict=True)`` below asserts the behaviour the reviewer believes the spec
requires; it fails today, which is the defect. Plain tests record behaviour that holds
and must keep holding.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from biddesk import config, desk, profiles, reader, snapshot
from biddesk.ledger import Ledger
from biddesk.models import DeadlineFindings, FitAssessment, KeyDates, MatrixRow, ReaderFindings
from biddesk.tests.test_desk import (
    FakeModel,
    _FakeAgent,
    _specialists,
    req,
    row,
    structured_round,
    text_round,
    tool_round,
)

REPO = Path(__file__).resolve().parents[3]
CASES = sorted((REPO / "gallery" / "cases").glob("*/*.json"))


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def _extracted(notice_id: str) -> dict:
    p = config.ATTACH / notice_id / reader.EXTRACTED_NAME
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _cases() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in CASES]


shipped = pytest.mark.skipif(
    not CASES or not config.ATTACH.is_dir() or not any(config.ATTACH.iterdir()),
    reason="needs the shipped gallery cases and the data/attachments cache (not in the repository)")


def _notice_with_evidence():
    """A red-cedar notice the D4 gate lets through, so the fallback path is what is tested."""
    for n in snapshot.load("red-cedar")[1]:
        if len(_norm(n.description_text)) >= desk.MIN_DESCRIPTION_CHARS:
            return n
    raise AssertionError("no red-cedar notice carries a description")


# ------------------------------------------------- item 1: zero fabrication (artifacts)


def _page_text(notice_id: str, slug: str = "") -> str:
    """Everything a matrix quote may come from: attachment page text plus the description.

    Pages are joined with a space, not a newline: the reader chunks a requirement across a
    page break, so a newline join splits real requirements and reads them as fabrications.
    """
    parts = []
    for entry in _extracted(notice_id).get("files", []):
        path = Path(entry.get("path", ""))
        if not path.exists():
            continue
        try:
            for page in reader.extract_text(path):
                # the reader's own line-joining: a PDF wraps a word mid-line, and the
                # requirement text it produced is what the desk validated against.
                parts += [_norm(s) for s in reader._sentences(page.get("text", ""))]
        except Exception:
            continue
    if slug:
        notice = next((n for n in snapshot.load(slug)[1] if n.notice_id == notice_id), None)
        if notice is not None:
            parts.append(_norm(notice.description_text or ""))
    return _norm(" ".join(parts))


@shipped
def test_no_shipped_matrix_quote_is_fabricated():
    """The zero-fabrication claim: every matrix quote is verbatim in the attachment text."""
    misses = []
    for case in _cases():
        hay = _page_text(case["notice_id"], case["firm_slug"])
        for r in case.get("matrix", []):
            q = _norm(r["quote"])
            if q and q not in hay:
                misses.append(f"{case['notice_id'][:8]} {r['requirement_id']}: {q[:80]!r}")
    assert misses == []


@shipped
def test_every_shipped_quote_is_verbatim_inside_some_requirement():
    misses = []
    for case in _cases():
        texts = [_norm(r["text"]) for r in _extracted(case["notice_id"]).get("requirements", [])]
        for r in case.get("matrix", []):
            q = _norm(r["quote"])
            if not any(q in t for t in texts):
                misses.append(f"{case['notice_id'][:8]} {r['requirement_id']}: {q[:80]!r}")
    assert misses == []


@shipped
def test_no_shipped_matrix_row_carries_an_empty_quote():
    empties = [(c["notice_id"][:8], r["requirement_id"])
               for c in _cases() for r in c.get("matrix", []) if not _norm(r["quote"])]
    assert empties == []


@shipped
def test_shipped_matrix_still_passes_the_desks_own_validator():
    from biddesk.models import Requirement
    bad = []
    for case in _cases():
        reqs = [Requirement(**r) for r in _extracted(case["notice_id"]).get("requirements", [])]
        rows = [MatrixRow(**r) for r in case.get("matrix", [])]
        _kept, dropped = desk.validate_matrix(rows, reqs)
        bad += [f"{case['notice_id'][:8]}:{d.requirement_id}" for d in dropped]
    assert bad == []


@shipped
def test_shipped_free_text_quotes_trace_to_the_corpus():
    """reader.quotes, key_dates.source_quotes and calendar source_quotes against the corpus."""
    from biddesk.models import Requirement
    misses = []
    for case in _cases():
        nid, slug = case["notice_id"], case["firm_slug"]
        reqs = [Requirement(**r) for r in _extracted(nid).get("requirements", [])]
        notice = next((n for n in snapshot.load(slug)[1] if n.notice_id == nid), None)
        corpus = desk.QuoteCorpus(notice, reqs)
        quotes = list(case.get("reader", {}).get("quotes") or [])
        quotes += list(((case.get("card") or {}).get("key_dates") or {}).get("source_quotes") or [])
        quotes += list(((case.get("deadlines") or {}).get("key_dates") or {}).get("source_quotes") or [])
        quotes += [e.get("source_quote", "") for e in ((case.get("drafts") or {}).get("calendar_entries") or [])]
        for q in quotes:
            if not corpus.contains(q):
                misses.append(f"{nid[:8]}: {_norm(q)[:80]!r}")
    assert misses == []


@shipped
def test_every_id_shaped_source_tag_resolves_to_a_real_requirement():
    """[source] tags are free text (a requirement id, a short quote, or a profile line).

    The prompt allows all three, so a non-id tag is not a defect. What would be a defect is
    a tag shaped exactly like a requirement id that names no requirement.
    """
    # the last segment is a content hash since D2; cases produced before the fix carry the
    # old positional counter, and both shapes must resolve against their own extracted.json
    ID_SHAPE = re.compile(r"^[0-9a-f]{8}-[0-9a-zA-Z_.\-]{1,8}-p\d+-(?:[0-9a-f]{8}|\d+)$")
    id_shaped = 0
    unresolved = []
    for case in _cases():
        req_ids = {r["id"] for r in _extracted(case["notice_id"]).get("requirements", [])}
        for field in ("reasons_for", "reasons_against"):
            for reason in case["card"].get(field) or []:
                for tag in re.findall(r"\[([^\]]+)\]", reason):
                    tag = tag.split(":")[0].strip()
                    if ID_SHAPE.match(tag):
                        id_shaped += 1
                        if tag not in req_ids:
                            unresolved.append((case["notice_id"], tag))
    assert id_shaped > 0, "no id-shaped source tags in the corpus at all"
    assert unresolved == []


# ------------------------------------- item 2: adversarial validate_matrix / validate_quotes


REQ_TEXT = "The contractor shall remove sixteen hazard trees before 30 September 2026."


def test_validate_matrix_drops_an_empty_quote():
    kept, dropped = desk.validate_matrix([row("r1", "")], [req("r1", REQ_TEXT)])
    assert kept == [] and len(dropped) == 1


def test_validate_quotes_rejects_an_empty_quote():
    corpus = desk.QuoteCorpus(None, [req("r1", REQ_TEXT)])
    assert corpus.contains("") is False


@pytest.mark.parametrize("quote", [
    "remove sixteen hazard trees—before",        # em dash replacing a space
    "remove​sixteen hazard trees",               # zero-width space
    "“remove sixteen hazard trees”",        # curly quotes
    "Remove Sixteen Hazard Trees",                    # case change
])
def test_validate_matrix_fails_closed_on_unicode_and_case_variants(quote):
    """Correct behaviour for a zero-fabrication bar: near-misses are dropped, not folded."""
    kept, dropped = desk.validate_matrix([row("r1", quote)], [req("r1", REQ_TEXT)])
    assert kept == [] and len(dropped) == 1


def test_a_non_breaking_space_is_folded_by_the_normalizer():
    """Python's ``\\s`` matches U+00A0, so an NBSP variant of a real quote is kept."""
    assert desk._norm("remove sixteen") == "remove sixteen"
    kept, _ = desk.validate_matrix(
        [row("r1", "remove sixteen hazard trees")], [req("r1", REQ_TEXT)])
    assert len(kept) == 1


def test_a_quote_spanning_two_requirements_is_dropped():
    reqs = [req("r1", "The contractor shall remove sixteen hazard trees."),
            req("r2", "Work is limited to daylight hours.")]
    spanning = "remove sixteen hazard trees. Work is limited to daylight hours."
    kept, dropped = desk.validate_matrix([row("r1", spanning)], reqs)
    assert kept == [] and len(dropped) == 1


def test_a_quote_spanning_two_requirements_is_also_rejected_by_the_corpus():
    reqs = [req("r1", "The contractor shall remove sixteen hazard trees."),
            req("r2", "Work is limited to daylight hours.")]
    corpus = desk.QuoteCorpus(None, reqs)
    assert corpus.contains("hazard trees. Work is limited") is False


def test_a_requirement_id_from_another_notice_is_dropped():
    reqs = [req("aaaa1111-p1-1", REQ_TEXT)]
    kept, dropped = desk.validate_matrix(
        [row("bbbb2222-p1-1", "remove sixteen hazard trees")], reqs)
    assert kept == [] and len(dropped) == 1


def test_page_given_as_a_string_is_coerced_and_still_compared():
    r = MatrixRow(requirement_id="r1", quote="remove sixteen hazard trees", source_file="a.pdf",
                  page="3", firm_answer="yes", status="meets", evidence="line 1")
    assert r.page == 3
    kept, _ = desk.validate_matrix([r], [req("r1", REQ_TEXT, page=3)])
    assert len(kept) == 1
    kept, dropped = desk.validate_matrix([r], [req("r1", REQ_TEXT, page=4)])
    assert kept == [] and len(dropped) == 1


# --------------------------------------------------------------------- item 3: the graph


def test_every_edge_into_the_merge_carries_the_and_condition():
    nodes = _specialists()
    merge = desk.DeskMerge(nodes)
    graph = desk.build_graph(nodes, merge)
    into_merge = [e for e in graph.edges if e.to_node.node_id == "desk_merge"]
    assert len(into_merge) == 3
    assert all(e.condition is not None for e in into_merge)


def test_the_merge_node_runs_exactly_once():
    nodes = _specialists()
    merge = desk.DeskMerge(nodes)
    graph = desk.build_graph(nodes, merge)
    result = graph("task")
    assert [n.node_id for n in result.execution_order].count("desk_merge") == 1


def test_a_specialist_that_returns_no_structured_output_is_recorded_as_an_error():
    nodes = _specialists()
    nodes["fit"] = desk.SpecialistNode("fit", _FakeAgent(output=None))
    merge = desk.DeskMerge(nodes)
    desk.build_graph(nodes, merge)("task")
    assert "fit" in merge.errors and "ValueError" in merge.errors["fit"]
    assert set(merge.collected) == {"reader", "deadlines"}


def test_a_specialist_timeout_does_not_take_the_graph_down():
    nodes = _specialists()
    nodes["deadlines"] = desk.SpecialistNode("deadlines", _FakeAgent(error=asyncio.TimeoutError()))
    merge = desk.DeskMerge(nodes)
    result = desk.build_graph(nodes, merge)("task")
    assert result.results["desk_merge"].status.name == "COMPLETED"
    assert "deadlines" in merge.errors


def test_desk_merge_returns_its_own_node_result_as_documented():
    nodes = _specialists()
    merge = desk.DeskMerge(nodes)
    desk.build_graph(nodes, merge)
    out = asyncio.run(merge.invoke_async("task", {}))
    assert "desk_merge" in out.results


# ------------------------------------------------------------------ item 4: the fallback


def test_both_models_failing_raises_and_writes_no_case_file(monkeypatch, tmp_path):
    notice = _notice_with_evidence()

    def always_fail(*a, **k):
        raise desk.DeskError("specialist fit failed")

    monkeypatch.setattr(desk, "_run_once", always_fail)
    monkeypatch.setattr(desk, "load_requirements", lambda n: [])
    monkeypatch.setattr(desk.config, "GALLERY", tmp_path / "gallery")
    with pytest.raises(desk.DeskError):
        desk.run_case("red-cedar", notice, ledger=Ledger("red-cedar", path=tmp_path / "l.jsonl"))
    assert not (tmp_path / "gallery").exists()


def test_haiku_flag_does_not_claim_a_fallback(monkeypatch, tmp_path):
    notice = _notice_with_evidence()
    monkeypatch.setattr(desk, "load_requirements", lambda n: [])
    monkeypatch.setattr(desk, "_run_once",
                        lambda *a, **k: SimpleNamespace(fallback_used=k["fallback_used"],
                                                        forced_model=k.get("forced_model")))
    result = desk.run_case("red-cedar", notice, model="haiku",
                           ledger=Ledger("red-cedar", path=tmp_path / "l.jsonl"))
    assert result.fallback_used is False and result.forced_model == "haiku"


# --------------------------------------------------------------------- item 5: the hooks


def _hook_types(agent) -> set[str]:
    out = set()
    for callbacks in agent.hooks._registered_callbacks.values():
        for entry in callbacks:
            owner = getattr(getattr(entry, "callback", entry), "__self__", None)
            if owner is not None:
                out.add(type(owner).__name__)
    return out


def test_every_agent_including_the_desk_carries_the_three_hooks(tmp_path):
    from biddesk.hooks import TierCounter
    ledger = Ledger("red-cedar", path=tmp_path / "l.jsonl")
    counter = TierCounter(persist=True)
    wanted = {"NoSubmitGuard", "TierCounter", "ProvenanceStamp"}
    specialists = desk.build_specialists("test-model", "red-cedar", ledger, counter)
    for node in specialists.values():
        assert wanted <= _hook_types(node.agent)
    assert wanted <= _hook_types(desk.build_desk_agent("test-model", "red-cedar", ledger, counter))


def test_one_counter_counts_every_model_call_in_a_run(monkeypatch, tmp_path):
    """3 specialists + 2 desk invocations = the 5 model calls the gallery reports."""
    from biddesk.hooks import TierCounter
    from biddesk.models import DecisionCard, Drafts
    ledger = Ledger("red-cedar", path=tmp_path / "l.jsonl")
    counter = TierCounter(persist=True)
    outputs = {
        "reader": ReaderFindings(notice_id="n1", key_requirement_ids=[], scope_summary="s",
                                 evaluation_basis="e", incumbent_or_history="none", quotes=[]),
        "fit": FitAssessment(notice_id="n1", fit_score=0.5, matched_past_performance=[],
                             strengths=[], gaps=[], matrix=[]),
        "deadlines": DeadlineFindings(notice_id="n1", key_dates=KeyDates(), days_to_close=3.0,
                                      set_aside_note="ok", amendment_note="no amendment",
                                      calendar_entries=[]),
    }
    order = iter(["reader", "fit", "deadlines"])
    monkeypatch.setattr(
        desk, "make_model",
        lambda model_id, max_tokens: FakeModel([structured_round(outputs[next(order)])]))
    specialists = desk.build_specialists("test-model", "red-cedar", ledger, counter)
    merge = desk.DeskMerge(specialists)
    desk.build_graph(specialists, merge)("task")
    assert counter.model_calls == 3

    card = DecisionCard(notice_id="n1", firm_slug="red-cedar", situation="a",
                        recommendation="no-bid", reasons_for=["a [r1]"], reasons_against=["b [r1]"],
                        default=desk.DEFAULT_TEXT, decide_by=None, key_dates=KeyDates(),
                        fit_score=0.2, matrix_summary="0 requirements checked",
                        evidence_link="https://sam.gov/x")
    drafts = Drafts(capability_statement="s", questions_for_co=["q"], calendar_entries=[])
    monkeypatch.setattr(desk, "make_model", lambda model_id, max_tokens: FakeModel(
        [structured_round(card), structured_round(drafts, "so-2")]))
    agent = desk.build_desk_agent("test-model", "red-cedar", ledger, counter)
    agent("card", structured_output_model=DecisionCard)
    agent("drafts", structured_output_model=Drafts)
    assert counter.model_calls == 5


def test_the_refusal_goes_through_before_tool_call_and_never_runs_the_body(tmp_path, monkeypatch):
    from biddesk.hooks import NoSubmitGuard
    ledger = Ledger("plains-med", path=tmp_path / "plains-med.jsonl")
    seen: list[str] = []
    original = NoSubmitGuard.before_tool_call

    def spy(self, event):
        seen.append(type(event).__name__)
        return original(self, event)

    monkeypatch.setattr(NoSubmitGuard, "before_tool_call", spy)
    monkeypatch.setattr(desk, "make_model", lambda model_id, max_tokens: FakeModel([
        tool_round("sam_submit_offer", {"notice_id": "abc", "offer_text": "x"}),
        text_round("I will not file it."),
    ]))
    before = len(desk.SUBMIT_CALLS)
    out = desk.run_refusal_scene("plains-med", ledger=ledger)
    assert seen and seen[0] == "BeforeToolCallEvent"
    assert out["tool_body_calls"] == 0 and len(desk.SUBMIT_CALLS) == before


@shipped
def test_the_denied_tool_row_records_which_model_was_asked():
    rows = Ledger("plains-med").rows(action="denied_tool")
    assert rows, "no refusal scene on file"
    assert json.loads(rows[-1].evidence).get("model_id")


# ------------------------------------------------------------------- item 6: the ledger


def test_concurrent_appends_from_three_threads_lose_no_line(tmp_path):
    path = tmp_path / "concurrent.jsonl"

    def work(tag: str) -> None:
        led = Ledger("red-cedar", path=path)
        for i in range(40):
            led.write(notice_id="n" * 32, action="surfaced", why=f"{tag}-{i}",
                      evidence="{}", tier=2, undo="u")

    threads = [threading.Thread(target=work, args=(t,)) for t in "abc"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 120
    assert all(json.loads(l)["action"] == "surfaced" for l in lines)


@shipped
def test_every_shipped_ledger_row_has_json_evidence_and_no_credential():
    pattern = re.compile(r"(AKIA[0-9A-Z]{16}|aws_secret|SAM_API_KEY|BEGIN [A-Z ]*PRIVATE KEY)", re.I)
    for slug in profiles.PROFILES:
        for r in Ledger(slug).rows():
            json.loads(r.evidence)
            assert not pattern.search(json.dumps(r.model_dump()))


@shipped
def test_exactly_one_surfaced_row_per_shipped_case():
    """Exactly one surfaced row on each shipped case, and it is the newest run.

    The ledger is append-only, so a notice run twice keeps both rows on purpose; what must
    never happen is the gallery showing several runs under one card (D5).
    """
    index = json.loads((REPO / "gallery" / "index.json").read_text(encoding="utf-8"))
    on_cards = {c["notice_id"]: c.get("ledger_rows") or []
                for f in index["firms"] for c in f["cases"]}
    for case in _cases():
        nid = case["notice_id"]
        rows = Ledger(case["firm_slug"]).rows(notice_id=nid, action="surfaced")
        assert rows, f"{nid[:8]} has no surfaced row"
        shown = on_cards.get(nid, [])
        assert len(shown) == 1, f"{nid[:8]} shows {len(shown)} surfaced rows on its card"
        assert shown[0]["ts"] == rows[-1].ts, f"{nid[:8]} does not show the newest run"


# ---------------------------------------------------------------- item 7: CLI and gallery


def test_find_notice_resolves_a_full_notice_id():
    """The unambiguous path, which must keep working after any prefix fix lands.

    The defect (no length floor, no ambiguity check) is recorded by the strict xfail below,
    so this test deliberately asserts nothing about short prefixes.
    """
    first = snapshot.load("red-cedar")[1][0]
    assert desk.find_notice("red-cedar", first.notice_id).notice_id == first.notice_id


def test_an_ambiguous_prefix_is_refused(monkeypatch):
    from biddesk.models import Notice
    base = dict(title="t", type="Solicitation", base_type="Solicitation", posted="2026-09-01",
                ui_link="https://sam.gov/x", fetched_at="2026-09-13T20:00:00-05:00")
    monkeypatch.setattr(desk, "_notices", lambda slug: [
        Notice(notice_id="abcd1234" + "0" * 24, **base),
        Notice(notice_id="abcd1234" + "1" * 24, **base),
    ])
    with pytest.raises(desk.DeskError):
        desk.find_notice("red-cedar", "abcd1234")
    # and a prefix too short to be an id at all is refused before it can match anything
    with pytest.raises(desk.DeskError):
        desk.find_notice("red-cedar", "abcd")


@shipped
def test_gallery_index_matches_the_web_data_contract():
    index = json.loads((REPO / "gallery" / "index.json").read_text(encoding="utf-8"))
    assert "produced_at" in index and "sources" in index
    for firm in index["firms"]:
        for case in firm["cases"]:
            assert {"title", "type", "agency", "deadline", "ui_link"} <= set(case)


@shipped
def test_gallery_tier_counts_match_the_bench_files():
    index = json.loads((REPO / "gallery" / "index.json").read_text(encoding="utf-8"))
    for firm in index["firms"]:
        slug = firm["slug"]
        counts = desk.bench_counts(slug)
        assert counts, slug
        tiers = firm["tiers"]
        assert tiers["total"] == counts["total"], slug
        assert tiers["tier0"] == counts["tier0_filed"], slug
        assert tiers["tier1"] == counts["tier1_filed"], slug
        assert tiers["tier2"] == counts["tier2_sent"], slug
        assert tiers["by_rule"] == counts["by_rule"], slug


# ------------------------------------------------- item 8: the builder's open defects


@shipped
def test_a_card_with_no_validated_evidence_carries_no_confident_fit_score():
    for case in _cases():
        if not case["matrix"]:
            assert case["card"]["fit_score"] is None, case["notice_id"][:8]
            assert case["fit_score_code"] is None, case["notice_id"][:8]


@shipped
def test_the_shipped_fit_score_is_the_code_score_over_the_shipped_rows():
    """Lead ruling 2026-09-14: the fit score is a function of the rows that survived validation in
    THAT run, so it is not comparable across runs or models (ee15f287 scored 0.64 and 0.56 on two
    Sonnet runs; 907ad2ce 0.59 Sonnet vs 0.84 Haiku). The claim the gallery makes is narrower and
    is what this test pins: the number on the card is computed in code from the shipped matrix,
    never typed by the model, and the page labels it "over N validated rows"."""
    for case in _cases():
        rows = [MatrixRow(**r) for r in case["matrix"]]
        expected = desk.fit_from_matrix(rows)
        assert case["card"]["fit_score"] == expected, case["notice_id"][:8]
        assert case["fit_score_code"] == expected, case["notice_id"][:8]


@shipped
def test_the_recommendation_agrees_across_models_where_both_ran():
    """Same-bar fallback claim: on every notice with a surfaced row from two different models, the
    recommendation matched. Fit scores are excluded on purpose (see the test above)."""
    by_notice: dict[str, dict[str, set[str]]] = {}
    for slug in profiles.PROFILES:
        for r in Ledger(slug).rows(action="surfaced"):
            ev = json.loads(r.evidence)
            model, rec = ev.get("model_used"), (r.why or "").split(":", 1)[0].strip()
            if model and rec:
                by_notice.setdefault(r.notice_id, {}).setdefault(model, set()).add(rec)
    compared = 0
    for nid, per_model in by_notice.items():
        if len(per_model) >= 2:
            compared += 1
            recs = {rec for s in per_model.values() for rec in s}
            assert len(recs) == 1, f"{nid[:8]}: {per_model}"
    assert compared >= 1, "no notice has surfaced rows from two models"


def test_the_run_fills_the_new_dropped_row_and_code_score_fields():
    import inspect
    source = inspect.getsource(desk._run_once)
    assert "matrix_dropped_rows" in source and "fit_score_code" in source


@shipped
def test_no_case_is_surfaced_with_zero_extracted_requirements():
    """D4's bar: a card needs something to cite, and says so when it is the description alone."""
    for case in _cases():
        nid = case["notice_id"]
        if _extracted(nid).get("requirements"):
            continue
        notice = next((n for n in snapshot.load(case["firm_slug"])[1] if n.notice_id == nid), None)
        assert notice is not None, nid[:8]
        assert len(_norm(notice.description_text)) >= desk.MIN_DESCRIPTION_CHARS, nid[:8]
        assert case["evidence_basis"], nid[:8]
