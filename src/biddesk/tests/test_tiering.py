"""Tier 0 must file on owner rules with zero model calls, and never file on unknown facts."""
import datetime as dt
from zoneinfo import ZoneInfo

from biddesk import tiering
from biddesk.models import Notice
from biddesk.profiles import PLAINS_MED, RED_CEDAR, SOONER_SYSTEMS

NOW = dt.datetime(2026, 9, 13, 20, 0, tzinfo=ZoneInfo("America/Chicago"))


def make(**kw) -> Notice:
    base = dict(notice_id="n1", title="Janitorial services", solicitation_number="X", type="Solicitation",
                base_type="Solicitation", naics="561720", naics_all=["561720"], set_aside_code="SBA",
                set_aside_desc="Total Small Business Set-Aside", posted="2026-09-10", response_deadline="2026-09-30T17:00:00-05:00",
                archive_date=None, agency_path="GSA", office_city="OKC", office_state="OK", pop_city="Oklahoma City",
                pop_state="OK", pop_zip="73102", pop_country="USA", description_url=None, resource_links=[],
                ui_link="", active="Yes", award=None, description_text="", fetched_at="2026-09-13T19:00:00-05:00")
    base.update(kw)
    return Notice(**base)


def test_award_filed_by_type():
    t = tiering.tier0(RED_CEDAR, make(type="Award Notice"), NOW)
    assert t and t.rule_id == "rc-types" and t.model_calls == 0


def test_unrestricted_none_code_passes():
    assert tiering.tier0(RED_CEDAR, make(set_aside_code="NONE"), NOW) is None
    assert tiering.tier0(RED_CEDAR, make(set_aside_code=""), NOW) is None


def test_ineligible_setaside_filed():
    t = tiering.tier0(RED_CEDAR, make(set_aside_code="SDVOSBC"), NOW)
    assert t and t.rule_id == "rc-setaside"


def test_distance_filed_at_zip_precision():
    t = tiering.tier0(RED_CEDAR, make(pop_city="Wilmington", pop_state="DE", pop_zip="19801"), NOW)
    assert t and t.rule_id == "rc-distance" and "Wilmington, DE" in t.why and "precision=zip" in t.evidence


def test_distance_state_precision_needs_double_margin():
    # Kansas centroid is ~211 mi from OKC: not beyond doubt, so not filed on state precision alone.
    assert tiering.tier0(RED_CEDAR, make(pop_city="", pop_state="KS", pop_zip=""), NOW) is None
    # California centroid is >400 mi: filed even at state precision.
    t = tiering.tier0(RED_CEDAR, make(pop_city="", pop_state="CA", pop_zip=""), NOW)
    assert t and t.rule_id == "rc-distance"


def test_unknown_place_never_files():
    assert tiering.tier0(RED_CEDAR, make(pop_city="", pop_state="", pop_zip="", pop_country=""), NOW) is None


def test_base_period_rule():
    t = tiering.tier0(RED_CEDAR, make(description_text="The period of performance is 6 months from award."), NOW)
    assert t and t.rule_id == "rc-base" and "6 months" in t.why and t.evidence
    assert tiering.tier0(RED_CEDAR, make(description_text="One base year plus four option years."), NOW) is None
    assert tiering.tier0(RED_CEDAR, make(description_text="No period stated."), NOW) is None


def test_days_to_close_rule():
    n = make(type="Solicitation", naics="561320", set_aside_code="8A", response_deadline="2026-09-18T16:00:00-05:00")
    t = tiering.tier0(PLAINS_MED, n, NOW)
    assert t and t.rule_id == "pm-days" and "4.8 days" in t.why
    assert tiering.tier0(PLAINS_MED, make(set_aside_code="8A", response_deadline=None), NOW) is None


def test_past_performance_rule():
    n = make(naics="541512", set_aside_code="SDVOSBC", title="IT help desk support",
             description_text="Tier 1 and tier 2 service desk with ticketing. Network cybersecurity RMF ATO support.")
    assert tiering.tier0(SOONER_SYSTEMS, n, NOW) is None
    short = make(naics="541512", set_aside_code="SDVOSBC", title="Laptops", description_text="Buy 40 laptops.")
    assert tiering.tier0(SOONER_SYSTEMS, short, NOW) is None          # too short to judge: the reader decides
    long = make(naics="541512", set_aside_code="SDVOSBC", title="Laptops", description_text="Buy 40 laptops. " * 120)
    t = tiering.tier0(SOONER_SYSTEMS, long, NOW)
    assert t and t.rule_id == "ss-pp" and "no overlap" in t.why


