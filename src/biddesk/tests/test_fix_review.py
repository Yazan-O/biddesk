"""Fix-review probes (2026-09-13).

Each test is a probe that fails if the corresponding fix is wrong or incomplete.
Tests marked ``xfail(strict=True)`` document a NEW defect found during the review;
when the defect is fixed the test starts passing and the strict marker fails the
suite, which is the signal to drop the marker.
"""
from __future__ import annotations

import datetime as dt
import json
import multiprocessing as mp
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from biddesk import geo
from biddesk.hooks import ProvenanceStamp, deny_reason, scrub
from biddesk.ledger import Ledger
from biddesk.models import LedgerRow, Notice, OwnerRule
from biddesk.profiles import PROFILES
from biddesk.tiering import CENTRAL, apply_rule, base_period_months, keyword_hits


# ------------------------------------------------------------------ helpers

def _notice(**kw) -> Notice:
    base = dict(notice_id="N1", title="t", type="Solicitation", posted="2026-09-01",
                ui_link="https://sam.gov/opp/N1/view", fetched_at="2026-09-01T00:00:00-05:00")
    base.update(kw)
    return Notice(**base)


def _rule(kind: str, value=None, rid="r1") -> OwnerRule:
    return OwnerRule(id=rid, text="owner rule", kind=kind, value=value)


@pytest.fixture()
def ledger(tmp_path) -> Ledger:
    return Ledger("testfirm", path=tmp_path / "t.jsonl")


def _row(action="filed", notice_id="N1", **kw) -> LedgerRow:
    base = dict(ts="", firm_slug="", notice_id=notice_id, action=action,
                why="because", evidence="{}", tier=1)
    base.update(kw)
    return LedgerRow(**base)


# ================================================================ tiering.py

def test_def01_blank_notice_type_is_not_filed():
    """DEF-01: a notice with a blank type must not be filed by the notice_types rule."""
    rule = _rule("notice_types", ["Solicitation", "Sources Sought"])
    prof = PROFILES["red-cedar"]
    assert apply_rule(rule, prof, _notice(type="")) is None
    assert apply_rule(rule, prof, _notice(type="Award Notice")) is not None


def test_def03_keyword_hits_are_whole_word():
    assert keyword_hits(["ATO"], "the operator shall") == 0
    assert keyword_hits(["RN"], "shall furnish") == 0
    assert keyword_hits(["SOC"], "smith associates llc") == 0
    assert keyword_hits(["ATO"], "an ato package is required") == 1
    assert keyword_hits(["nurse"], "registered nurses on site") == 1


def test_def04_pp_rule_needs_long_prose_and_skips_amendment_boilerplate():
    prof = PROFILES["sooner-systems"]
    rule = _rule("min_past_performance_matches", 2, rid="ss-pp")
    short = _notice(title="Widget", description_text="Buy widgets.")
    assert apply_rule(rule, prof, short) is None
    long_off_topic = _notice(title="Bridge painting",
                             description_text="The contractor shall paint the bridge. " * 80)
    assert apply_rule(rule, prof, long_off_topic) is not None
    amended = _notice(title="Bridge painting",
                      description_text="The purpose of this amendment is to extend the date. " * 80)
    assert apply_rule(rule, prof, amended) is None


def test_def05_shortened_base_year_returns_none():
    assert base_period_months("a shortened base year is contemplated")[0] is None
    assert base_period_months("partial base year")[0] is None
    assert base_period_months("prorated base year")[0] is None
    assert base_period_months("a 12 month base period")[0] == 12


def test_def06_day_and_week_base_periods_parse():
    assert base_period_months("period of performance shall be no later than 120 days")[0] == 4
    assert base_period_months("this is a 25-week period of performance")[0] == 6


def test_def07_one_paren_one_year_base():
    assert base_period_months("a one (1) year base period with four options")[0] == 12


def test_def08_negative_days_to_close_reads_closed_on():
    rule = _rule("min_days_to_close", 10, rid="pm-days")
    n = _notice(response_deadline="2026-09-10T17:00:00-05:00")
    hit = apply_rule(rule, PROFILES["plains-med"], n,
                     now=dt.datetime(2026, 9, 13, 9, 0, tzinfo=CENTRAL))
    assert hit is not None
    assert hit.why == "closed on 2026-09-10", hit.why


def test_set_aside_codes_are_case_and_space_normalized():
    rule = _rule("set_aside_eligible", ["WOSB", "SBA", "", "NONE"], rid="rc-setaside")
    prof = PROFILES["red-cedar"]
    for code in (" wosb ", "WOSB", "wosb\n", "sba"):
        assert apply_rule(rule, prof, _notice(set_aside_code=code)) is None, code
    assert apply_rule(rule, prof, _notice(set_aside_code=" 8a ")) is not None


# ---------------------------------------------------------------- NEW: tiering

