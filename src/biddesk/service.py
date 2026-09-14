"""AgentCore Runtime entrypoint for Biddesk.

One HTTP service (``POST /invocations``, ``GET /ping``) in front of the desk, the
triage tiers, and the ledger. The payload is untrusted: every field is type-checked
here before it reaches any module.

Local run::

    python -m biddesk.service          # 0.0.0.0:8080
    curl -s localhost:8080/ping
    curl -s localhost:8080/invocations -H 'content-type: application/json' \
         -d '{"action":"health"}'

Nothing in this file prints a key, a credential, or an account id.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from . import config, profiles, sam_client, snapshot
from .ledger import Ledger

app = BedrockAgentCoreApp()

ACTIONS = ("triage", "desk", "answer_card", "undo", "ledger", "refusal", "week", "health")

#: Session storage mount configured on the Runtime. Available at invocation time only,
#: never at import time, so it is probed inside the handler.
MOUNT_LEDGER_DIR = Path("/mnt/data/ledger")
PACKAGE_LEDGER_DIR = config.DATA / "ledger"

SERVICE_VERSION = "0.1.0"


# ---------------------------------------------------------------- helpers

def now_iso() -> str:
    return sam_client.now_iso()


def _ok(action: str, **fields: Any) -> dict:
    out = {"ok": True, "action": action, "produced_at": now_iso()}
    out.update(fields)
    return out


_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")


def redact(text: str) -> str:
    """Strip anything that looks like an AWS account id out of a message.

    AWS error strings quote the caller ARN, which carries the account id; the
    envelope goes to a public page, so the id never leaves the container.
    """
    return _ACCOUNT_ID.sub("<account>", text)


def _err(action: str, exc: BaseException) -> dict:
    """Error envelope. The class name and the message only: no traceback, no paths."""
    return {"ok": False, "action": action, "produced_at": now_iso(),
            "error": redact(f"{type(exc).__name__}: {exc}")}


def _writable(directory: Path) -> bool:
    """True when a file can actually be created in ``directory``."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def ledger_location(mount_dir: Path = MOUNT_LEDGER_DIR,
                    package_dir: Path = PACKAGE_LEDGER_DIR,
                    allow_mount: Optional[bool] = None) -> tuple[str, Path]:
    """Pick the ledger directory: session mount first, then the package, then temp.

    Returns ``("live"|"package", directory)``. The mount exists only while an
    invocation is running, so this is called per request, never at import. The
    mount is a Linux path on the Runtime, so it is not probed on a dev box.
    """
    if allow_mount is None:
        allow_mount = os.name == "posix"
    if allow_mount and _writable(mount_dir):
        return "live", mount_dir
    if _writable(package_dir):
        return "package", package_dir
    fallback = Path(tempfile.gettempdir()) / "biddesk-ledger"
    fallback.mkdir(parents=True, exist_ok=True)
    return "package", fallback


def _ledger_for(slug: str) -> tuple[Ledger, str, Path]:
    scope, directory = ledger_location()
    return Ledger(slug, path=directory / f"{slug}.jsonl"), scope, directory


def _firm(payload: dict) -> str:
    slug = payload.get("firm")
    if not isinstance(slug, str) or not slug:
        raise ValueError("payload needs a 'firm' slug (string)")
    if slug not in profiles.PROFILES:
        raise ValueError(f"unknown firm {slug!r}; known firms: {', '.join(sorted(profiles.PROFILES))}")
    return slug


def _text(payload: dict, key: str, required: bool = True, default: str = "") -> str:
    value = payload.get(key, default)
    if value is None and not required:
        return default
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string")
    if required and not value:
        raise ValueError(f"payload needs '{key}'")
    return value


def _rows_as_dicts(rows) -> list[dict]:
    return [r.model_dump() for r in rows]


def _bench_path(slug: str) -> Path:
    return config.DATA / "bench" / f"tiering_{slug}.json"


# ---------------------------------------------------------------- actions

def _cached_triage(slug: str, reason: str) -> dict:
    """Triage served from the bench file written by ``tiering.run_bench``.

    No model call, no network call. ``candidates`` are the tier-2 rows: the notices
    the desk would open.
    """
    path = _bench_path(slug)
    if not path.exists():
        raise FileNotFoundError(f"no cached triage for {slug} (data/bench/tiering_{slug}.json missing)")
    data = json.loads(path.read_text(encoding="utf-8"))
    counts = data.get("counts", {})
    rows = data.get("rows", [])
    candidates = [
        {"notice_id": r.get("notice_id"), "title": r.get("title"), "type": r.get("type"),
         "set_aside_code": r.get("set_aside_code"), "why": r.get("why")}
        for r in rows if r.get("tier") == 2
    ]
    return {
        "firm": slug,
        "counts": {k: v for k, v in counts.items() if k != "by_rule"},
        "by_rule": counts.get("by_rule", {}),
        "candidates": candidates,
        "cached": True,
        "reason": reason,
        "as_of": data.get("as_of") or data.get("snapshot_written_at"),
        "model": data.get("model"),
    }