def test_triage_all_counts_without_model():
    ns = [make(type="Award Notice"), make(set_aside_code="HZC"), make()]
    tr, c = tiering.triage_all(RED_CEDAR, ns, use_model=False, now=NOW)
    assert c.as_dict() == {"total": 3, "tier0_filed": 2, "tier1_filed": 0, "tier2_sent": 1,
                           "by_rule": {"rc-types": 1, "rc-setaside": 1}, "model_calls": 0}


def test_tier1_stub_is_counted():
    stub = lambda p, n: tiering.Triage(n.notice_id, "filed", 1, "scope", "out of scope", "stub", 1, 300, 20)
    tr, c = tiering.triage_all(RED_CEDAR, [make()], use_model=True, now=NOW, tier1_fn=stub)
    assert c.tier1_filed == 1 and c.model_calls == 1


def test_foreign_place_filed_by_distance_rule():
    t = tiering.tier0(RED_CEDAR, make(pop_city="Libreville", pop_state="GA-1", pop_zip="", pop_country="GAB"), NOW)
    assert t and t.rule_id == "rc-distance" and "outside the United States" in t.why


def test_tier1_skips_model_when_no_description():
    t = tiering.tier1(RED_CEDAR, make(description_text=""))
    assert t.outcome == "pass" and t.model_calls == 0


# ---------------------------------------------------------------------------
# Stress-test regressions added by the tiering/geo review (2026-09-13).
# Each xfail carries the defect id used in the review report.
# ---------------------------------------------------------------------------
import json
import re
from pathlib import Path

import pytest

SNAPSHOT_DIR = Path(__file__).resolve().parents[3] / "data" / "snapshot"
SLUGS = ("red-cedar", "sooner-systems", "plains-med")


def _snapshot(slug: str) -> list[Notice]:
    """Load a committed snapshot without importing biddesk.snapshot (which pulls in requests)."""
    raw = json.loads((SNAPSHOT_DIR / f"{slug}.json").read_text(encoding="utf-8"))
    return [Notice(**n) for n in raw["notices"]]


def test_def01_missing_notice_type_must_not_file():
    # An absent type is an unknown fact, and the owner-rule contract is that unknown facts
    # never fire a rule. Today it files with the reason " is not a solicitation".
    assert tiering.tier0(RED_CEDAR, make(type=""), NOW) is None


def test_def01_blank_type_reason_is_never_empty():
    """Guard on the owner-facing string: a kill never reads ' is not a solicitation'."""
    t = tiering.tier0(RED_CEDAR, make(type=""), NOW)
    if t is not None:
        assert t.why.strip() and not t.why.strip().startswith("is not"), t.why


def test_def03_keyword_substring_false_positive():
    # Real corpus: sooner-systems b922088d60ee4996be8262bbb1d8f9ff matches 'ATO' inside
    # "operator of Brookhaven National Laboratory". No IT keyword is really present.
    n = make(naics="541512", set_aside_code="SDVOSBC", title="Workday HCM subscription renewal",
             description_text="Brookhaven Science Associates, LLC (BSA), operator of Brookhaven "
                              "National Laboratory, seeks a subscription renewal. " * 20)
    count, _ = tiering.past_performance_matches(SOONER_SYSTEMS, n, min_hits=1)
    assert count == 0


def test_def04_amendment_boilerplate_must_not_file_pp():
    # Real corpus: sooner-systems b20a69e553fb4a728085fa93f9608764 ("Dorm Wi-Fi Heat Mapping
    # Survey") is 2892 chars of amendment boilerplate with no statement of work, and is filed
    # for "no overlap with any past-performance record".
    boiler = ("The purpose of this amendment is to; 1. Upload the Questions and Answers. "
              "All other terms and conditions remain unchanged. " + "-" * 200 + " ") * 6
    n = make(naics="541512", set_aside_code="SDVOSBC", title="Dorm Wi-Fi Heat Mapping Survey",
             description_text=boiler)
    assert len(boiler) >= tiering.MIN_TEXT_FOR_PP_RULE
    assert tiering.tier0(SOONER_SYSTEMS, n, NOW) is None


def test_def05_shortened_base_year_is_not_twelve_months():
    # Real corpus: red-cedar acaccd4ddab1488fbc52b51511cc447f says "there is a shortened base year"
    # and performance begins 1/1/27; the regex returns 12 from the bare words "base year".
    months, _ = tiering.base_period_months(
        "Period of performance begins 1/1/27. Please note that there is a shortened base year.")
    assert months != 12


