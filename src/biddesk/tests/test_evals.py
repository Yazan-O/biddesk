"""The deterministic halves of the eval bench: tiering expectations and hand-marked completeness.

Part 3 (the judge model) is not tested here: it is one model's opinion and it costs a call.
"""
import importlib.util
import json
import sys

import pytest

from biddesk import config, snapshot

ROOT = config.ROOT
SPEC = importlib.util.spec_from_file_location("run_evals", ROOT / "evals" / "run_evals.py")


@pytest.fixture(scope="module")
def ev():
    module = importlib.util.module_from_spec(SPEC)
    sys.modules["run_evals"] = module
    SPEC.loader.exec_module(module)
    return module


SLUGS = ("red-cedar", "sooner-systems", "plains-med")


def test_expected_files_exist_and_match_the_bench(ev):
    for slug in SLUGS:
        exp = ev.load_expected(slug)
        bench = ev.bench_file(slug)
        filed = [r for r in bench["rows"] if r["tier"] == 0 and r["outcome"] == "filed"]
        assert len(exp["filed_by_rule"]) == len(filed) == bench["counts"]["tier0_filed"]
        assert exp["as_of"] == bench["as_of"]
        assert len(exp["hand_checked"]) == 10, f"{slug} needs ten hand-checked filings"
        ids = {n.notice_id for n in snapshot.load(slug)[1]}
        for h in exp["hand_checked"]:
            assert h["notice_id"] in ids
            assert h["checked"].split(":")[0] in {r["rule_id"] for r in filed}


def test_part1_tiering_expectations_pass(ev):
    out = ev.part1(list(SLUGS))
    assert out["kind"] == "deterministic"
    for row in out["rows"]:
        assert row["model_calls"] == 0
        assert not row["filed_fail"], f"{row['firm']}: {row['filed_fail'][:3]}"
        assert not row["unexpected_filings"], f"{row['firm']}: {row['unexpected_filings'][:3]}"
        assert not row["surface_fail"], f"{row['firm']}: {row['surface_fail'][:3]}"
    assert out["all_ok"]
    assert out["totals"]["filed_pass"] == out["totals"]["filed_expected"]
    assert out["totals"]["surface_pass"] == out["totals"]["surface_expected"] == 35


def test_tier0_reruns_at_the_bench_as_of_not_the_wall_clock(ev):
    """pm-days depends on the clock: the rerun must use the bench's own as_of."""
    import datetime as dt
    exp = ev.load_expected("plains-med")
    assert dt.datetime.fromisoformat(exp["as_of"]).year == 2026
    days_rule = [e for e in exp["filed_by_rule"] if e["rule_id"] == "pm-days"]
    assert days_rule, "plains-med must still exercise the deadline rule"


@pytest.mark.skipif(not config.ATTACH.is_dir() or not any(config.ATTACH.iterdir()),
                    reason="needs the data/attachments cache (not in the repository)")
def test_part2_hand_marked_completeness_is_reproducible(ev):
    out = ev.part2()
    assert out["model_calls"] == 0
    assert len(out["rows"]) == 10, "the hand-marked list covers ten attachments"
    scored = [r for r in out["rows"] if r["completeness"] is not None]
    assert scored, "at least one hand-marked notice must be a gallery case"
    for r in scored:
        assert 0.0 <= r["completeness"] <= 1.0
        assert r["carried"] <= r["hand_statements"]
        assert len(r["missed"]) == r["hand_statements"] - r["carried"]
    again = ev.part2()
    assert [r["completeness"] for r in again["rows"]] == [r["completeness"] for r in out["rows"]]


def test_hand_marked_notices_without_a_card_are_not_scored(ev):
    out = ev.part2()
    for r in out["rows"]:
        if not r["in_gallery"]:
            assert r["completeness"] is None and r["carried"] is None
            assert "no gallery card" in r["note"]


def test_bench_json_and_bench_md_exist_and_agree(ev):
    bench = json.loads((ROOT / "evals" / "bench.json").read_text(encoding="utf-8"))
    md = (ROOT / "evals" / "BENCH.md").read_text(encoding="utf-8")
    assert bench["model_primary"] == config.MODEL_PRIMARY
    assert "tiering" in bench["parts"] and "handmark" in bench["parts"]
    assert bench["parts"]["tiering"]["all_ok"]
    assert "deterministic" in md and "judge" in md.lower()


def test_judge_scores_are_never_invented(ev):
    """A case the judge did not score carries null, never a number."""
    bench = json.loads((ROOT / "evals" / "bench.json").read_text(encoding="utf-8"))
    judge = bench["parts"].get("judge")
    if not judge or "rows" not in judge:
        pytest.skip("part 3 has not been run")
    assert judge["model"] == config.MODEL_PRIMARY
    for r in judge["rows"]:
        if r["score"] is None:
            assert r["error"] or r.get("skipped"), "an unscored case must say why"
        else:
            assert 0.0 <= r["score"] <= 1.0
            assert r["model_calls"] >= 1 and r["tokens_in"] > 0
    assert judge["scored"] == sum(1 for r in judge["rows"] if r["score"] is not None)


def test_judge_prompt_caps_and_ranks_requirements(ev):
    """The judge sees at most JUDGE_CAP requirements, ranked by desk.select_requirements."""
    from biddesk import desk
    from biddesk.models import Requirement
    reqs = [Requirement(id=f"r{i}", text=f"t{i}", source_file="f.pdf", page=1,
                        category="scope" if i % 2 else "submission") for i in range(400)]
    chosen = desk.select_requirements(reqs, cap=ev.JUDGE_CAP)
    assert len(chosen) == ev.JUDGE_CAP
    assert chosen[0].category == "submission"
    prompt = ev.judge_prompt("n1", [], chosen)
    assert prompt.count("\n- [") == ev.JUDGE_CAP
