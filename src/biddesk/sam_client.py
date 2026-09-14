"""SAM.gov Get Opportunities v2 client with a write-through cache.

Every response is written to data/raw/ before it is parsed, because the daily request
limit for a personal Public API Key is not published. Attachments are cached under
data/attachments/<notice_id>/. Nothing here calls a model.
Source: https://open.gsa.gov/api/get-opportunities-public-api/ (read 2026-09-13).
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import requests

from . import config
from .models import Notice

CENTRAL = ZoneInfo("America/Chicago")


def now_iso() -> str:
    return dt.datetime.now(CENTRAL).isoformat(timespec="seconds")


def today_central() -> dt.date:
    return dt.datetime.now(CENTRAL).date()


class SamError(RuntimeError):
    pass


class SamThrottled(SamError):
    """HTTP 429 with SAM's `nextAccessTime`; keyed calls are pointless until then."""
    def __init__(self, msg: str, until: dt.datetime | None):
        super().__init__(msg)
        self.until = until


THROTTLE_FILE = config.RAW / "throttle.json"
PUBLIC_NOTICE_URL = "https://sam.gov/api/prod/opps/v2/opportunities/{notice_id}"


def _parse_next_access(text: str) -> dt.datetime | None:
    m = re.search(r'"nextAccessTime"\s*:\s*"([^"]+)"', text)
    if not m:
        return None
    try:  # format seen live: 2026-Sep-15 00:00:00+0000 UTC
        return dt.datetime.strptime(m.group(1).replace(" UTC", ""), "%Y-%b-%d %H:%M:%S%z")
    except ValueError:
        return None


def throttled_until() -> dt.datetime | None:
    """When SAM last told us to stop, if that moment is still in the future."""
    if not THROTTLE_FILE.exists():
        return None
    try:
        until = dt.datetime.fromisoformat(json.loads(THROTTLE_FILE.read_text())["until"])
    except (ValueError, KeyError):
        return None
    return until if until > dt.datetime.now(dt.timezone.utc) else None


def _remember_throttle(until: dt.datetime | None, text: str) -> None:
    if until is None:
        until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    _cache_write(THROTTLE_FILE, {"until": until.isoformat(), "seen_at": now_iso(), "message": text[:300]})


def _cache_write(path: Path, payload: dict) -> None:
    """Write-through cache; a read-only package tree (the AgentCore Runtime mounts /var/task
    read-only) is not an error, the live answer is what matters."""
    try:
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    except OSError:
        pass


def _cache_path(kind: str, key: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return config.RAW / f"{kind}_{h}.json"


def _get_json(url: str, params: dict, kind: str, use_cache: bool = True, timeout: int = 240) -> dict:
    key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "api_key")
    cp = _cache_path(kind, key)
    if use_cache and cp.exists():
        return json.loads(cp.read_text(encoding="utf-8"))
    if config.OFFLINE:
        raise SamError(f"offline and no cache for {key}")
    if not config.SAM_API_KEY:
        raise SamError("SAM_API_KEY is not set in .env")
    until = throttled_until()
    if until is not None:
        raise SamThrottled(f"SAM.gov API key is throttled until {until.isoformat()} (cached 429); no call made", until)
    # SAM answers keyed calls in ~64 s; one retry covers a transient timeout or reset.
    last = None
    for attempt in range(2):
        try:
            r = requests.get(url, params={**params, "api_key": config.SAM_API_KEY}, timeout=timeout)
            break
        except requests.RequestException as e:
            last = e
    else:
        raise SamError(f"SAM.gov network error on {kind}: {type(last).__name__}: {last}")
    if r.status_code == 429:
        until = _parse_next_access(r.text)
        _remember_throttle(until, r.text)
        raise SamThrottled(f"SAM.gov rate limit hit (HTTP 429) on {kind}: {r.text[:300]}", until)
    if r.status_code != 200:
        raise SamError(f"SAM.gov HTTP {r.status_code} on {kind}: {r.text[:300]}")
    body = r.json()
    _cache_write(cp, {"_url": key, "_fetched_at": now_iso(), "_status": r.status_code, "body": body})
    return {"_url": key, "_fetched_at": now_iso(), "_status": r.status_code, "body": body}


