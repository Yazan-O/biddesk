"""Pull a window of SAM.gov notices per firm profile and write the committed snapshot.

Usage: python -m biddesk.snapshot [--days 7] [--no-desc] [--show-one]
       python -m biddesk.snapshot --public --days 30 --profiles red-cedar   (unkeyed route)
Writes data/snapshot/<slug>.json  ({"meta": {...}, "notices": [Notice...]})
"""
from __future__ import annotations
import argparse
import json
import sys

from . import config
from .models import Notice
from .profiles import PROFILES
from .sam_client import (PUBLIC_SEARCH_URL, fetch_description, fetch_notice_public,
                         fetch_resource_links, normalize, normalize_public, now_iso,
                         search, search_public, SamError)


def pull_profile(slug: str, days: int, with_desc: bool) -> dict:
    prof = PROFILES[slug]
    seen: dict[str, Notice] = {}
    metas = []
    for n in prof.naics:
        raws, meta = search(n, days=days)
        metas.append(meta)
        for r in raws:
            if r["noticeId"] not in seen:
                seen[r["noticeId"]] = normalize(r, meta["fetched_at"])
    notices = sorted(seen.values(), key=lambda x: (x.posted, x.notice_id), reverse=True)
    desc_ok = desc_fail = 0
    if with_desc:
        for nt in notices:
            try:
                nt.description_text = fetch_description(nt)
                desc_ok += 1
            except SamError as e:
                print(f"  [desc] {nt.notice_id}: {e}", file=sys.stderr)
                desc_fail += 1
                if "429" in str(e):
                    break
    out = {"meta": {"firm": slug, "naics": prof.naics, "windows": metas, "count": len(notices),
                    "descriptions_fetched": desc_ok, "descriptions_failed": desc_fail,
                    "written_at": now_iso(), "source": config.SAM_SEARCH_URL},
           "notices": [n.model_dump() for n in notices]}
    (config.SNAPSHOT / f"{slug}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    return out


def pull_profile_public(slug: str, days: int, workers: int = 8) -> dict:
    """Widen a profile's snapshot over the unkeyed sam.gov routes (no API key, so it works while
    the keyed Get Opportunities key is throttled).

    Merge rule: a notice already in the saved snapshot keeps its existing record verbatim; only
    notices the wider window adds are normalized from the public JSON. The public route is
    `is_active=true`, so it returns open notices only.
    """
    from concurrent.futures import ThreadPoolExecutor
    prof = PROFILES[slug]
    try:
        old_meta, old_notices = load(slug)
    except FileNotFoundError:
        old_meta, old_notices = {}, []
    kept = {n.notice_id: n for n in old_notices}

    hits, meta = search_public(prof.naics, days=days)
    new_hits = [h for h in hits if h["_id"] not in kept]
    print(f"{slug:15s} public search: {meta['totalActiveRecords']} active in NAICS {meta['naics']}, "
          f"{meta['kept_in_window']} posted >= {meta['posted_from']}, {len(new_hits)} not already in the snapshot",
          file=sys.stderr)

    added: dict[str, Notice] = {}
    errors: list[str] = []

    def one(hit: dict):
        nid = hit["_id"]
        try:
            env = fetch_notice_public(nid)
            links = fetch_resource_links(nid)
            return nid, normalize_public(env["body"], hit, env["_fetched_at"], links), None
        except SamError as e:
            return nid, None, str(e)
        except Exception as e:            # timeouts, resets, malformed payloads: record, keep going
            return nid, None, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(workers) as ex:
        for nid, notice, err in ex.map(one, new_hits):
            if err:
                errors.append(f"{nid}: {err}")
                print(f"  [public] {nid}: {err}", file=sys.stderr)
            else:
                added[nid] = notice

    merged = {**kept, **added}
    notices = sorted(merged.values(), key=lambda x: (x.posted, x.notice_id), reverse=True)
    windows = list(old_meta.get("windows") or [])
    out_meta = {
        "firm": slug, "naics": prof.naics, "windows": windows, "count": len(notices),
        "descriptions_fetched": sum(1 for n in notices if n.description_text),
        "descriptions_failed": len(errors),
        "written_at": now_iso(),
        "source": (f"{PUBLIC_SEARCH_URL} (unkeyed) index=opp is_active=true naics={meta['naics']} "
                   f"sort=-modifiedDate size=100; window posted {meta['posted_from']}..{meta['posted_to']} "
                   f"filtered client-side; fetched_at {meta['fetched_at']}"),
        "public_window": meta,
        "kept_from_previous": len(kept),
        "added_from_public": len(added),
        "public_errors": errors,
        "previous_source": old_meta.get("source"),
        "previous_count": old_meta.get("count"),
    }
    out = {"meta": out_meta, "notices": [n.model_dump() for n in notices]}
    (config.SNAPSHOT / f"{slug}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{slug:15s} kept={len(kept)} added={len(added)} total={len(notices)} "
          f"desc={out_meta['descriptions_fetched']}/{len(notices)} errors={len(errors)}")
    return out


def fill_descriptions(slug: str, workers: int = 8) -> dict:
    """Fetch every missing description for a saved snapshot, in parallel (SAM answers each keyed call in ~64 s
    regardless of concurrency, so width is what makes this finish). Rewrites the snapshot file."""
    from concurrent.futures import ThreadPoolExecutor
    meta, notices = load(slug)
    todo = [n for n in notices if n.description_url and not n.description_text]
    ok = fail = 0

    def one(n: Notice):
        try:
            return n, fetch_description(n), None
        except SamError as e:
            return n, None, str(e)

    with ThreadPoolExecutor(workers) as ex:
        for n, txt, err in ex.map(one, todo):
            if err:
                fail += 1
                print(f"  [desc] {n.notice_id}: {err}", file=sys.stderr)
            else:
                n.description_text = txt
                ok += 1
    meta["descriptions_fetched"] = sum(1 for n in notices if n.description_text)
    meta["descriptions_failed"] = fail
    meta["written_at"] = now_iso()
    out = {"meta": meta, "notices": [n.model_dump() for n in notices]}
    (config.SNAPSHOT / f"{slug}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{slug:15s} descriptions filled: +{ok} (failed {fail}) -> {meta['descriptions_fetched']}/{meta['count']}")
    return meta


def load(slug: str) -> tuple[dict, list[Notice]]:
    d = json.loads((config.SNAPSHOT / f"{slug}.json").read_text(encoding="utf-8"))
    return d["meta"], [Notice(**n) for n in d["notices"]]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--no-desc", action="store_true")
    ap.add_argument("--show-one", action="store_true")
    ap.add_argument("--profiles", nargs="*", default=list(PROFILES))
    ap.add_argument("--fill-desc", action="store_true", help="only fill missing descriptions on the saved snapshot")
    ap.add_argument("--public", action="store_true",
                    help="pull over the unkeyed sam.gov sgs route and merge into the saved snapshot")
    a = ap.parse_args(argv)
    if a.fill_desc:
        for slug in a.profiles:
            fill_descriptions(slug)
        return
    if a.public:
        for slug in a.profiles:
            pull_profile_public(slug, a.days)
        if a.show_one:
            _, notices = load(a.profiles[0])
            print(json.dumps(notices[0].model_dump(), indent=1, ensure_ascii=False))
        return
    for slug in a.profiles:
        out = pull_profile(slug, a.days, not a.no_desc)
        m = out["meta"]
        by_type: dict[str, int] = {}
        for n in out["notices"]:
            by_type[n["type"]] = by_type.get(n["type"], 0) + 1
        print(f"{slug:15s} notices={m['count']:4d}  window={m['windows'][0]['postedFrom']}..{m['windows'][0]['postedTo']}  "
              f"naics={m['naics']}  desc={m['descriptions_fetched']}/{m['count']}  types={by_type}")
    if a.show_one:
        slug = a.profiles[0]
        _, notices = load(slug)
        print(json.dumps(notices[0].model_dump(), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
