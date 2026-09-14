"""The background week: replay the committed snapshot day by day, in posted order.

Nothing here calls a model or the network. Every count comes from a committed file:

* ``data/snapshot/<slug>.json``      which notice was posted on which day
* ``data/bench/tiering_<slug>.json`` what each tier decided (tier 0 rule id, tier 1, tier 2)
* ``gallery/index.json``             which tier-2 notice produced a card
* ``data/raw/pub_*.json``            the cached public notice JSON, for amendment parentage

Usage: python -m biddesk.week [--profile <slug> ...] [--no-write]
Writes data/bench/week_<slug>.json and prints the done-check line per profile.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from typing import Optional

from . import config, profiles, snapshot
from .models import Notice
from .sam_client import PUBLIC_NOTICE_URL, now_iso


class WeekError(RuntimeError):
    """A replay input is missing or inconsistent; never smoothed over."""


# ------------------------------------------------------------------ inputs

def bench(slug: str) -> dict:
    path = config.DATA / "bench" / f"tiering_{slug}.json"
    if not path.exists():
        raise WeekError(f"no tiering bench for {slug}: run python -m biddesk.tiering first ({path})")
    return json.loads(path.read_text(encoding="utf-8"))


def gallery_cards(slug: str) -> list[str]:
    """Notice ids that produced a gallery card for this firm, from the built index."""
    path = config.GALLERY / "index.json"
    if not path.exists():
        raise WeekError(f"no gallery index at {path}: run python -m biddesk.desk gallery first")
    index = json.loads(path.read_text(encoding="utf-8"))
    for firm in index.get("firms", []):
        if firm.get("slug") == slug:
            return [c["notice_id"] for c in firm.get("cases", [])]
    return []


def _cached_public(notice_id: str) -> Optional[dict]:
    """The cached public notice JSON body, or None when this notice was never fetched.

    Offline by construction: the cache path is recomputed the way ``sam_client`` writes it
    and read straight off disk, so a replay can never reach the network.
    """
    url = PUBLIC_NOTICE_URL.format(notice_id=notice_id)
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    path = config.RAW / f"pub_{h}.json"
    if not path.exists():
        return None
    try:
        env = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return env.get("body") or {}


def _deadline(body: dict) -> Optional[str]:
    d = ((body.get("data2") or {}).get("solicitation") or {}).get("deadlines") or {}
    return d.get("response")


def amendment_row(notice: Notice) -> Optional[dict]:
    """An amendment row for the week, or None when the notice shows no amendment marker.

    Detection matches ``desk.amendment_note``: a base type that differs from the type, or
    "amend" in the title. The deadline comparison is only reported when BOTH the previous
    version's public JSON and this notice's public JSON are cached; otherwise the row says
    "previous version not cached" and claims no movement.
    """
    marker = []
    if notice.base_type and notice.base_type != notice.type:
        marker.append(f"type {notice.type} on base type {notice.base_type}")
    if "amend" in notice.title.lower():
        marker.append("'amend' in the title")
    if not marker:
        return None
    body = _cached_public(notice.notice_id)
    parent = ((body or {}).get("parent") or {}).get("opportunityId")
    row = {
        "notice_id": notice.notice_id,
        "title": notice.title,
        "why": "; ".join(marker),
        "parent_opportunity_id": parent,
        "public_json_cached": body is not None,
        "previous_deadline": None,
        "current_deadline": notice.response_deadline,
        "deadline_moved": None,
        "note": "",
    }
    if not parent:
        row["note"] = ("no parent.opportunityId in the cached public JSON"
                       if body is not None else "public JSON for this notice is not cached")
        return row
    prev = _cached_public(parent)
    if prev is None:
        row["note"] = "previous version not cached"
        return row
    row["previous_deadline"] = _deadline(prev)
    cur = _deadline(body) or notice.response_deadline
    row["current_deadline"] = cur
    if row["previous_deadline"] and cur:
        row["deadline_moved"] = row["previous_deadline"] != cur
        row["note"] = "deadline moved" if row["deadline_moved"] else "deadline unchanged"
    else:
        row["note"] = "previous version cached but carries no response deadline"
    return row


# ------------------------------------------------------------------ replay

def replay(slug: str) -> dict:
    prof = profiles.PROFILES[slug]
    data = bench(slug)
    meta, notices = snapshot.load(slug)
    by_id = {n.notice_id: n for n in notices}
    cards = gallery_cards(slug)
    card_ids = set(cards)

    rows = data.get("rows") or []
    if not rows:
        raise WeekError(f"tiering bench for {slug} has no rows")
    tier2_ids = {r["notice_id"] for r in rows if r.get("tier") == 2}
    stray = sorted(card_ids - tier2_ids)
    if stray:
        raise WeekError(f"{slug}: gallery cards not in the bench tier-2 set: {stray}")

    days: dict[str, dict] = {}
    missing_posted: list[str] = []
    missing_notice: list[str] = []
    for r in rows:
        nid = r["notice_id"]
        n = by_id.get(nid)
        if n is None:
            missing_notice.append(nid)
            continue
        if not n.posted:
            missing_posted.append(nid)
            continue
        day = days.setdefault(n.posted, {
            "date": n.posted, "posted": 0, "filed_by_rule": 0, "by_rule": {},
            "filed_by_model": 0, "sent_to_desk": 0, "surfaced": [], "no_card": [],
            "amendments": [],
        })
        day["posted"] += 1
        tier, outcome = r.get("tier"), r.get("outcome")
        if tier == 0 and outcome == "filed":
            day["filed_by_rule"] += 1
            rid = r.get("rule_id") or "?"
            day["by_rule"][rid] = day["by_rule"].get(rid, 0) + 1
        elif tier == 1 and outcome == "filed":
            day["filed_by_model"] += 1
            rid = r.get("rule_id") or "scope"
            day["by_rule"][rid] = day["by_rule"].get(rid, 0) + 1
        else:
            day["sent_to_desk"] += 1
            if nid in card_ids:
                day["surfaced"].append(nid)
            else:
                day["no_card"].append({"notice_id": nid,
                                       "why": "sent to the desk, no card: no evidence"})
        amend = amendment_row(n)
        if amend:
            day["amendments"].append(amend)

    if missing_notice:
        raise WeekError(f"{slug}: bench rows name notices that are not in the snapshot: {missing_notice[:5]}")
    if missing_posted:
        raise WeekError(f"{slug}: notices with an empty posted date cannot be placed on a day: {missing_posted[:5]}")

    day_rows = [days[d] for d in sorted(days)]
    totals = {
        "days": len(day_rows),
        "posted": sum(d["posted"] for d in day_rows),
        "filed_by_rule": sum(d["filed_by_rule"] for d in day_rows),
        "filed_by_model": sum(d["filed_by_model"] for d in day_rows),
        "sent_to_desk": sum(d["sent_to_desk"] for d in day_rows),
        "surfaced": sum(len(d["surfaced"]) for d in day_rows),
        "no_card": sum(len(d["no_card"]) for d in day_rows),
        "amendments_seen": sum(len(d["amendments"]) for d in day_rows),
    }
    by_rule: dict[str, int] = {}
    for d in day_rows:
        for k, v in d["by_rule"].items():
            by_rule[k] = by_rule.get(k, 0) + v
    totals["by_rule"] = by_rule

    counts = data.get("counts") or {}
    surfaced_ids = [nid for d in day_rows for nid in d["surfaced"]]
    invariants = check_invariants(totals, counts, surfaced_ids, card_ids, rows)

    amend_rows = [a for d in day_rows for a in d["amendments"]]
    out = {
        "firm": slug,
        "name": prof.name,
        "as_of": now_iso(),
        "bench_as_of": data.get("as_of"),
        "snapshot_written_at": meta.get("written_at"),
        "gallery_produced_at": json.loads((config.GALLERY / "index.json").read_text(encoding="utf-8")).get("produced_at"),
        "window": {"first_posted": day_rows[0]["date"] if day_rows else None,
                   "last_posted": day_rows[-1]["date"] if day_rows else None,
                   "days_with_notices": len(day_rows)},
        "model_calls": 0,
        "sources": {
            "snapshot": f"data/snapshot/{slug}.json",
            "bench": f"data/bench/tiering_{slug}.json",
            "gallery": "gallery/index.json",
            "public_notice_cache": "data/raw/pub_*.json",
        },
        "days": day_rows,
        "totals": totals,
        "amendments": {
            "detected": len(amend_rows),
            "with_parent_id": sum(1 for a in amend_rows if a["parent_opportunity_id"]),
            "previous_version_cached": sum(1 for a in amend_rows if a["previous_deadline"] is not None),
            "deadline_moved": sum(1 for a in amend_rows if a["deadline_moved"] is True),
            "rows": amend_rows,
        },
        "invariants": invariants,
    }
    return out


def check_invariants(totals: dict, counts: dict, surfaced_ids: list[str],
                     card_ids: set[str], rows: list[dict]) -> dict:
    """The PLAN phase-5 done-check, as data: only cards above the bar surface."""
    tier_of = {r["notice_id"]: (r.get("tier"), r.get("outcome")) for r in rows}
    below_bar = [nid for nid in surfaced_ids
                 if tier_of.get(nid, (None, None))[0] != 2]
    checks = [
        {"name": "every surfaced id is a gallery case",
         "ok": all(nid in card_ids for nid in surfaced_ids),
         "detail": f"{len(surfaced_ids)} surfaced, {len(card_ids)} gallery cards"},
        {"name": "every gallery case surfaces exactly once",
         "ok": sorted(surfaced_ids) == sorted(card_ids) and len(set(surfaced_ids)) == len(surfaced_ids),
         "detail": f"{len(set(surfaced_ids))} distinct surfaced ids"},
        {"name": "no tier-0 or tier-1 notice surfaces",
         "ok": not below_bar,
         "detail": "none" if not below_bar else f"below the bar: {below_bar}"},
        {"name": "per-day counts sum to the bench counts",
         "ok": (totals["posted"] == counts.get("total")
                and totals["filed_by_rule"] == counts.get("tier0_filed")
                and totals["filed_by_model"] == counts.get("tier1_filed")
                and totals["sent_to_desk"] == counts.get("tier2_sent")
                and totals["by_rule"] == (counts.get("by_rule") or {})),
         "detail": (f"week posted/filed/model/desk = {totals['posted']}/{totals['filed_by_rule']}/"
                    f"{totals['filed_by_model']}/{totals['sent_to_desk']}; bench = {counts.get('total')}/"
                    f"{counts.get('tier0_filed')}/{counts.get('tier1_filed')}/{counts.get('tier2_sent')}")},
        {"name": "the replay made no model call",
         "ok": True, "detail": "0 model calls: every outcome is read from the committed bench"},
    ]
    return {"all_ok": all(c["ok"] for c in checks), "checks": checks}


def week_block(week: dict) -> list[dict]:
    """The per-firm ``week`` array the page renders: one box per posted day.

    ``amendment`` is set only when a deadline move is proven from two cached public
    responses. With no previous version cached it stays absent, so the page never claims
    a move it cannot show.
    """
    out = []
    for d in week["days"]:
        box = {"date": d["date"], "posted": d["posted"],
               "filed": d["filed_by_rule"] + d["filed_by_model"],
               "surfaced": len(d["surfaced"])}
        if any(a.get("deadline_moved") is True for a in d["amendments"]):
            box["amendment"] = True
        out.append(box)
    return out


def done_check_line(week: dict) -> str:
    inv = week["invariants"]
    t = week["totals"]
    state = "PASS" if inv["all_ok"] else "FAIL"
    return (f"{week['firm']:15s} {state}  {t['days']} days {week['window']['first_posted']}.."
            f"{week['window']['last_posted']}  posted {t['posted']}  filed {t['filed_by_rule']} by rule + "
            f"{t['filed_by_model']} by the cheap model  desk {t['sent_to_desk']}  surfaced {t['surfaced']}  "
            f"no card {t['no_card']}  amendments {t['amendments_seen']}  model calls {week['model_calls']}")


def write(slug: str) -> dict:
    out = replay(slug)
    path = config.DATA / "bench" / f"week_{slug}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    return out


def load(slug: str) -> Optional[dict]:
    path = config.DATA / "bench" / f"week_{slug}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Replay the committed snapshot day by day.")
    ap.add_argument("--profile", "--profiles", dest="profiles", nargs="*", default=list(profiles.PROFILES))
    ap.add_argument("--no-write", action="store_true", help="print the done-check without writing the file")
    a = ap.parse_args(argv)
    bad = 0
    for slug in a.profiles:
        out = replay(slug) if a.no_write else write(slug)
        print(done_check_line(out))
        for c in out["invariants"]["checks"]:
            if not c["ok"]:
                bad += 1
                print(f"    FAIL {c['name']}: {c['detail']}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
