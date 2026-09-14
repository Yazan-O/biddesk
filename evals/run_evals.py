"""The Biddesk eval bench: tiering expectations, matrix completeness, judge scores.

Three parts, two of them deterministic:

1. Tiering expectations (deterministic, no model). ``evals/expected_<slug>.json`` names every
   notice the owner's rules must file, with the rule id, and every notice that should surface
   (the gallery cards). Part 1 re-runs tier 0 in code at the bench's own ``as_of`` and compares.
2. Matrix completeness against a hand-marked list (deterministic). The phase-3 hand-marked
   "shall" list covers ten attachments; for every gallery case whose notice is in that list,
   completeness = hand-marked statements carried by a kept matrix row over hand-marked
   statements.
3. Matrix completeness, judge model (an opinion, not a measurement). One structured-output
   call per gallery case on MODEL_PRIMARY. Calls and tokens are recorded per case. A throttle
   stops the part: the cases that ran keep their score, the rest stay null.

Usage:
    PYTHONIOENCODING=utf-8 python evals/run_evals.py --build-expected
    PYTHONIOENCODING=utf-8 python evals/run_evals.py --parts 1 2
    PYTHONIOENCODING=utf-8 python evals/run_evals.py --parts 1 2 3 [--workers 2] [--limit N]

Writes evals/bench.json (rewritten after every part and after every judged case) and
evals/BENCH.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pydantic import BaseModel, Field                                    # noqa: E402

from biddesk import config, desk, profiles, snapshot, tiering            # noqa: E402
from biddesk.ledger import now_iso                                       # noqa: E402
from biddesk.models import Requirement                                   # noqa: E402

EVALS = ROOT / "evals"
HAND = ROOT / "_runs" / "2026-09-13_phase3_donecheck" / "hand_marked"
BENCH_JSON = EVALS / "bench.json"
BENCH_MD = EVALS / "BENCH.md"
JUDGE_MAX_TOKENS = 5000         # a truncated structured output is a lost case, not a score
JUDGE_CAP = 150                 # highest-ranked requirements handed to the judge
MAX_REQ_CHARS = 600             # reader.MAX_REQ_CHARS; a row may be a prefix of a long hand sentence

#: Ten tier-0 filings per profile read by hand against the notice fields on 2026-09-14, with the
#: field that settles each one. These are recorded in ``expected_<slug>.json`` so a reader can
#: repeat the check without re-deriving it.
HAND_CHECKED: dict[str, dict[str, str]] = {
    "red-cedar": {
        "0177c8f8": "rc-distance: pop Wright Patterson AFB, OH 45433 is 794 mi from Oklahoma City (limit 200)",
        "08127248": "rc-distance: pop Nebo, NC 28761 is 877 mi (limit 200)",
        "08fe4436": "rc-distance: pop zip 98271 (Marysville WA) is 1531 mi (limit 200)",
        "09dce64e": "rc-distance: pop state NE only, 409 mi, past the 2x state-precision limit of 400",
        "05810e4b": "rc-setaside: typeOfSetAside 8A is not in WOSB/EDWOSB/SBA/unrestricted",
        "11b69a0c": "rc-setaside: typeOfSetAside SDVOSBC is not one this firm can claim",
        "1f04e70b": "rc-setaside: typeOfSetAside SDVOSBC is not one this firm can claim",
        "0fe1fe9b": "rc-types: type 'Special Notice' is not a real solicitation",
        "12fb78f6": "rc-types: type 'Justification' is not a real solicitation",
        "15cd0127": "rc-types: type 'Award Notice' is not a real solicitation",
    },
    "sooner-systems": {
        "00553ac5": "ss-types: type 'Award Notice' is not a real solicitation",
        "00f4df4e": "ss-types: type 'Special Notice' is not a real solicitation",
        "011fa308": "ss-types: type 'Special Notice' is not a real solicitation",
        "206cebfc": "ss-setaside: typeOfSetAside BICiv (Buy Indian) is not in SDVOSBC/SBA/unrestricted",
        "6bd7483f": "ss-setaside: typeOfSetAside 8AN (8(a) sole source) is not one this firm can claim",
        "6d83796d": "ss-setaside: typeOfSetAside ISBEE is not one this firm can claim",
        "09e9e201": "ss-pp: 5827 chars of prose, 0 hits across all 32 past-performance keywords",
        "459718be": "ss-pp: 3547 chars ('Social Media Monitoring'), 0 keyword hits",
        "7915ed25": "ss-pp: 4136 chars ('Enterprise Service Management'), 0 keyword hits",
        "ab8ecb8f": "ss-pp: 2758 chars ('Content Authorizing Tools'), 0 keyword hits",
    },
    "plains-med": {
        "1308054b": "pm-types: type 'Award Notice' is not a real solicitation",
        "ad81a8e6": "pm-types: type 'Special Notice' is not a real solicitation",
        "d9cebba5": "pm-types: type 'Award Notice' is not a real solicitation",
        "4652e0fa": "pm-setaside: typeOfSetAside SDVOSBC is not in 8A/SBA/unrestricted",
        "8a487ff9": "pm-setaside: typeOfSetAside EDWOSB is not one this firm can claim",
        "ad33f0dd": "pm-setaside: typeOfSetAside SDVOSBC is not one this firm can claim",
        "dcf0d149": "pm-setaside: typeOfSetAside ISBEE is not one this firm can claim",
        "8b795e67": "pm-days: deadline 2026-09-14T10:00-05:00 is 0.6 days after the bench as_of (minimum 10)",
        "9c85e8fa": "pm-days: deadline 2026-09-10T08:00-04:00 had already passed at the bench as_of",
        "ec37c594": "pm-days: deadline 2026-09-18T14:00-07:00 is 4.8 days after the bench as_of (minimum 10)",
    },
}


# ------------------------------------------------------------------ shared inputs

def bench_file(slug: str) -> dict:
    return json.loads((config.DATA / "bench" / f"tiering_{slug}.json").read_text(encoding="utf-8"))


def gallery_index() -> dict:
    return json.loads((config.GALLERY / "index.json").read_text(encoding="utf-8"))


def gallery_cases() -> list[dict]:
    """Every gallery case as {slug, notice_id, title}, in index order."""
    out = []
    for firm in gallery_index()["firms"]:
        for c in firm["cases"]:
            out.append({"slug": firm["slug"], "notice_id": c["notice_id"], "title": c["title"]})
    return out


def case_file(slug: str, notice_id: str) -> dict:
    return json.loads((config.GALLERY / "cases" / slug / f"{notice_id}.json").read_text(encoding="utf-8"))


def extracted(notice_id: str) -> list[dict]:
    path = config.ATTACH / notice_id / "extracted.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("requirements", [])


# ------------------------------------------------------------------ part 1

def build_expected(slug: str) -> dict:
    """Derive the expectation file from the committed bench and the built gallery."""
    data = bench_file(slug)
    _meta, notices = snapshot.load(slug)
    by_id = {n.notice_id: n for n in notices}
    filed = []
    for r in data["rows"]:
        if r.get("tier") == 0 and r.get("outcome") == "filed":
            n = by_id[r["notice_id"]]
            filed.append({"notice_id": n.notice_id, "rule_id": r["rule_id"], "title": n.title,
                          "type": n.type, "set_aside_code": n.set_aside_code,
                          "pop": ", ".join(x for x in (n.pop_city, n.pop_state, n.pop_zip) if x),
                          "response_deadline": n.response_deadline, "why": r.get("why", "")})
    firm = next((f for f in gallery_index()["firms"] if f["slug"] == slug), {"cases": []})
    surface = [{"notice_id": c["notice_id"], "title": c["title"]} for c in firm["cases"]]
    checked = {}
    for prefix, note in HAND_CHECKED.get(slug, {}).items():
        full = [n.notice_id for n in notices if n.notice_id.startswith(prefix)]
        if len(full) != 1:
            raise RuntimeError(f"{slug}: hand-checked prefix {prefix} matches {len(full)} notices")
        checked[full[0]] = note
    out = {
        "firm": slug,
        "as_of": data["as_of"],
        "built_at": now_iso(),
        "derived_from": {"bench": f"data/bench/tiering_{slug}.json", "gallery": "gallery/index.json"},
        "how_to_read": ("filed_by_rule is the first owner rule that fires (tier 0 is first-kill-wins); "
                        "should_surface is the set of notices that produced a gallery card. Tier 0 is "
                        "re-run at as_of so the deadline rule decides the same way it did at bench time."),
        "hand_checked": [{"notice_id": nid, "checked": note} for nid, note in checked.items()],
        "filed_by_rule": filed,
        "should_surface": surface,
    }
    (EVALS / f"expected_{slug}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    return out


def load_expected(slug: str) -> dict:
    path = EVALS / f"expected_{slug}.json"
    if not path.exists():
        return build_expected(slug)
    return json.loads(path.read_text(encoding="utf-8"))


def part1(slugs: list[str]) -> dict:
    """Re-run tier 0 in code and assert both expected sets. No model call."""
    rows = []
    for slug in slugs:
        exp = load_expected(slug)
        prof = profiles.PROFILES[slug]
        now = dt.datetime.fromisoformat(exp["as_of"])
        _meta, notices = snapshot.load(slug)
        by_id = {n.notice_id: n for n in notices}
        expect_rule = {e["notice_id"]: e["rule_id"] for e in exp["filed_by_rule"]}
        got = {}
        for n in notices:
            t = tiering.tier0(prof, n, now)
            if t:
                got[n.notice_id] = t.rule_id
        filed_pass = [nid for nid, rid in expect_rule.items() if got.get(nid) == rid]
        filed_fail = [{"notice_id": nid, "expected": rid, "got": got.get(nid)}
                      for nid, rid in expect_rule.items() if got.get(nid) != rid]
        extra = [{"notice_id": nid, "expected": None, "got": rid}
                 for nid, rid in got.items() if nid not in expect_rule]
        surf_pass, surf_fail = [], []
        for e in exp["should_surface"]:
            nid = e["notice_id"]
            t = tiering.tier0(prof, by_id[nid], now) if nid in by_id else "missing"
            if t is None:
                surf_pass.append(nid)
            else:
                surf_fail.append({"notice_id": nid,
                                  "filed_by": getattr(t, "rule_id", "notice not in the snapshot")})
        rows.append({
            "firm": slug, "as_of": exp["as_of"],
            "filed_expected": len(expect_rule), "filed_pass": len(filed_pass),
            "filed_fail": filed_fail, "unexpected_filings": extra,
            "surface_expected": len(exp["should_surface"]), "surface_pass": len(surf_pass),
            "surface_fail": surf_fail,
            "hand_checked": [h["notice_id"] for h in exp.get("hand_checked", [])],
            "model_calls": 0,
        })
    ok = all(not r["filed_fail"] and not r["unexpected_filings"] and not r["surface_fail"] for r in rows)
    return {"name": "tiering expectations",
            "kind": "deterministic",
            "command": "python evals/run_evals.py --parts 1",
            "all_ok": ok,
            "totals": {"filed_expected": sum(r["filed_expected"] for r in rows),
                       "filed_pass": sum(r["filed_pass"] for r in rows),
                       "surface_expected": sum(r["surface_expected"] for r in rows),
                       "surface_pass": sum(r["surface_pass"] for r in rows),
                       "model_calls": 0},
            "rows": rows}


# ------------------------------------------------------------------ part 2

FOLD = {"‘": "'", "’": "'", "“": '"', "”": '"',
        "–": "-", "—": "-", "‑": "-", " ": " "}


def norm(s: str) -> str:
    for a, b in FOLD.items():
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def load_hand(path: Path) -> dict:
    """One hand-marked attachment page: header fields plus the numbered verbatim sentences."""
    head: dict = {}
    items: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(notice_id|source_file|page|original_name|span):\s*(.+)$", line)
        if m:
            head.setdefault(m.group(1), m.group(2).strip())
            continue
        m = re.match(r"^(\d+)\.\s+(.*\S)\s*$", line)
        if m:
            items.append(m.group(2))
    head["items"] = items
    head["key"] = path.stem
    return head


def hand_pages() -> list[dict]:
    return [load_hand(p) for p in sorted(HAND.glob("*.md"))]


def part2() -> dict:
    """Hand-marked statements carried by a kept matrix row, per gallery case."""
    cases = {c["notice_id"]: c for c in gallery_cases()}
    pages = hand_pages()
    by_notice: dict[str, list[dict]] = {}
    for h in pages:
        by_notice.setdefault(h["notice_id"], []).append(h)

    rows = []
    for notice_id, hand in sorted(by_notice.items()):
        case = cases.get(notice_id)
        if case is None:
            rows.append({"notice_id": notice_id, "firm": None, "in_gallery": False,
                         "hand_statements": sum(len(h["items"]) for h in hand),
                         "carried": None, "completeness": None,
                         "note": "hand-marked notice has no gallery card; nothing to score"})
            continue
        data = case_file(case["slug"], notice_id)
        matrix = data["matrix"]
        reqs = extracted(notice_id)
        carried, missed = 0, []
        total = 0
        for h in hand:
            page = int(h["page"])
            page_reqs = [r for r in reqs if r["source_file"] == h["source_file"] and r["page"] == page]
            page_rows = [m for m in matrix if m["source_file"] == h["source_file"] and m["page"] == page]
            row_ids = {m["requirement_id"] for m in page_rows}
            for i, sent in enumerate(h["items"], 1):
                total += 1
                s = norm(sent)
                req_ids = {r["id"] for r in page_reqs
                           if s in norm(r["text"]) or (len(s) > MAX_REQ_CHARS and s.startswith(norm(r["text"])))}
                hit = bool(req_ids & row_ids)
                if not hit:
                    hit = any(norm(m["quote"]) and (norm(m["quote"]) in s or s in norm(m["quote"]))
                              for m in page_rows)
                if hit:
                    carried += 1
                else:
                    missed.append({"page_key": h["key"], "n": i, "text": sent[:160]})
        rows.append({"notice_id": notice_id, "firm": case["slug"], "in_gallery": True,
                     "title": case["title"][:80],
                     "hand_statements": total, "carried": carried,
                     "completeness": round(carried / total, 3) if total else None,
                     "matrix_kept": len(matrix), "missed": missed})
    scored = [r for r in rows if r["completeness"] is not None]
    tot_hand = sum(r["hand_statements"] for r in scored)
    tot_carried = sum(r["carried"] for r in scored)
    return {"name": "matrix completeness against the hand-marked list",
            "kind": "deterministic",
            "command": "python evals/run_evals.py --parts 2",
            "hand_source": "_runs/2026-09-13_phase3_donecheck/hand_marked/*.md",
            "cases_scored": len(scored),
            "cases_not_in_gallery": len(rows) - len(scored),
            "hand_statements": tot_hand, "carried": tot_carried,
            "completeness": round(tot_carried / tot_hand, 3) if tot_hand else None,
            "model_calls": 0,
            "rows": rows}


# ------------------------------------------------------------------ part 3 (judge)

class JudgeVerdict(BaseModel):
    """The judge's opinion on one case's requirements matrix."""
    score: float = Field(ge=0, le=1, description="fraction of the bid-critical requirements the matrix carries")
    missed: list[str] = Field(default_factory=list,
                              description="requirement ids the matrix should have carried and does not; at most 25")
    notes: str = Field(description="two sentences: what the matrix covers and what it leaves out")


JUDGE_SYSTEM = (
    "You grade a requirements matrix built from a federal solicitation's attachments. You are given the "
    "requirements a reader extracted from those attachments (id, file, page, verbatim text) and the rows the "
    "matrix kept. Judge coverage only: what fraction of the requirements a bid/no-bid decision turns on does "
    "the matrix carry? Ignore the firm's answers and the status labels. score is 0 to 1. missed holds "
    "requirement ids that appear in the requirement list and should have been carried, at most 25 of them; "
    "never invent an id. Keep notes to two sentences."
)


def judge_prompt(notice_id: str, matrix: list[dict], reqs: list[Requirement]) -> str:
    lines = [f"NOTICE {notice_id}", "",
             f"EXTRACTED REQUIREMENTS ({len(reqs)} shown, ranked by category priority then document order):"]
    for r in reqs:
        lines.append(f"- [{r.id}] ({r.source_file} p{r.page}, {r.category}) {r.text}")
    lines += ["", f"MATRIX ROWS KEPT ({len(matrix)}):"]
    for m in matrix:
        lines.append(f"- [{m['requirement_id']}] ({m['source_file']} p{m['page']}) {m['quote']}")
    lines += ["", "How completely does the matrix cover the requirements a bid/no-bid turns on?"]
    return "\n".join(lines)


def _is_throttle(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return ("Throttl" in text or "throttl" in text or "TooManyRequests" in text
            or "quota" in text.lower() or "ServiceQuotaExceeded" in text)


def judge_one(case: dict) -> dict:
    """One structured-output call on MODEL_PRIMARY. Raises on a throttle so the part can stop."""
    from strands import Agent
    from biddesk.hooks import TierCounter

    data = case_file(case["slug"], case["notice_id"])
    reqs = [Requirement(**r) for r in extracted(case["notice_id"])]
    chosen = desk.select_requirements(reqs, cap=JUDGE_CAP)
    if not chosen:
        # lead ruling 2026-09-14: nothing to cover means no score, not a vacuous 1.0; no model call
        return {
            "notice_id": case["notice_id"], "firm": case["slug"], "title": case["title"][:80],
            "model": config.MODEL_PRIMARY, "requirements_total": 0, "requirements_shown": 0,
            "matrix_kept": len(data["matrix"]), "score": None, "missed": None, "missed_unknown_ids": None,
            "notes": "no extracted requirements; nothing to judge", "model_calls": 0,
            "tokens_in": 0, "tokens_out": 0, "error": None, "skipped": "no requirements",
        }
    counter = TierCounter(persist=True)
    agent = Agent(model=desk.make_model(config.MODEL_PRIMARY, JUDGE_MAX_TOKENS),
                  system_prompt=JUDGE_SYSTEM, structured_output_model=JudgeVerdict,
                  callback_handler=None, hooks=[counter], name="biddesk-judge")
    result = agent(judge_prompt(case["notice_id"], data["matrix"], chosen))
    verdict = getattr(result, "structured_output", None)
    if verdict is None:
        raise RuntimeError("the judge returned no structured output")
    usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None) or {}
    known = {r.id for r in chosen}
    return {
        "notice_id": case["notice_id"], "firm": case["slug"], "title": case["title"][:80],
        "model": config.MODEL_PRIMARY,
        "requirements_total": len(reqs), "requirements_shown": len(chosen),
        "matrix_kept": len(data["matrix"]),
        "score": round(float(verdict.score), 3),
        "missed": verdict.missed,
        "missed_unknown_ids": [m for m in verdict.missed if m not in known],
        "notes": verdict.notes,
        "model_calls": counter.model_calls,
        "tokens_in": int(usage.get("inputTokens", 0) or 0),
        "tokens_out": int(usage.get("outputTokens", 0) or 0),
        "error": None,
    }


def part3(cases: list[dict], workers: int, on_row=None, prior: Optional[dict] = None) -> dict:
    """One judge call per gallery case, at low concurrency, persisted as each lands."""
    from concurrent.futures import ThreadPoolExecutor

    rows: dict[str, dict] = {c["notice_id"]: {
        "notice_id": c["notice_id"], "firm": c["slug"], "title": c["title"][:80],
        "score": None, "missed": None, "notes": None, "model_calls": 0,
        "tokens_in": 0, "tokens_out": 0, "error": "not run"} for c in cases}
    for nid, row in (prior or {}).items():
        rows.setdefault(nid, row)
        if rows[nid].get("score") is None and row.get("score") is not None:
            rows[nid] = row
    stop = threading.Event()
    lock = threading.Lock()
    throttle: list[str] = []

    def one(case: dict):
        if stop.is_set():
            return
        try:
            row = judge_one(case)
        except Exception as exc:                      # noqa: BLE001 - a failure is data here
            text = f"{type(exc).__name__}: {exc}"
            if _is_throttle(exc):
                stop.set()
                with lock:
                    throttle.append(text)
            with lock:
                rows[case["notice_id"]]["error"] = text
            return
        with lock:
            rows[case["notice_id"]] = row
            if on_row:
                on_row()

    with ThreadPoolExecutor(max(1, workers)) as ex:
        list(ex.map(one, cases))

    order = list(dict.fromkeys(list((prior or {}).keys()) + [c["notice_id"] for c in cases]))
    done = [r for r in rows.values() if r["score"] is not None]
    return {"name": "matrix completeness, judge model",
            "kind": "judge opinion (not a measurement)",
            "command": "python evals/run_evals.py --parts 3",
            "model": config.MODEL_PRIMARY,
            "cases": len(order), "scored": len(done), "unscored": len(order) - len(done),
            "mean_score": round(sum(r["score"] for r in done) / len(done), 3) if done else None,
            "model_calls": sum(r["model_calls"] for r in rows.values()),
            "tokens_in": sum(r["tokens_in"] for r in rows.values()),
            "tokens_out": sum(r["tokens_out"] for r in rows.values()),
            "throttled": bool(throttle),
            "throttle_error": throttle[0] if throttle else None,
            "rows": [rows[nid] for nid in order]}


# ------------------------------------------------------------------ output

def save(state: dict) -> None:
    EVALS.mkdir(parents=True, exist_ok=True)
    BENCH_JSON.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def md_table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    return "\n".join(out)


def write_md(state: dict) -> None:
    L: list[str] = []
    L.append("# Biddesk eval bench")
    L.append("")
    L.append(f"Run `{state['as_of']}` (America/Chicago). Every row below traces to a committed file: "
             "`data/bench/tiering_<slug>.json`, `data/snapshot/<slug>.json`, `gallery/index.json`, "
             "`gallery/cases/<slug>/<id>.json`, `data/attachments/<id>/extracted.json`, and the "
             "hand-marked list in `_runs/2026-09-13_phase3_donecheck/hand_marked/`.")
    L.append("")
    L.append("Rebuild: `PYTHONIOENCODING=utf-8 python evals/run_evals.py --parts 1 2 3`.")
    L.append("")

    p1 = state["parts"].get("tiering")
    if p1:
        L.append("## 1. Tiering expectations (deterministic, no model call)")
        L.append("")
        L.append(f"Command: `{p1['command']}`. Tier 0 is re-run in code against every notice in the "
                 "snapshot at the bench's own `as_of`, so the deadline rule decides the same way it did "
                 "at bench time. Tier 0 is first-kill-wins: the expected rule id is the first owner rule "
                 "that fires. Only tier 0 is asserted here; tier 1 is a model call and is not re-run.")
        L.append("")
        L.append(md_table(["firm", "as_of", "must file by rule", "pass", "should surface", "pass",
                           "unexpected filings", "hand-checked"],
                          [[r["firm"], r["as_of"], r["filed_expected"], r["filed_pass"],
                            r["surface_expected"], r["surface_pass"], len(r["unexpected_filings"]),
                            len(r["hand_checked"])] for r in p1["rows"]]))
        L.append("")
        L.append(f"Result: **{'PASS' if p1['all_ok'] else 'FAIL'}** "
                 f"({p1['totals']['filed_pass']}/{p1['totals']['filed_expected']} rule filings, "
                 f"{p1['totals']['surface_pass']}/{p1['totals']['surface_expected']} surfacing notices "
                 "clear of every rule). Ten filings per profile were read by hand against the notice "
                 "fields; the ids and what settles each one are in `evals/expected_<slug>.json` under "
                 "`hand_checked`.")
        L.append("")

    p2 = state["parts"].get("handmark")
    if p2:
        L.append("## 2. Matrix completeness against a hand-marked list (deterministic)")
        L.append("")
        L.append(f"Command: `{p2['command']}`. The phase-3 hand-marked list covers ten attachment pages, "
                 "marked before the extractor was read. A statement counts as carried when a kept matrix "
                 "row cites a requirement whose text contains it, or when the row's quote and the "
                 "statement contain one another after whitespace and quote folding, on the same file and "
                 "page. Three hand-marked notices produced no gallery card, so they carry no score.")
        L.append("")
        L.append(md_table(["notice", "firm", "hand statements", "carried", "completeness", "matrix rows"],
                          [[r["notice_id"][:8], r["firm"] or "(no card)", r["hand_statements"],
                            r["carried"], r["completeness"], r.get("matrix_kept")]
                           for r in p2["rows"]]))
        L.append("")
        L.append(f"Total over the {p2['cases_scored']} scored cases: {p2['carried']}/{p2['hand_statements']} "
                 f"= **{p2['completeness']}**. This is a coverage number against one human's marking of ten "
                 "pages, not a claim about every attachment.")
        L.append("")
        L.append("Most of the gap is the desk's cap, not a failed read: the matrix keeps 26-37 ranked rows out of every requirement the extractor found, so a hand-marked page the ranking never reached scores near zero by construction. `54a51abc` is that case: its 29 kept rows cite the 394-requirement PWS attachment, while the hand-marked page is the 5-requirement RFI description document (404 requirements extracted in all).")
        L.append("")

    p3 = state["parts"].get("judge")
    if p3:
        L.append("## 3. Matrix completeness, judge model (an opinion, not a measurement)")
        L.append("")
        L.append(f"Command: `{p3['command']}`. One structured-output call per gallery case on "
                 f"`{p3['model']}`, temperature 0. The judge sees the kept matrix rows and the case's "
                 f"extracted requirements capped at the {JUDGE_CAP} highest-ranked, ranked by "
                 "`desk.select_requirements`: category priority (submission, evaluation, schedule, scope, "
                 "staffing, compliance, other) then document order, which is the same ordering the desk "
                 "hands its specialists. The score is the model's opinion of coverage; it is reported as "
                 "such and is never mixed into the deterministic numbers above.")
        L.append("")
        L.append(md_table(["notice", "firm", "score", "matrix rows", "reqs shown / total",
                           "calls", "tokens in", "tokens out", "error"],
                          [[r["notice_id"][:8], r["firm"], r["score"], r.get("matrix_kept"),
                            f"{r.get('requirements_shown')} / {r.get('requirements_total')}"
                            if r.get("requirements_total") is not None else None,
                            r["model_calls"], r["tokens_in"], r["tokens_out"],
                            (r["error"] or "")[:60]] for r in p3["rows"]]))
        L.append("")
        L.append(f"Scored {p3['scored']} of {p3['cases']} cases; mean score **{p3['mean_score']}**. "
                 f"Cost: {p3['model_calls']} calls, {p3['tokens_in']} tokens in, {p3['tokens_out']} out, "
                 f"all on `{p3['model']}`.")
        if p3["throttled"]:
            L.append("")
            L.append("Bedrock throttled this part. The exact error text:")
            L.append("")
            L.append("```")
            L.append(p3["throttle_error"])
            L.append("```")
            L.append("")
            L.append("Cases that did not run carry `score: null`. No score was filled in.")
        L.append("")

    L.append("## What is and is not proven here")
    L.append("")
    L.append("- Parts 1 and 2 are deterministic: no model runs, and re-running them on the committed "
             "files reproduces the numbers exactly.")
    L.append("- Part 3 is one model's opinion of another model's output. It is useful as a relative "
             "signal across cases and worthless as ground truth.")
    L.append("- The hand-marked list is ten attachment pages marked by one person. It is the only "
             "human ground truth in this repo.")
    L.append("")
    BENCH_MD.write_text("\n".join(L) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the Biddesk eval bench.")
    ap.add_argument("--parts", nargs="*", default=["1", "2"], help="which parts to run: 1 2 3")
    ap.add_argument("--profiles", nargs="*", default=list(profiles.PROFILES))
    ap.add_argument("--build-expected", action="store_true", help="rewrite evals/expected_<slug>.json and stop")
    ap.add_argument("--workers", type=int, default=2, help="judge concurrency (part 3)")
    ap.add_argument("--limit", type=int, default=0, help="judge at most N cases (part 3)")
    ap.add_argument("--only-unscored", action="store_true",
                    help="part 3: re-run only the cases that carry no score yet, keeping the ones that landed")
    a = ap.parse_args(argv)

    if a.build_expected:
        for slug in a.profiles:
            out = build_expected(slug)
            print(f"{slug:15s} expected: {len(out['filed_by_rule'])} filed by rule, "
                  f"{len(out['should_surface'])} should surface, {len(out['hand_checked'])} hand-checked")
        return 0

    state = json.loads(BENCH_JSON.read_text(encoding="utf-8")) if BENCH_JSON.exists() else {"parts": {}}
    state["as_of"] = now_iso()
    state["model_primary"] = config.MODEL_PRIMARY
    state["gallery_produced_at"] = gallery_index().get("produced_at")
    state.setdefault("parts", {})

    if "1" in a.parts:
        state["parts"]["tiering"] = part1(a.profiles)
        save(state)
        p = state["parts"]["tiering"]
        print(f"part 1 tiering expectations: {'PASS' if p['all_ok'] else 'FAIL'}  "
              f"{p['totals']['filed_pass']}/{p['totals']['filed_expected']} rule filings, "
              f"{p['totals']['surface_pass']}/{p['totals']['surface_expected']} surfacing notices, 0 model calls")

    if "2" in a.parts:
        state["parts"]["handmark"] = part2()
        save(state)
        p = state["parts"]["handmark"]
        print(f"part 2 hand-marked completeness: {p['carried']}/{p['hand_statements']} = {p['completeness']} "
              f"over {p['cases_scored']} cases ({p['cases_not_in_gallery']} hand-marked notices have no card), "
              "0 model calls")

    if "3" in a.parts:
        cases = gallery_cases()
        prior = {r["notice_id"]: r for r in (state["parts"].get("judge") or {}).get("rows", [])}
        if a.only_unscored:
            cases = [c for c in cases if prior.get(c["notice_id"], {}).get("score") is None]
            print(f"part 3: {len(cases)} cases still unscored")
        else:
            prior = {}
        if a.limit:
            cases = cases[:a.limit]
        state["parts"]["judge"] = {"name": "matrix completeness, judge model", "status": "running",
                                   "model": config.MODEL_PRIMARY, "cases": len(cases)}
        save(state)

        def flush():
            save(state)

        try:
            state["parts"]["judge"] = part3(cases, a.workers, on_row=flush, prior=prior)
        except Exception:                              # noqa: BLE001 - never lose the finished parts
            state["parts"]["judge"]["status"] = "failed"
            state["parts"]["judge"]["error"] = traceback.format_exc(limit=3)
            save(state)
            write_md(state)
            raise
        save(state)
        p = state["parts"]["judge"]
        print(f"part 3 judge ({p['model']}): scored {p['scored']}/{p['cases']}, mean {p['mean_score']}, "
              f"{p['model_calls']} calls, {p['tokens_in']} tokens in, {p['tokens_out']} out"
              + (f"  THROTTLED: {p['throttle_error']}" if p["throttled"] else ""))

    save(state)
    write_md(state)
    print(f"wrote {BENCH_JSON.relative_to(ROOT)} and {BENCH_MD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