def test_def06_days_and_weeks_base_periods_parse():
    # Real corpus: red-cedar 0177c8f802b34de6ab1214d1d2a917e0 ("no later than 120 days after
    # receipt of order") and sooner-systems 00f4df4e491e44328147405a9df1e276 ("25-week period
    # of performance") are both well under a 12-month base and neither is read.
    a, _ = tiering.base_period_months("Period of Performance: To be completed no later than 120 days "
                                      "after receipt of order (ARO) to Wright Patterson AFB, Ohio.")
    b, _ = tiering.base_period_months("A firm-fixed-price contract with a 25-week period of performance "
                                      "from September 30, 2026 through March 23, 2027.")
    assert a is not None and a < 12
    assert b is not None and b < 12


def test_def07_parenthesised_numeral_base_period():
    # Real corpus: sooner-systems e289267da17a4ce9bde458b7bb8a8c56 "a one (1) year base period
    # to include four (4) option years" reads as no base period at all.
    months, _ = tiering.base_period_months(
        "Intends to award a Firm-Fixed-Price Contract for a one (1) year base period "
        "to include four (4) option years.")
    assert months == 12


def test_pp_rule_never_files_on_a_missing_description():
    """Guards the fix that landed 2026-09-13: an unfetched description is an unknown fact."""
    for text in (None, "", "   "):
        n = make(naics="541512", set_aside_code="SDVOSBC",
                 title="Cisco SmartNet renewal", description_text=text)
        assert tiering.tier0(SOONER_SYSTEMS, n, NOW) is None, text


def test_every_real_deadline_shape_parses():
    """Every deadline in the committed snapshots parses, and so do the other shapes SAM emits."""
    for slug in SLUGS:
        for n in _snapshot(slug):
            if n.response_deadline:
                assert tiering.days_to_close(n, NOW) is not None, (slug, n.notice_id, n.response_deadline)
    for shape in ("2026-09-30T17:00:00-05:00", "2026-09-30T17:00-05:00", "2026-09-30T17:00:00Z",
                  "2026-09-30 17:00:00-05:00", "2026-09-30"):
        assert tiering.days_to_close(make(response_deadline=shape), NOW) is not None, shape
    assert tiering.days_to_close(make(response_deadline=None), NOW) is None


def test_set_aside_matrix_over_every_real_code():
    """Every distinct typeOfSetAside in the three snapshots, against each firm's eligibility rule."""
    expected = {  # code -> (red-cedar, sooner-systems, plains-med); True = survives the set-aside rule
        "8A": (False, False, True), "8AN": (False, False, False), "BICiv": (False, False, False),
        "EDWOSB": (True, False, False), "HZC": (False, False, False), "IEE": (False, False, False),
        "ISBEE": (False, False, False), "NONE": (True, True, True), "SBA": (True, True, True),
        # SBP (partial small business) and VSA (veteran-owned) arrived with the 30-day window;
        # no firm's profile claims either, so both are filed for all three.
        "SBP": (False, False, False), "VSA": (False, False, False),
        "SDVOSBC": (False, True, False), "SDVOSBS": (False, False, False), "WOSB": (True, False, False),
        "WOSBSS": (False, False, False), "": (True, True, True), None: (True, True, True),
    }
    seen = {n.set_aside_code for slug in SLUGS for n in _snapshot(slug)}
    assert seen <= set(expected), seen - set(expected)
    for code, (rc, ss, pm) in expected.items():
        for profile, want in ((RED_CEDAR, rc), (SOONER_SYSTEMS, ss), (PLAINS_MED, pm)):
            rule = next(r for r in profile.rules if r.kind == "set_aside_eligible")
            t = tiering.apply_rule(rule, profile, make(set_aside_code=code), NOW)
            assert (t is None) is want, (profile.slug, code, t)


def test_no_tier0_rule_fires_on_an_empty_notice():
    """The unknown-fact contract end to end: a notice with every optional field blank survives tier 0."""
    blank = make(type="Solicitation", set_aside_code=None, set_aside_desc=None, response_deadline=None,
                 pop_city=None, pop_state=None, pop_zip=None, pop_country=None, description_text=None)
    for profile in (RED_CEDAR, SOONER_SYSTEMS, PLAINS_MED):
        assert tiering.tier0(profile, blank, NOW) is None, profile.slug


def test_closed_notice_is_filed_with_a_readable_reason():
    """Real corpus: plains-med 1308054b2df24235825b1e782266e190 closed on 2026-09-09 (-4.4 days)."""
    n = make(naics="561320", set_aside_code="8A", response_deadline="2026-09-09T11:12:13-05:00")
    t = tiering.tier0(PLAINS_MED, n, NOW)
    assert t and t.rule_id == "pm-days"
    assert not re.search(r"only -\d", t.why), f"owner-facing string reads {t.why!r} (DEF-08)"
