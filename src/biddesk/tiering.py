"""Tiered triage. Tier 0 is the owner's rules in code (zero model calls). Tier 1 is a cheap
scope check on Haiku (~50 output tokens). Tier 2 is the full reader graph (desk.py).

Every decision returns a Triage record with the tier that decided it, the rule or reason,
and the evidence string, so the ledger and the counter on the demo page are exact.
"""
from __future__ import annotations
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .models import FirmProfile, Notice, OwnerRule

CENTRAL = ZoneInfo("America/Chicago")
UNRESTRICTED = {"", "NONE", None}
MIN_TEXT_FOR_PP_RULE = 1500   # shorter notices keep the work in the attachments; the reader decides


@dataclass
class Triage:
    notice_id: str
    outcome: Literal["filed", "pass"]
    tier: int                       # 0, 1, or 2 (2 = passed to the full reader)
    rule_id: Optional[str] = None
    why: str = ""
    evidence: str = ""
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class TierCounts:
    total: int = 0
    tier0_filed: int = 0
    tier1_filed: int = 0
    tier2_sent: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)
    model_calls: int = 0

    def add(self, t: Triage) -> None:
        self.total += 1
        self.model_calls += t.model_calls
        if t.outcome == "filed" and t.tier == 0:
            self.tier0_filed += 1
            self.by_rule[t.rule_id or "?"] = self.by_rule.get(t.rule_id or "?", 0) + 1
        elif t.outcome == "filed" and t.tier == 1:
            self.tier1_filed += 1
            self.by_rule[t.rule_id or "scope"] = self.by_rule.get(t.rule_id or "scope", 0) + 1
        else:
            self.tier2_sent += 1

    def as_dict(self) -> dict:
        return {"total": self.total, "tier0_filed": self.tier0_filed, "tier1_filed": self.tier1_filed,
                "tier2_sent": self.tier2_sent, "by_rule": dict(self.by_rule), "model_calls": self.model_calls}


# ---------------------------------------------------------------- tier 0 helpers

def days_to_close(notice: Notice, now: dt.datetime | None = None) -> Optional[float]:
    if not notice.response_deadline:
        return None
    try:
        d = dt.datetime.fromisoformat(notice.response_deadline)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=CENTRAL)
    now = now or dt.datetime.now(CENTRAL)
    return (d - now).total_seconds() / 86400.0


_BASE_PATTERNS = [
    # explicit base period in months
    (re.compile(r"(\d{1,2})[- ]month\s+(?:base|initial)\s+period", re.I), lambda m: int(m.group(1))),
    (re.compile(r"(?:base|initial)\s+(?:period|term)\s+(?:of\s+)?(?:performance\s+)?(?:of\s+|is\s+|shall\s+be\s+)?(\d{1,2})\s+months?", re.I), lambda m: int(m.group(1))),
    (re.compile(r"(?:base|initial)\s+(?:period|term)\s+(?:of\s+)?(?:performance\s+)?(?:of\s+|is\s+|shall\s+be\s+)?(one|two|three|four|five|six|twelve)\s+(months?|years?)", re.I),
     lambda m: {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "twelve": 12}[m.group(1).lower()] * (12 if m.group(2).lower().startswith("year") else 1)),
    (re.compile(r"(?:one|1)[- ]year\s+base", re.I), lambda m: 12),
    (re.compile(r"base\s+year", re.I), lambda m: 12),
    (re.compile(r"(\d{1,2})[- ]month\s+(?:period\s+of\s+performance|pop\b)", re.I), lambda m: int(m.group(1))),
    (re.compile(r"period\s+of\s+performance\s+(?:is|of|shall\s+be|will\s+be)?\s*(?:approximately\s+)?(\d{1,2})\s+months?", re.I), lambda m: int(m.group(1))),
    (re.compile(r"period\s+of\s+performance\s+(?:is|of|shall\s+be|will\s+be)?\s*(?:approximately\s+)?(one|two|three|four|five|six)\s+(months?|years?)", re.I),
     lambda m: {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}[m.group(1).lower()] * (12 if m.group(2).lower().startswith("year") else 1)),
    (re.compile(r"(?:one|1)\s*(?:\(\s*1\s*\))?[- ]?year\s+base", re.I), lambda m: 12),
    # a period stated in days or weeks (only when the sentence names the period of performance)
    (re.compile(r"(?:period\s+of\s+performance|\bpop\b)(?![^.]{0,60}?\b(?:begin|commence|start|award)\b)[^.]{0,60}?\b(?:shall\s+be|is|no\s+later\s+than|not\s+to\s+exceed)\s+(\d{1,3})\s+(?:calendar\s+)?days\b", re.I),
     lambda m: max(0, round(int(m.group(1)) / 30.4))),
    (re.compile(r"(\d{1,3})[- ]week\s+period\s+of\s+performance", re.I), lambda m: round(int(m.group(1)) / 4.345)),
    (re.compile(r"one[- ]time\s+(?:purchase|service|event)|single\s+(?:delivery|event)", re.I), lambda m: 0),
]
_SHORTENED = re.compile(r"shortened|partial|prorated|pro-rated", re.I)