def search(naics: str, days: int = 7, posted_to: dt.date | None = None, limit: int = 1000,
           use_cache: bool = True) -> tuple[list[dict], dict]:
    """One NAICS code, one date window, all notice types. Returns (raw opportunities, meta)."""
    to = posted_to or today_central()
    frm = to - dt.timedelta(days=days)
    params = {"postedFrom": frm.strftime("%m/%d/%Y"), "postedTo": to.strftime("%m/%d/%Y"),
              "ncode": naics, "limit": limit, "offset": 0}
    out, offset, total = [], 0, None
    fetched_at = None
    while True:
        params["offset"] = offset
        env = _get_json(config.SAM_SEARCH_URL, params, "search", use_cache=use_cache)
        body = env["body"]
        fetched_at = fetched_at or env["_fetched_at"]
        total = body.get("totalRecords", 0)
        page = body.get("opportunitiesData", []) or []
        out.extend(page)
        offset += len(page)
        if not page or offset >= total:
            break
    return out, {"naics": naics, "postedFrom": params["postedFrom"], "postedTo": params["postedTo"],
                 "totalRecords": total, "fetched_at": fetched_at}


def normalize(raw: dict, fetched_at: str) -> Notice:
    pop = raw.get("placeOfPerformance") or {}
    off = raw.get("officeAddress") or {}
    return Notice(
        notice_id=raw["noticeId"],
        title=(raw.get("title") or "").strip(),
        solicitation_number=raw.get("solicitationNumber"),
        type=raw.get("type") or raw.get("baseType") or "Unknown",
        base_type=raw.get("baseType"),
        naics=raw.get("naicsCode"),
        naics_all=raw.get("naicsCodes") or ([raw["naicsCode"]] if raw.get("naicsCode") else []),
        set_aside_code=raw.get("typeOfSetAside") or None,
        set_aside_desc=raw.get("typeOfSetAsideDescription") or None,
        posted=raw.get("postedDate", "")[:10],
        response_deadline=raw.get("responseDeadLine"),
        archive_date=raw.get("archiveDate"),
        agency_path=raw.get("fullParentPathName"),
        office_city=off.get("city"), office_state=off.get("state"),
        pop_city=(pop.get("city") or {}).get("name"),
        pop_state=(pop.get("state") or {}).get("code"),
        pop_zip=pop.get("zip"),
        pop_country=(pop.get("country") or {}).get("code"),
        description_url=raw.get("description"),
        resource_links=raw.get("resourceLinks") or [],
        ui_link=raw.get("uiLink") or f"https://sam.gov/opp/{raw['noticeId']}/view",
        active=(raw.get("active") == "Yes"),
        award=raw.get("award"),
        fetched_at=fetched_at,
    )