def test_new01_day_pattern_must_not_match_a_start_clause():
    months, _ = base_period_months("The period of performance shall begin within 30 days after award.")
    assert months is None


def test_new02_update_log_description_must_not_clear_the_prose_gate():
    body = "\n".join(
        "UPDATE 0{}/1{}/2026: The solicitation closing date is revised to October {}, 2026 "
        "at 2:00 PM Eastern Time.".format(i % 9 + 1, i % 9, i % 28 + 1) for i in range(40))
    text = ("This notice is the official release of solicitation 19AQMM24R0113. "
            "Please see the attached documents.\n" + body)
    n = _notice(title="NextGen Passport Personalization Printers Support", description_text=text)
    rule = _rule("min_past_performance_matches", 2, rid="ss-pp")
    assert apply_rule(rule, PROFILES["sooner-systems"], n) is None


def test_new03_amendment_n_is_issued_is_boilerplate():
    text = ("AMENDMENT 1\nAmendment 1 is issued to revise the base Period of Performance. "
            "All other terms and conditions remain unchanged. " * 30)
    n = _notice(title="WORKDAY HCM", description_text=text)
    rule = _rule("min_past_performance_matches", 2, rid="ss-pp")
    assert apply_rule(rule, PROFILES["sooner-systems"], n) is None


# ==================================================================== geo.py

def test_def02_state_precision_with_blank_country_is_unknown():
    prof = PROFILES["red-cedar"]
    n = _notice(pop_state="OK", pop_city=None, pop_zip=None, pop_country="")
    assert geo.distance_from(prof.lat, prof.lon, n) == (None, "unknown")
    n2 = _notice(pop_state="OK", pop_country="USA")
    assert geo.distance_from(prof.lat, prof.lon, n2)[1] == "state"


# ================================================================= profiles.py

def test_def09_sole_source_codes_are_not_in_eligibility_rules():
    for slug, rid in (("sooner-systems", "ss-setaside"), ("plains-med", "pm-setaside")):
        rule = next(r for r in PROFILES[slug].rules if r.id == rid)
        assert "SDVOSBS" not in rule.value
        assert "8AN" not in rule.value


def test_def09_sooner_keywords_were_widened():
    kws = {k.lower() for pp in PROFILES["sooner-systems"].past_performance for k in pp.keywords}
    for k in ("security", "cyber", "soc", "siem", "soar", "incident response",
              "information technology", "it services", "it support"):
        assert k in kws, k


def test_new04_profile_set_asides_hold_no_sole_source_code():
    for p in PROFILES.values():
        assert "SDVOSBS" not in p.set_asides and "8AN" not in p.set_asides


# =================================================================== hooks.py

def test_hooks01_non_dict_tool_input_is_tolerated(ledger):
    event = SimpleNamespace(
        tool_use={"name": "read_attachment", "toolUseId": "t1", "input": "a bare string"},
        result={"toolUseId": "t1", "status": "success", "content": [{"text": "x"}]},
        exception=None)
    ProvenanceStamp(ledger=ledger).after_tool_call(event)
    assert json.loads(event.result["content"][-1]["text"])["biddesk_provenance"]["notice_id"] == "-"


def test_hooks02_errored_tool_is_neither_stamped_nor_logged(ledger):
    for result, exc in (({"toolUseId": "t", "status": "error", "content": [{"text": "boom"}]}, None),
                        ({"toolUseId": "t", "status": "success", "content": [{"text": "x"}]},
                         ValueError("boom"))):
        ev = SimpleNamespace(
            tool_use={"name": "read_attachment", "toolUseId": "t", "input": {"notice_id": "N1"}},
            result=result, exception=exc)
        ProvenanceStamp(ledger=ledger).after_tool_call(ev)
    assert ledger.rows() == []


def test_hooks03_deny_matches_versioned_tool_names():
    assert deny_reason("submit_bid_v2", {}) is not None
    assert deny_reason("v2_submit_bid", {}) is not None
    assert deny_reason("sam_search", {}) is None
    assert deny_reason("design_review", {}) is None
    assert deny_reason("cosign_check", {}) is None
    assert deny_reason("payment_status", {}) is None


def test_hooks04_urls_are_percent_decoded_before_the_path_rule():
    assert deny_reason("fetch", {"url": "https://sam.gov/api/%73ubmit/x"}) is not None
    assert deny_reason("fetch", {"url": "https://sam.gov/opp/abc/view"}) is None


def test_hooks06_secrets_in_query_strings_are_redacted():
    for param in ("api_key", "apikey", "token", "access_token", "signature"):
        out = scrub({"url": "https://api.sam.gov/x?" + param + "=FAKEVALUE123&limit=10"})["url"]
        assert "FAKEVALUE123" not in out, param
        assert "[redacted]" in out