def base_period_months(text: str) -> tuple[Optional[int], str]:
    """Best-effort read of the base period from notice text. Returns (months, quote) or (None, '').
    A 'shortened/partial base year' is an unknown length, not 12 months, so it returns None."""
    if not text:
        return None, ""
    for pat, fn in _BASE_PATTERNS:
        m = pat.search(text)
        if m:
            s = max(0, m.start() - 60); e = min(len(text), m.end() + 60)
            quote = text[s:e].replace("\n", " ").strip()
            months = fn(m)
            if months == 12 and _SHORTENED.search(text[max(0, m.start() - 40):m.end()]):
                return None, ""
            return months, quote
    return None, ""


def keyword_hits(keywords: list[str], hay_lower: str) -> int:
    """Whole-word keyword hits (plural allowed). 'ATO' must not hit 'operator', 'RN' must not hit 'furnish'."""
    return sum(1 for k in keywords
               if re.search(r"(?<![a-z])" + re.escape(k.lower()) + r"(?:e?s)?(?![a-z])", hay_lower))


def past_performance_matches(profile: FirmProfile, notice: Notice, min_hits: int = 2) -> tuple[int, list[str]]:
    """Count past-performance records whose keywords hit the notice title + description at least min_hits times."""
    hay = f"{notice.title}\n{notice.description_text or ''}".lower()
    matched = []
    for pp in profile.past_performance:
        hits = keyword_hits(pp.keywords, hay)
        if hits >= min_hits:
            matched.append(f"{pp.customer}: {pp.title} ({hits} keyword hits)")
    return len(matched), matched


_AMENDMENT_BOILERPLATE = re.compile(
    r"purpose of this (amendment|modification)"
    r"|\b(amendment|modification)\s+\d+\s+is\s+issued\b"
    r"|\bis\s+issued\s+to\s+revise\b", re.I)
# "UPDATE 03/01/2026: the closing date is revised" lines are a changelog, not a statement of work
_CHANGELOG_LINE = re.compile(r"^\s*(?:UPDATE|AMENDMENT|REVISED)\b.*$", re.I | re.M)
# a notice that defers its scope to an attachment is not judged on its description (tier 1 reads it)
_SCOPE_ATTACHED = re.compile(r"attached\s+(?:statement\s+of\s+work|sow|pws|performance\s+work\s+statement)"
                             r"|(?:statement\s+of\s+work|sow|pws)\b[^.]{0,40}\battached", re.I)


def _distance(profile: FirmProfile, notice: Notice) -> tuple[Optional[float], str]:
    try:
        from . import geo
    except ImportError:
        return None, "geo module unavailable"
    return geo.distance_from(profile.lat, profile.lon, notice)


