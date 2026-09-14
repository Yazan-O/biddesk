"""The background week replays committed data only, and surfaces only cards above the bar."""
import json

import pytest

from biddesk import config, week
from biddesk.profiles import PROFILES

SLUGS = list(PROFILES)


def bench_counts(slug: str) -> dict:
    return json.loads((config.DATA / "bench" / f"tiering_{slug}.json").read_text(encoding="utf-8"))["counts"]


@pytest.fixture(scope="module", params=SLUGS)
def wk(request):
    return week.replay(request.param)


def test_done_check_all_invariants_pass(wk):
    failed = [c["name"] for c in wk["invariants"]["checks"] if not c["ok"]]
    assert not failed, f"{wk['firm']}: {failed}"
    assert wk["invariants"]["all_ok"]


def test_replay_makes_no_model_call(wk):
    assert wk["model_calls"] == 0


def test_per_day_counts_sum_to_the_bench(wk):
    counts = bench_counts(wk["firm"])
    t = wk["totals"]
    assert t["posted"] == counts["total"]
    assert t["filed_by_rule"] == counts["tier0_filed"]
    assert t["filed_by_model"] == counts["tier1_filed"]
    assert t["sent_to_desk"] == counts["tier2_sent"]
    assert t["by_rule"] == counts["by_rule"]
    assert sum(d["posted"] for d in wk["days"]) == t["posted"]


def test_only_gallery_cases_surface(wk):
    cards = set(week.gallery_cards(wk["firm"]))
    surfaced = [nid for d in wk["days"] for nid in d["surfaced"]]
    assert sorted(surfaced) == sorted(cards)
    assert len(set(surfaced)) == len(surfaced)


def test_no_tier0_or_tier1_notice_surfaces(wk):
    rows = week.bench(wk["firm"])["rows"]
    tier = {r["notice_id"]: r["tier"] for r in rows}
    for d in wk["days"]:
        for nid in d["surfaced"]:
            assert tier[nid] == 2, f"{nid} surfaced from tier {tier[nid]}"


def test_desk_notices_without_a_card_say_why(wk):
    for d in wk["days"]:
        for row in d["no_card"]:
            assert row["why"] == "sent to the desk, no card: no evidence"
    assert wk["totals"]["sent_to_desk"] == wk["totals"]["surfaced"] + wk["totals"]["no_card"]


def test_days_are_in_posted_order_and_inside_the_window(wk):
    dates = [d["date"] for d in wk["days"]]
    assert dates == sorted(dates)
    assert wk["window"]["first_posted"] == dates[0]
    assert wk["window"]["last_posted"] == dates[-1]


def test_amendment_rows_claim_no_unproven_deadline_move(wk):
    for a in wk["amendments"]["rows"]:
        if a["previous_deadline"] is None:
            assert a["deadline_moved"] is None
            assert a["note"] in ("previous version not cached",
                                 "no parent.opportunityId in the cached public JSON",
                                 "public JSON for this notice is not cached",
                                 "previous version cached but carries no response deadline")


def test_web_week_block_flags_no_unproven_amendment(wk):
    boxes = week.week_block(wk)
    assert len(boxes) == len(wk["days"])
    proven = wk["amendments"]["deadline_moved"]
    assert sum(1 for b in boxes if b.get("amendment")) <= proven


def test_gallery_index_carries_the_same_week_strip(wk):
    index = json.loads((config.GALLERY / "index.json").read_text(encoding="utf-8"))
    firm = next(f for f in index["firms"] if f["slug"] == wk["firm"])
    assert [b["date"] for b in firm["week"]] == [d["date"] for d in wk["days"]]
    assert [b["surfaced"] for b in firm["week"]] == [len(d["surfaced"]) for d in wk["days"]]