def test_hooks06_must_not_redact_notice_ids_or_resource_guids():
    nid = "0123456789abcdef0123456789abcdef"
    link = "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/" + nid + "/download"
    assert scrub({"url": link})["url"] == link
    assert scrub({"notice_id": nid})["notice_id"] == nid
    assert scrub({"url": "https://api.sam.gov/opportunities/v2/search?noticeid=" + nid})["url"].endswith(nid)


# ------------------------------------------------------------------ NEW: hooks

def test_new05_deny_stems_must_not_collide_with_in_domain_tools():
    for name in ("fetch_pay_rates", "pay_rate_lookup", "file_transfer",
                 "transfer_notice_to_gallery", "design_review", "assign_owner"):
        assert deny_reason(name, {}) is None, name
    # the bare `sign` stem stays: a tool named read_sign_in_sheet is denied until it gets an allow entry
    assert deny_reason("read_sign_in_sheet", {}) is not None


def test_new06_camelcase_and_hyphenated_submit_tools_are_denied():
    for name in ("submitBid", "submit-bid", "submitbid", "submit_bid2"):
        assert deny_reason(name, {}) is not None, name


def test_new07_submit_in_a_query_string_is_denied():
    assert deny_reason("fetch", {"url": "https://sam.gov?action=submit"}) is not None


def test_new08_bare_key_query_param_is_redacted():
    out = scrub({"url": "https://api.sam.gov/x?key=FAKEVALUE123&limit=10"})["url"]
    assert "FAKEVALUE123" not in out


def test_new09_keywords_argument_is_not_redacted():
    assert scrub({"keywords": ["janitorial", "custodial"]})["keywords"] == ["janitorial", "custodial"]


# ================================================================== ledger.py

def test_ledger01_append_is_a_single_write(ledger):
    for i in range(50):
        ledger.write(notice_id="N%d" % i, action="filed", why="w", evidence="e", tier=0)
    lines = [l for l in ledger.path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 50
    assert all(json.loads(l)["notice_id"] == "N%d" % i for i, l in enumerate(lines))


def test_ledger02_unreadable_lines_are_skipped_and_counted(ledger):
    ledger.append(_row("filed", "N1"))
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write('{"ts": "2026-01-01T00:00:00-06:00"}\n')
    rows = ledger.rows()
    assert len(rows) == 1 and ledger.skipped == 2


def test_ledger03_undo_refuses_a_second_undo_and_an_undone_row(ledger):
    row = ledger.append(_row("filed", "N1"))
    undo_row = ledger.undo(row.ts, "owner vetoed")
    with pytest.raises(ValueError):
        ledger.undo(row.ts, "again")
    with pytest.raises(ValueError):
        ledger.undo(undo_row.ts, "undo the undo")


def test_ledger04_undo_matches_the_instant_not_the_string(ledger):
    row = ledger.append(_row("filed", "N1", ts="2026-09-13T08:00:00-05:00"))
    ledger.undo("2026-09-13T13:00:00+00:00", "same instant")
    assert [r.action for r in ledger.rows()] == ["filed", "undone"]
    assert row.ts in ledger.undone_ts()


# ------------------------------------------------------------------ NEW: ledger

def test_new10_non_object_json_line_is_skipped_not_fatal(ledger):
    ledger.append(_row("filed", "N1"))
    with ledger.path.open("a", encoding="utf-8") as fh:
        fh.write('"hello"\n')
        fh.write("[1, 2, 3]\n")
    assert len(ledger.rows()) == 1
    assert ledger.skipped == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows newline translation only")
def test_new11_ledger_bytes_are_not_crlf_translated(ledger):
    ledger.write(notice_id="N1", action="filed", why="w", evidence="e", tier=0)
    assert b"\r\n" not in ledger.path.read_bytes()


def _append_many(args):
    path, tag, n = args
    from biddesk.ledger import Ledger as L
    led = L("f", Path(path))
    for i in range(n):
        led.write(notice_id="%s-%d" % (tag, i), action="filed",
                  why="w" * 200, evidence="e" * 200, tier=0)
    return n


@pytest.mark.skipif(os.name != "nt", reason="POSIX O_APPEND is atomic; this defect is Windows-only")
def test_new12_concurrent_appends_lose_no_rows(tmp_path):
    path = tmp_path / "conc.jsonl"
    path.write_bytes(b"")
    with mp.Pool(2) as pool:
        pool.map(_append_many, [(str(path), "A", 500), (str(path), "B", 500)])
    raw = path.read_bytes()
    lines = [l for l in raw.decode("utf-8", "replace").splitlines() if l.strip()]
    failures = 0
    ids = set()
    for line in lines:
        try:
            ids.add(json.loads(line)["notice_id"])
        except Exception:
            failures += 1
    assert failures == 0, "%d unparseable lines" % failures
    assert len(lines) == 1000, "%d rows lost" % (1000 - len(lines))
    assert len(ids) == 1000