def apply_rule(rule: OwnerRule, profile: FirmProfile, notice: Notice, now: dt.datetime | None = None) -> Optional[Triage]:
    """Return a filed Triage if the rule kills this notice, else None. Unknown facts never fire a rule."""
    nid = notice.notice_id
    if rule.kind == "notice_types":
        if not notice.type:
            return None                      # absent type is an unknown fact
        if notice.type not in (rule.value or []):
            return Triage(nid, "filed", 0, rule.id, f"{notice.type} is not a solicitation", f"type={notice.type}")
    elif rule.kind == "set_aside_eligible":
        raw_code = (notice.set_aside_code or "").strip().upper()
        code = raw_code if raw_code not in UNRESTRICTED else ""
        allowed = {str(v).strip().upper() for v in (rule.value or [])} | {""}
        if code not in allowed:
            return Triage(nid, "filed", 0, rule.id, f"set-aside {notice.set_aside_desc or code} is one the firm cannot claim",
                          f"typeOfSetAside={notice.set_aside_code}")
    elif rule.kind == "max_distance_miles":
        country = (notice.pop_country or "").strip().upper()
        if country and country not in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
            where = ", ".join(x for x in (notice.pop_city, notice.pop_country) if x)
            return Triage(nid, "filed", 0, rule.id, f"place of performance is outside the United States ({where})",
                          f"placeOfPerformance.country={notice.pop_country}")
        miles, precision = _distance(profile, notice)
        # A state centroid can sit hundreds of miles from the real site, so it only files a notice
        # when the distance is beyond doubt (twice the owner's limit). Zip and city precision file at the limit.
        limit = float(rule.value) * (2.0 if precision == "state" else 1.0)
        if miles is not None and miles > limit:
            city = notice.pop_city if notice.pop_city and any(ch.isalpha() for ch in notice.pop_city) else ""
            where = ", ".join(x for x in (city, notice.pop_state) if x) or notice.pop_zip or "?"
            return Triage(nid, "filed", 0, rule.id, f"{where} is {miles:.0f} miles from {profile.city} (limit {rule.value:.0f})",
                          f"placeOfPerformance={where}; precision={precision}")
    elif rule.kind == "min_days_to_close":
        d = days_to_close(notice, now)
        if d is not None and d < float(rule.value):
            why = (f"closed on {notice.response_deadline[:10]}" if d < 0
                   else f"only {d:.1f} days to close (owner minimum {rule.value})")
            return Triage(nid, "filed", 0, rule.id, why, f"responseDeadLine={notice.response_deadline}")
    elif rule.kind == "min_base_months":
        months, quote = base_period_months(notice.description_text or "")
        if months is not None and months < int(rule.value):
            return Triage(nid, "filed", 0, rule.id, f"base period is {months} months (owner minimum {rule.value})", quote)
    elif rule.kind == "min_past_performance_matches":
        # The full rule (N matched records, cited) is a reading job for the fit scorer in tier 2.
        # Tier 0 only files the clear case: a long description with zero overlap with any record.
        text = f"{notice.title}\n{notice.description_text or ''}"
        scope_text = _CHANGELOG_LINE.sub("", text)
        prose = re.sub(r"[^A-Za-z ]+", "", scope_text)     # separators and amendment tables are not a statement of work
        if (len(prose) >= MIN_TEXT_FOR_PP_RULE and not _AMENDMENT_BOILERPLATE.search(text)
                and not _SCOPE_ATTACHED.search(text)):
            hay = text.lower()
            total = sum(keyword_hits(pp.keywords, hay) for pp in profile.past_performance)
            if total == 0:
                return Triage(nid, "filed", 0, rule.id, "no overlap with any past-performance record (owner requires "
                              f"{rule.value} matches)", f"{len(text)} chars of notice text, 0 keyword hits across "
                              f"{len(profile.past_performance)} records")
    return None


def tier0(profile: FirmProfile, notice: Notice, now: dt.datetime | None = None) -> Optional[Triage]:
    """Owner rules in order. First kill wins. No model call."""
    for rule in profile.rules:
        t = apply_rule(rule, profile, notice, now)
        if t:
            return t
    return None


# ---------------------------------------------------------------- tier 1 (Haiku scope check)

class ScopeCheck(BaseModel):
    in_scope: bool
    reason: str
    place_city: Optional[str] = None    # where the work is performed, if the text says (else null)
    place_state: Optional[str] = None   # two-letter US state/territory code, or null


TIER1_SYSTEM = ("You screen federal solicitations for a small contractor. Decide only two things: (1) is the work "
                "described the kind of work this firm does (be strict: adjacent industries, product purchases when the "
                "firm sells services, and research/R&D are out of scope); (2) where is the work performed, if the text "
                "says so (city and two-letter state code; null when not stated; foreign country -> state null, city = "
                "country name). Reason: one sentence under 25 words.")


def tier1_prompt(profile: FirmProfile, notice: Notice, max_chars: int = 900) -> str:
    desc = (notice.description_text or "")[:max_chars]
    past = "; ".join(pp.title for pp in profile.past_performance)
    return (f"FIRM: {profile.name}. Does: {profile.what}. NAICS {', '.join(profile.naics)}. Past contracts: {past}.\n"
            f"NOTICE: {notice.title}\nAGENCY: {notice.agency_path}\nNAICS: {notice.naics}\n"
            f"TEXT: {desc}\n\nIs this notice in scope for the firm, and where is the work performed?")


def _distance_rule_from_model_place(profile: FirmProfile, notice: Notice, sc: "ScopeCheck") -> Optional[Triage]:
    """When SAM left the place of performance blank, apply the owner's distance rule to the place the
    scope check read out of the text. Precision is labeled from geo and the rule needs a 2x margin."""
    rule = next((r for r in profile.rules if r.kind == "max_distance_miles"), None)
    if rule is None or not sc.place_state or notice.pop_state or notice.pop_zip:
        return None
    from . import geo
    found = geo.locate(sc.place_city, sc.place_state, None)
    if not found:
        return None
    miles = geo.haversine_miles(profile.lat, profile.lon, found[0], found[1])
    if miles > 2.0 * float(rule.value):
        where = ", ".join(x for x in (sc.place_city, sc.place_state) if x)
        return Triage(notice.notice_id, "filed", 1, rule.id,
                      f"{where} is about {miles:.0f} miles from {profile.city} (limit {rule.value:.0f}); place read from the notice text",
                      f"place per scope check={where}; geo precision={found[2]}; SAM placeOfPerformance blank")
    return None