def fetch_notice_public(notice_id: str, use_cache: bool = True, timeout: int = 120) -> dict:
    """The public notice JSON the sam.gov web page itself loads (no API key, no quota seen).
    Carries `description[].body` (HTML), `parent.opportunityId` (previous version, for amendments),
    place of performance, deadlines and points of contact. Cached like every other response."""
    url = PUBLIC_NOTICE_URL.format(notice_id=notice_id)
    cp = _cache_path("pub", url)
    if use_cache and cp.exists():
        return json.loads(cp.read_text(encoding="utf-8"))
    if config.OFFLINE:
        raise SamError(f"offline and no cache for {url}")
    try:
        r = requests.get(url, params={"random": int(time.time())}, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0 (Biddesk research client)"})
    except requests.RequestException as e:
        raise SamError(f"SAM.gov network error on public notice {notice_id}: {type(e).__name__}: {e}")
    if r.status_code != 200:
        raise SamError(f"SAM.gov public notice HTTP {r.status_code} for {notice_id}: {r.text[:200]}")
    env = {"_url": url, "_fetched_at": now_iso(), "_status": 200, "body": r.json()}
    _cache_write(cp, env)
    return env


def html_to_text(html: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</tr>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text); text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#39;|&rsquo;|&lsquo;", "'", text)
    text = re.sub(r"&quot;|&ldquo;|&rdquo;", '"', text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_description(notice: Notice, use_cache: bool = True) -> str:
    """Plain-text description. Keyed API first (`description` URL); when the key is throttled,
    the public notice JSON (same text, no key). Returns '' only when the notice has no description."""
    if not notice.description_url:
        return ""
    try:
        env = _get_json(notice.description_url, {}, "desc", use_cache=use_cache)
        body = env["body"]
        html = body.get("description", "") if isinstance(body, dict) else str(body)
    except SamThrottled:
        pub = fetch_notice_public(notice.notice_id, use_cache=use_cache)["body"]
        html = "\n".join(d.get("body", "") for d in (pub.get("description") or []))
    return html_to_text(html)


def fetch_attachment(notice_id: str, url: str, use_cache: bool = True, timeout: int = 120) -> Path:
    """Download one resource link into data/attachments/<notice_id>/. Returns the local path.
    SAM resource files download without the API key; we append it only if the first try is refused."""
    d = config.ATTACH / notice_id
    d.mkdir(parents=True, exist_ok=True)
    fid = url.rstrip("/").split("/")[-2] if url.endswith("/download") else hashlib.sha1(url.encode()).hexdigest()[:12]
    existing = [p for p in d.glob(f"{fid}.*") if not p.name.endswith(".meta.json")]
    if use_cache and existing:
        return existing[0]
    if config.OFFLINE:
        raise SamError(f"offline and no cached attachment {fid} for {notice_id}")
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        raise SamError(f"attachment network error for {url}: {type(e).__name__}: {e}")
    if r.status_code in (401, 403) and config.SAM_API_KEY:
        r = requests.get(url, params={"api_key": config.SAM_API_KEY}, timeout=timeout, allow_redirects=True)
    if r.status_code != 200:
        raise SamError(f"attachment HTTP {r.status_code} for {url}")
    name = None
    cd = r.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if m:
        name = m.group(1)
    ext = Path(name).suffix.lower() if name else ""
    if not ext:
        ct = r.headers.get("content-type", "")
        ext = {"application/pdf": ".pdf",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
               "application/msword": ".doc",
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
               "application/vnd.ms-excel": ".xls"}.get(ct.split(";")[0].strip(), ".bin")
    p = d / f"{fid}{ext}"
    p.write_bytes(r.content)
    (d / f"{fid}.meta.json").write_text(json.dumps({
        "url": url, "original_name": name, "content_type": r.headers.get("content-type"),
        "bytes": len(r.content), "fetched_at": now_iso()}, indent=1), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- unkeyed sgs search
# The search endpoint the sam.gov web app itself calls. No API key, no quota seen, so it stays
# usable while the keyed Get Opportunities v2 key is throttled. Verified live 2026-09-13 (see
# data/SOURCES.md). It returns notice identity only: no NAICS list, no set-aside, no place of
# performance, no full description. Those come from fetch_notice_public() per notice.
PUBLIC_SEARCH_URL = "https://sam.gov/api/prod/sgs/v1/search/"
PUBLIC_RESOURCES_URL = "https://sam.gov/api/prod/opps/v3/opportunities/{notice_id}/resources"
RESOURCE_DOWNLOAD_URL = "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/{file_id}/download"
UA = {"User-Agent": "Mozilla/5.0 (Biddesk research client)"}
# SAM's own set-aside code -> description table is not exposed on the unkeyed routes. Every pair
# below was read out of this project's own keyed v2 responses (typeOfSetAside ->
# typeOfSetAsideDescription); a code that is not in there stays None rather than being invented.
SET_ASIDE_DESC: dict[str, str] = {
    "8A": "8(a) Set-Aside (FAR 19.8)",
    "8AN": "8(a) Sole Source (FAR 19.8)",
    "BICIV": "Buy Indian Set-Aside (specific to Department of Health and Human Services, Indian Health Services)",
    "EDWOSB": "Economically Disadvantaged Women-Owned Small Business",
    "HZC": "Historically Underutilized Business (HUBZone) Set-Aside (FAR 19.13)",
    "IEE": "Indian Economic Enterprise",
    "ISBEE": "Indian Small Business Economic Enterprise",
    "SBA": "Small Business Set Aside - Total",
    "SDVOSBC": "Service-Disabled Veteran-Owned Small Business (SDVOSB) Set-Aside (FAR 19.14)",
    "WOSB": "SBA Certified Women-Owned Small Business (WOSB) Program Set-Aside (FAR 19.15)",
    "WOSBSS": "SBA Certified Women-Owned Small Business (WOSB) Program Sole Source (FAR 19.15)",
}
UNRESTRICTED_SET_ASIDE = {"", "N/A", "NONE", "NULL"}


def _get_public_json(url: str, params: dict, kind: str, use_cache: bool = True, timeout: int = 180) -> dict:
    """Write-through cache for an unkeyed sam.gov route. Same envelope shape as _get_json."""
    key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    cp = _cache_path(kind, key)
    if use_cache and cp.exists():
        return json.loads(cp.read_text(encoding="utf-8"))
    if config.OFFLINE:
        raise SamError(f"offline and no cache for {key}")
    try:
        r = requests.get(url, params=params, timeout=timeout, headers=UA)
    except requests.RequestException as e:
        raise SamError(f"SAM.gov network error on {kind}: {type(e).__name__}: {e}")
    if r.status_code != 200:
        raise SamError(f"SAM.gov HTTP {r.status_code} on {kind}: {r.text[:300]}")
    env = {"_url": key, "_fetched_at": now_iso(), "_status": r.status_code, "body": r.json()}
    _cache_write(cp, env)
    return env


def in_window(publish_date: str, cutoff: str) -> bool:
    """Client-side date filter. The sgs route rejected every date parameter tried (HTTP 400), so
    the window is applied here on `publishDate` (UTC, so a boundary notice can land one day later
    than its Central date). `cutoff` is 'YYYY-MM-DD' and is inclusive."""
    return bool(publish_date) and publish_date[:10] >= cutoff


def search_public(naics_list: Iterable[str], days: int = 30, size: int = 100,
                  posted_to: dt.date | None = None, use_cache: bool = True,
                  notice_type: str | None = None) -> tuple[list[dict], dict]:
    """Unkeyed sam.gov search over a list of NAICS codes and a window of `days`.

    Pages to exhaustion (results come back sorted by -modifiedDate, which is always >=
    publishDate, so no early stop on publishDate would be lossless) and keeps the hits whose
    publishDate is on or after the cutoff. Returns (hits, meta); hits are the raw search records,
    deduped by `_id`.
    """
    to = posted_to or today_central()
    cutoff = (to - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    naics = ",".join(naics_list)
    seen: dict[str, dict] = {}
    fetched_at = None
    pages = 0
    total = 0
    page = 0
    while True:
        params = {"index": "opp", "q": "", "page": page, "size": size, "sort": "-modifiedDate",
                  "mode": "search", "is_active": "true", "naics": naics}
        if notice_type:
            params["notice_type"] = notice_type
        env = _get_public_json(PUBLIC_SEARCH_URL, params, "pubsearch", use_cache=use_cache)
        fetched_at = fetched_at or env["_fetched_at"]
        body = env["body"]
        results = ((body.get("_embedded") or {}).get("results") or [])
        pg = body.get("page") or {}
        total = pg.get("totalElements", 0)
        total_pages = pg.get("totalPages", 0)
        pages += 1
        for hit in results:
            if in_window(hit.get("publishDate", ""), cutoff) and hit.get("_id") not in seen:
                seen[hit["_id"]] = hit
        page += 1
        if not results or page >= total_pages:
            break
    return list(seen.values()), {
        "naics": naics, "days": days, "posted_from": cutoff, "posted_to": to.strftime("%Y-%m-%d"),
        "totalActiveRecords": total, "pages_fetched": pages, "kept_in_window": len(seen),
        "is_active": True, "fetched_at": fetched_at, "url": PUBLIC_SEARCH_URL}


def fetch_resource_links(notice_id: str, use_cache: bool = True) -> list[str]:
    """Attachment download URLs for one notice, from the unkeyed resources route the sam.gov page
    uses. Public, existing, undeleted files only, in SAM's own attachmentOrder."""
    env = _get_public_json(PUBLIC_RESOURCES_URL.format(notice_id=notice_id), {}, "pubres",
                           use_cache=use_cache)
    lists = ((env["body"].get("_embedded") or {}).get("opportunityAttachmentList") or [])
    rows = []
    for group in lists:
        for att in (group.get("attachments") or []):
            if att.get("type") != "file" or not att.get("resourceId"):
                continue
            if str(att.get("fileExists")) != "1" or str(att.get("deletedFlag")) not in ("0", "None"):
                continue
            if att.get("accessStatus") not in (None, "public"):
                continue
            rows.append((att.get("attachmentOrder") or 0, att["resourceId"]))
    rows.sort(key=lambda r: r[0])
    return [RESOURCE_DOWNLOAD_URL.format(file_id=rid) for _order, rid in rows]


def _public_naics(data2: dict) -> tuple[str | None, list[str]]:
    """data2.naics is [{"code": ["561730"], "type": "primary"}] - `code` is a list, not a string."""
    primary, all_codes = None, []
    for entry in (data2.get("naics") or []):
        codes = entry.get("code")
        codes = [codes] if isinstance(codes, str) else list(codes or [])
        for c in codes:
            if c and c not in all_codes:
                all_codes.append(c)
        if entry.get("type") == "primary" and codes and primary is None:
            primary = codes[0]
    return (primary or (all_codes[0] if all_codes else None)), all_codes


def _public_set_aside(data2: dict) -> str | None:
    """The code sits under data2.solicitation.setAside on solicitations and under data2.setAside
    on other notice types. 'N/A' means unrestricted, which a Notice records as None."""
    for raw in ((data2.get("solicitation") or {}).get("setAside"), data2.get("setAside")):
        code = (raw or "").strip()
        if code and code.upper() not in UNRESTRICTED_SET_ASIDE:
            return code
    return None


def normalize_public(pub_body: dict, hit: dict, fetched_at: str,
                     resource_links: list[str] | None = None) -> Notice:
    """Build a Notice from the public notice JSON (`fetch_notice_public(...)["body"]`) and its sgs
    search hit. The hit supplies the human-readable notice type and the agency path; the public
    body supplies NAICS, set-aside, place of performance, deadlines and the description. A field
    with no source on these routes stays None; no keyed-API URL is invented."""
    data2 = pub_body.get("data2") or {}
    pop = data2.get("placeOfPerformance") or {}
    notice_id = pub_body.get("opportunityId") or pub_body.get("id") or hit["_id"]
    naics, naics_all = _public_naics(data2)
    set_aside = _public_set_aside(data2)
    hit_type = hit.get("type") or {}
    deadlines = (data2.get("solicitation") or {}).get("deadlines") or {}
    desc_html = "\n".join(d.get("body") or "" for d in (pub_body.get("description") or []))
    org = hit.get("organizationHierarchy") or []
    agency_path = ".".join(o.get("name") for o in org if o.get("name")) or None
    office = next((o for o in reversed(org) if (o.get("address") or {}).get("city")), None)
    office_addr = (office or {}).get("address") or {}
    award = data2.get("award") or None
    return Notice(
        notice_id=notice_id,
        title=(data2.get("title") or hit.get("title") or "").strip(),
        solicitation_number=data2.get("solicitationNumber") or hit.get("solicitationNumber"),
        type=hit_type.get("value") or "Unknown",
        base_type=hit_type.get("value") or None,
        naics=naics,
        naics_all=naics_all,
        set_aside_code=set_aside,
        set_aside_desc=SET_ASIDE_DESC.get((set_aside or "").upper()) if set_aside else None,
        posted=(pub_body.get("postedDate") or hit.get("publishDate") or "")[:10],
        response_deadline=deadlines.get("response") or hit.get("responseDateActual") or hit.get("responseDate"),
        archive_date=(data2.get("archive") or {}).get("date"),
        agency_path=agency_path,
        office_city=office_addr.get("city") or None,
        office_state=office_addr.get("state") or None,
        pop_city=(pop.get("city") or {}).get("name") if pop else None,
        pop_state=(pop.get("state") or {}).get("code") if pop else None,
        pop_zip=(pop.get("zip") or None) if pop else None,
        pop_country=(pop.get("country") or {}).get("code") if pop else None,
        description_url=None,          # the keyed noticedesc URL was never fetched for these
        resource_links=resource_links if resource_links is not None else [],
        ui_link=f"https://sam.gov/opp/{notice_id}/view",
        active=bool(hit.get("isActive", True)),
        award=award if award else None,
        description_text=html_to_text(desc_html) if desc_html else None,
        fetched_at=fetched_at,
    )