def _pull_keyed(profile, days: int, fetched_at: str) -> tuple[list, list[dict]]:
    """Keyed Get Opportunities search, one call per NAICS. Raises SamThrottled on a 429."""
    raw: list[dict] = []
    seen: set[str] = set()
    windows = []
    for naics in profile.naics:
        items, meta = sam_client.search(naics, days=days)
        windows.append({"naics": naics, "count": len(items),
                        "totalRecords": meta.get("totalRecords")})
        for item in items:
            nid = item.get("noticeId")
            if nid and nid not in seen:
                seen.add(nid)
                raw.append(item)
    return [sam_client.normalize(r, fetched_at) for r in raw], windows


def _pull_public(profile, days: int, workers: int = 8) -> tuple[list, list[dict], list[str]]:
    """Unkeyed sam.gov search (the route the sam.gov page itself uses), then the public notice
    JSON per hit for NAICS, set-aside, place of performance and description. No cache: a live
    triage is a live pull, so `produced_at` means what it says. Open notices only (is_active)."""
    from concurrent.futures import ThreadPoolExecutor
    hits, meta = sam_client.search_public(profile.naics, days=days, use_cache=False)
    errors: list[str] = []

    def one(hit: dict):
        nid = hit["_id"]
        try:
            env = sam_client.fetch_notice_public(nid, use_cache=False)
            return sam_client.normalize_public(env["body"], hit, env["_fetched_at"]), None
        except Exception as e:  # noqa: BLE001 - one bad notice must not sink the pull
            return None, f"{nid}: {type(e).__name__}: {e}"

    notices = []
    with ThreadPoolExecutor(workers) as ex:
        for notice, err in ex.map(one, hits):
            if err:
                errors.append(redact(err))
            else:
                notices.append(notice)
    windows = [{"naics": meta["naics"], "count": len(notices), "kept_in_window": meta["kept_in_window"],
                "totalActiveRecords": meta["totalActiveRecords"], "pages_fetched": meta["pages_fetched"],
                "posted_from": meta["posted_from"], "posted_to": meta["posted_to"],
                "is_active_only": True, "url": meta["url"]}]
    return notices, windows, errors


def _live_triage(slug: str, days: int = 7, route: str = "keyed") -> dict:
    """Live SAM pull over ``route`` ("keyed" first choice, "public" when the key is throttled or
    the keyed API is unavailable), then tier 0 in code and tier 1 on Haiku for what survives."""
    from . import tiering

    profile = profiles.get(slug)
    fetched_at = now_iso()
    errors: list[str] = []
    if route == "keyed":
        notices, windows = _pull_keyed(profile, days, fetched_at)
        reason = "live SAM.gov keyed search"
    elif route == "public":
        notices, windows, errors = _pull_public(profile, days)
        reason = "live SAM.gov public search (unkeyed route; the API key is throttled or unavailable)"
    else:
        raise ValueError(f"unknown route {route!r}")
    triages, counts = tiering.triage_all(profile, notices, use_model=True)
    by_id = {n.notice_id: n for n in notices}
    candidates = []
    for t in triages:
        if t.tier != 2:
            continue
        n = by_id.get(t.notice_id)
        candidates.append({"notice_id": t.notice_id,
                           "title": getattr(n, "title", None),
                           "type": getattr(n, "type", None),
                           "set_aside_code": getattr(n, "set_aside_code", None),
                           "why": t.why})
    as_dict = counts.as_dict()
    out = {
        "firm": slug,
        "route": route,
        "days": days,
        "counts": {k: v for k, v in as_dict.items() if k != "by_rule"},
        "by_rule": as_dict.get("by_rule", {}),
        "candidates": candidates,
        "cached": False,
        "reason": reason,
        "as_of": fetched_at,
        "windows": windows,
        "model": config.MODEL_FALLBACK,
    }
    if errors:
        out["public_errors"] = errors
    return out


def action_triage(payload: dict) -> dict:
    """Keyed live pull first; the unkeyed public route when the key is throttled or the keyed API
    fails; the committed bench snapshot only when both routes fail. The answer says which."""
    slug = _firm(payload)
    if sam_client.throttled_until() is None:
        try:
            return _live_triage(slug, route="keyed")
        except sam_client.SamError:      # SamThrottled included: fall through to the public route
            pass
    try:
        return _live_triage(slug, route="public")
    except sam_client.SamError as exc:
        return _cached_triage(slug, f"SAM.gov unavailable on both routes: {type(exc).__name__}")


def action_desk(payload: dict) -> dict:
    from . import desk

    slug = _firm(payload)
    notice_id = _text(payload, "notice_id")
    model = _text(payload, "model", required=False, default="sonnet") or "sonnet"
    if model not in ("sonnet", "haiku"):
        raise ValueError("'model' must be 'sonnet' or 'haiku'")
    ledger, scope, _dir = _ledger_for(slug)
    notice = desk.find_notice(slug, notice_id)
    result = desk.run_case(slug, notice, model=model, ledger=ledger)
    return {"firm": slug, "notice_id": notice.notice_id, "model": model,
            "ledger_scope": scope, "result": result.model_dump()}