def tier1(profile: FirmProfile, notice: Notice, model_id: str | None = None) -> Triage:
    """One structured Haiku call. Falls back to 'pass' on any model failure so the reader decides.
    A notice with no description text is not judged here at all: the work is in the attachments."""
    if not (notice.description_text or "").strip():
        return Triage(notice.notice_id, "pass", 2, None, "no description text in the notice; the attachments decide",
                      "description empty; scope check skipped", 0)
    from strands import Agent
    from strands.models import BedrockModel
    from . import config
    model = BedrockModel(model_id=model_id or config.MODEL_FALLBACK, region_name=config.REGION,
                         max_tokens=400, temperature=0.0)
    agent = Agent(model=model, system_prompt=TIER1_SYSTEM, callback_handler=None)
    try:
        res = agent(tier1_prompt(profile, notice), structured_output_model=ScopeCheck)
        sc: ScopeCheck = res.structured_output
        usage = res.metrics.accumulated_usage if hasattr(res, "metrics") else {}
        ti, to = int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0))
    except Exception as e:  # noqa: BLE001 - fallback must never lose a notice
        return Triage(notice.notice_id, "pass", 2, None, f"scope check failed ({type(e).__name__}); sent to reader", "", 1)
    ev = f"haiku scope check on title + {min(900, len(notice.description_text or ''))} chars of description"
    if not sc.in_scope:
        return Triage(notice.notice_id, "filed", 1, "scope", sc.reason, ev, 1, ti, to)
    far = _distance_rule_from_model_place(profile, notice, sc)
    if far:
        far.model_calls, far.tokens_in, far.tokens_out = 1, ti, to
        return far
    return Triage(notice.notice_id, "pass", 2, None, sc.reason, ev, 1, ti, to)


def triage_all(profile: FirmProfile, notices: list[Notice], use_model: bool = True,
               now: dt.datetime | None = None, tier1_fn=None) -> tuple[list[Triage], TierCounts]:
    counts = TierCounts()
    out: list[Triage] = []
    t1 = tier1_fn or tier1
    for n in notices:
        t = tier0(profile, n, now)
        if t is None:
            t = t1(profile, n) if use_model else Triage(n.notice_id, "pass", 2, None, "tier 1 skipped (no model)", "")
        out.append(t)
        counts.add(t)
    return out, counts


# ---------------------------------------------------------------- bench CLI

def run_bench(slugs: list[str] | None = None, use_model: bool = True, workers: int = 6, now: dt.datetime | None = None) -> dict:
    """Triage every snapshot notice for each firm; write data/bench/tiering_<slug>.json; return the counters.
    Tier 0 runs in this thread; tier 1 calls fan out (each is one Haiku call)."""
    import json
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import asdict
    from . import config, snapshot
    from .profiles import PROFILES
    out_dir = config.DATA / "bench"; out_dir.mkdir(exist_ok=True)
    report = {}
    for slug in slugs or list(PROFILES):
        prof = PROFILES[slug]
        meta, notices = snapshot.load(slug)
        as_of = now or dt.datetime.fromisoformat(meta["written_at"])
        t0 = {n.notice_id: tier0(prof, n, as_of) for n in notices}
        rest = [n for n in notices if t0[n.notice_id] is None]
        if use_model and rest:
            with ThreadPoolExecutor(workers) as ex:
                t1 = dict(zip([n.notice_id for n in rest], ex.map(lambda n: tier1(prof, n), rest)))
        else:
            t1 = {n.notice_id: Triage(n.notice_id, "pass", 2, None, "tier 1 skipped (no model)", "") for n in rest}
        counts = TierCounts(); rows = []
        for n in notices:
            t = t0[n.notice_id] or t1[n.notice_id]
            counts.add(t); rows.append({**asdict(t), "title": n.title, "type": n.type, "set_aside_code": n.set_aside_code})
        assert all(r["model_calls"] == 0 for r in rows if r["tier"] == 0), "a tier-0 row consumed a model call"
        assert counts.model_calls == sum(r["model_calls"] for r in rows)
        report[slug] = counts.as_dict()
        # a rules-only run never overwrites the model-enabled bench the gallery and README quote
        stem = f"tiering_{slug}" if use_model else f"rules_only_{slug}"
        (out_dir / f"{stem}.json").write_text(json.dumps(
            {"firm": slug, "as_of": as_of.isoformat(), "snapshot_written_at": meta["written_at"],
             "model": config.MODEL_FALLBACK if use_model else None,
             "counts": counts.as_dict(), "rows": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"{slug:15s} {counts.as_dict()}")
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Tiered triage bench over the committed snapshots")
    ap.add_argument("--profiles", nargs="*")
    ap.add_argument("--no-model", action="store_true")
    a = ap.parse_args()
    run_bench(a.profiles, use_model=not a.no_model)