def action_answer_card(payload: dict) -> dict:
    slug = _firm(payload)
    notice_id = _text(payload, "notice_id")
    answer = _text(payload, "answer").strip().lower().replace("_", "-")
    if answer not in ("bid", "no-bid"):
        raise ValueError("'answer' must be 'bid' or 'no-bid'")
    reason = _text(payload, "reason", required=False)
    ledger, scope, _dir = _ledger_for(slug)
    row = ledger.write(
        notice_id=notice_id,
        action="card_answered",
        why=f"owner answered {answer}" + (f": {reason}" if reason else ""),
        evidence=json.dumps({"answer": answer, "reason": reason or None,
                             "source": "runtime payload", "ledger_scope": scope},
                            ensure_ascii=False),
        undo="remove this answer",
    )
    return {"firm": slug, "notice_id": notice_id, "answer": answer,
            "ledger_scope": scope, "row": row.model_dump(),
            "ledger": _rows_as_dicts(ledger.rows(limit=10))}


def action_undo(payload: dict) -> dict:
    slug = _firm(payload)
    ts = _text(payload, "ts")
    reason = _text(payload, "reason")
    ledger, scope, _dir = _ledger_for(slug)
    row = ledger.undo(ts, reason)
    return {"firm": slug, "undone_ts": ts, "ledger_scope": scope,
            "row": row.model_dump(), "ledger": _rows_as_dicts(ledger.rows(limit=10))}


def action_ledger(payload: dict) -> dict:
    slug = _firm(payload)
    limit = payload.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("'limit' must be a positive integer")
    ledger, scope, _dir = _ledger_for(slug)
    rows = ledger.rows(limit=limit)
    return {"firm": slug, "ledger_scope": scope, "limit": limit,
            "rows": _rows_as_dicts(rows), "undone_ts": sorted(ledger.undone_ts()),
            "summary": ledger.summary(), "skipped": ledger.skipped}


def action_refusal(payload: dict) -> dict:
    from . import desk

    slug = _firm(payload)
    notice_id = payload.get("notice_id")
    if notice_id is not None and not isinstance(notice_id, str):
        raise ValueError("'notice_id' must be a string")
    ledger, scope, _dir = _ledger_for(slug)
    scene = desk.run_refusal_scene(slug, notice_id=notice_id or None,
                                   model=_text(payload, "model", required=False, default="sonnet") or "sonnet",
                                   ledger=ledger)
    return {"firm": slug, "ledger_scope": scope, "scene": scene}


def action_week(payload: dict) -> dict:
    """The background week: a replay of the committed snapshot (data/bench/week_<slug>.json), never computed here."""
    from . import week
    slug = _firm(payload)
    data = week.load(slug)
    if data is None:
        return {"firm": slug, "available": False,
                "reason": f"no committed week replay for {slug} (data/bench/week_{slug}.json missing)"}
    return {"firm": slug, "available": True, "as_of": data.get("as_of"), "window": data.get("window"),
            "days": data.get("days", []), "totals": data.get("totals"),
            "amendments": {k: v for k, v in (data.get("amendments") or {}).items() if k != "rows"},
            "invariants_ok": bool((data.get("invariants") or {}).get("all_ok")),
            "model_calls": data.get("model_calls", 0)}


def action_health(payload: dict) -> dict:
    scope, directory = ledger_location()
    until = sam_client.throttled_until()
    firms = {}
    for slug in sorted(profiles.PROFILES):
        snap = config.SNAPSHOT / f"{slug}.json"
        firms[slug] = {"snapshot": snap.exists(), "bench": _bench_path(slug).exists()}
    return {
        "service": "biddesk", "version": SERVICE_VERSION,
        "actions": list(ACTIONS),
        "ledger_scope": scope, "ledger_dir": str(directory),
        "sam_throttled": until is not None,
        "sam_throttled_until": until.isoformat() if until is not None else None,
        "firms": firms,
        "gallery": (config.GALLERY / "cases").exists(),
        "region": config.REGION,
        "models": {"primary": config.MODEL_PRIMARY, "fallback": config.MODEL_FALLBACK},
    }


HANDLERS = {
    "triage": action_triage,
    "desk": action_desk,
    "answer_card": action_answer_card,
    "undo": action_undo,
    "ledger": action_ledger,
    "refusal": action_refusal,
    "week": action_week,
    "health": action_health,
}


def handle(payload: Any) -> dict:
    """Route one payload. Never raises: every failure comes back as the error envelope."""
    action = "unknown"
    try:
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        raw_action = payload.get("action")
        if not isinstance(raw_action, str) or not raw_action:
            raise ValueError(f"payload needs an 'action'; one of: {', '.join(ACTIONS)}")
        action = raw_action
        handler = HANDLERS.get(action)
        if handler is None:
            raise ValueError(f"unknown action {action!r}; one of: {', '.join(ACTIONS)}")
        return _ok(action, **handler(payload))
    except BaseException as exc:  # noqa: BLE001 - the envelope is the contract
        return _err(action, exc)


@app.entrypoint
def invoke(payload, context=None):
    return handle(payload)


def main() -> None:
    app.run(port=int(os.environ.get("PORT", "8080")), host="0.0.0.0")


if __name__ == "__main__":
    main()
