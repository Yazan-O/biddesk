/* Biddesk owner page. Vanilla JS, no build step.
   Every visible number comes from the JSON under BASE_PATH; only labels are hard-coded. */

/* ---- the two constants ---------------------------------------------------
   BASE_PATH is the repo root as seen from this page (web/ and gallery/ are
   siblings in the bucket). Every fetch path is derived from it.
   BIDDESK_API is the Lambda Function URL; empty means no live endpoint. */
const BASE_PATH = "../";
const BIDDESK_API = (typeof window.BIDDESK_API === "string" ? window.BIDDESK_API : "").trim();

const P = {
  index: () => BASE_PATH + "gallery/index.json",
  case: (slug, id) => BASE_PATH + "gallery/cases/" + slug + "/" + id + ".json",
  bench: (slug) => BASE_PATH + "data/bench/tiering_" + slug + ".json"
};

const VETO_MINUTES = 10;

/* short owner wording for each rule id; the firm's own sentence comes from the JSON */
const RULE_LABEL = {
  "rc-types": "not a real solicitation",
  "rc-setaside": "set-aside we cannot bid",
  "rc-distance": "too far from Oklahoma City",
  "rc-base": "base period under 12 months",
  "ss-types": "not a real solicitation",
  "ss-setaside": "set-aside we cannot bid",
  "ss-pp": "no past-performance match",
  "pm-types": "not a real solicitation",
  "pm-setaside": "set-aside we cannot bid",
  "pm-days": "closes too soon",
  "scope": "outside what the firm does"
};

const S = {
  index: null,
  cases: {},        // "slug/id" -> DeskResult
  bench: {},        // slug -> bench file or {error}
  localLedger: [],  // offline answers, never labelled live
  live: {}          // key -> live payload that replaced a cached panel
};

/* ---- tiny helpers -------------------------------------------------------- */
const $ = (sel, root) => (root || document).querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
};
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function dayOf(ts) {
  if (!ts) return "";
  const m = String(ts).match(/^(\d{4}-\d{2}-\d{2})/);
  return m ? m[1] : String(ts);
}
function stamp(ts) {
  if (!ts) return "";
  const m = String(ts).match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
  return m ? m[1] + " " + m[2] : String(ts);
}
function localStamp(ms) {
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
    " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}
function num(v, digits) {
  if (v === null || v === undefined || v === "") return "";
  const n = Number(v);
  return Number.isFinite(n) ? (digits === undefined ? String(n) : n.toFixed(digits)) : String(v);
}
/* display-only trim of the Bedrock routing prefix; the full id stays in the title attribute */
function shortModel(m) {
  return String(m || "").replace(/^(global|us|eu|apac)\.anthropic\./, "");
}

/* plain model name from the id in the data; unknown ids fall back to the id itself */
function modelName(m) {
  const id = String(m || "");
  if (!id) return "";
  if (/haiku-4-5/.test(id)) return "Claude Haiku 4.5";
  if (/sonnet-4-6/.test(id)) return "Claude Sonnet 4.6";
  if (/opus-4-\d/.test(id)) return "Claude Opus " + (id.match(/opus-4-(\d)/) || [])[1];
  return shortModel(id);
}
/* a Sources Sought notice takes a capability statement, not a proposal */
function isRfi(c) {
  const t = String((c && c.type) || "").toLowerCase();
  const k = String((c && c.response_kind) || "").toLowerCase();
  const hay = t + " " + k;
  return hay.indexOf("sources sought") >= 0 || hay.indexOf("rfi") >= 0 ||
    hay.indexOf("capability") >= 0;
}
const RFI_NOTE = "Sources Sought (RFI): a response is a capability statement, not a proposal";
/* the data says "bid"; on an RFI the honest English for it is "respond" */
function recWord(rec, rfi) {
  const r = String(rec || "");
  return rfi && r === "bid" ? "respond" : r;
}
function isClosed(c) { return !!(c && c.closed); }
function closedLine(c) {
  const d = c && c.days_to_close;
  if (d === undefined || d === null || !Number.isFinite(Number(d))) {
    return "closed before this build";
  }
  return "closed " + num(Math.abs(Number(d)), 1) + " days before this build";
}
function firstSentence(text, cap) {
  const t = String(text || "").replace(/\s+/g, " ").trim();
  if (!t) return "";
  const cut = t.search(/\.\s|\.$/);
  let out = cut > 20 ? t.slice(0, cut + 1) : t;
  const lim = cap || 120;
  if (out.length > lim) {
    out = out.slice(0, lim - 1).replace(/\s+\S*$/, "");
    /* do not end a trimmed title on a dangling function word */
    let prev = null;
    while (prev !== out) {
      prev = out;
      out = out.replace(/[\s,;:(\u2013\u2014-]+$/, "")
               .replace(/\s+(the|a|an|at|of|for|in|on|to|and|or|with|by|from)$/i, "");
    }
    out += "\u2026";
  }
  return out;
}

/* ---- ledger line: the one layout primitive ------------------------------- */
function ledgerLine(keyText, bodyNode, keyExtra) {
  const row = el("div", "ll");
  const k = el("div", "ll-key");
  k.appendChild(el("div", null, keyText));
  if (keyExtra) k.appendChild(el("div", "muted", keyExtra));
  const b = el("div", "ll-body");
  if (bodyNode) b.appendChild(bodyNode);
  row.appendChild(k);
  row.appendChild(b);
  return row;
}
function frag(html) {
  const d = document.createElement("div");
  d.innerHTML = html;
  return d;
}
function quote(text, sourceFile, page) {
  const q = el("blockquote");
  q.appendChild(document.createTextNode("\u201c" + String(text || "").trim() + "\u201d"));
  const c = el("cite");
  c.textContent = sourceFile ? sourceFile + (page || page === 0 ? " p" + page : "")
    : "source file not recorded in this snapshot";
  q.appendChild(c);
  return q;
}

/* ---- the honest banner --------------------------------------------------- */
function snapshotDate() {
  return S.index && S.index.built_at ? dayOf(S.index.built_at) : "the committed snapshot";
}
function showCached(reason, dateOverride) {
  const b = document.getElementById("banner");
  b.hidden = false;
  b.innerHTML = "";
  const w = el("div", "wrap");
  w.appendChild(el("b", null, "CACHED"));
  w.appendChild(el("span", null,
    "showing the cached snapshot from " + (dateOverride || snapshotDate()) + ": " + reason));
  b.appendChild(w);
}
function clearBanner() {
  const b = document.getElementById("banner");
  b.hidden = true;
  b.innerHTML = "";
}

/* ---- live calls ---------------------------------------------------------- */
/* Resolves with the live payload, or rejects with a plain-English reason.
   Nothing is ever labelled live unless it came back from here. */
async function callApi(action, payload) {
  if (!BIDDESK_API) {
    const e = new Error("no live endpoint configured");
    e.reason = "no live endpoint configured";
    throw e;
  }
  let res;
  try {
    res = await fetch(BIDDESK_API, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(Object.assign({ action: action }, payload || {}))
    });
  } catch (err) {
    const e = new Error("live call failed");
    e.reason = "the live endpoint did not answer (" + (err && err.message ? err.message : "network error") + ")";
    throw e;
  }
  if (!res.ok) {
    const e = new Error("http " + res.status);
    e.reason = "HTTP " + res.status + " from the live endpoint";
    throw e;
  }
  let body;
  try {
    body = await res.json();
  } catch (err) {
    const e = new Error("bad json");
    e.reason = "the live endpoint returned something that is not JSON";
    throw e;
  }
  if (body && body.cached) {
    const e = new Error("service fell back");
    e.reason = body.reason || "the service fell back to its snapshot";
    throw e;
  }
  return body;
}

async function getJSON(url) {
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) throw new Error("HTTP " + res.status + " for " + url);
  return res.json();
}

/* ---- routing ------------------------------------------------------------- */
function route() {
  const h = (location.hash || "#/").replace(/^#/, "");
  const parts = h.split("/").filter(Boolean);
  if (parts[0] === "bench") return { view: "bench" };
  if (parts[0] === "f" && parts[1] && parts[2] === "c" && parts[3]) {
    return { view: "case", slug: parts[1], id: parts[3] };
  }
  if (parts[0] === "f" && parts[1]) return { view: "firm", slug: parts[1] };
  return { view: "firms" };
}
function firmBySlug(slug) {
  return (S.index.firms || []).find((f) => f.profile && f.profile.slug === slug);
}
function caseInIndex(firm, id) {
  return (firm.cases || []).find((c) => c.notice_id === id);
}

function mount(nodes) {
  const main = document.getElementById("main");
  main.innerHTML = "";
  let i = 0;
  nodes.forEach((n) => {
    if (!n) return;
    n.classList.add("rise");
    n.style.animationDelay = Math.min(i * 45, 270) + "ms";
    i += 1;
    main.appendChild(n);
  });
  window.scrollTo(0, 0);
}

/* ---- view 1: pick a firm ------------------------------------------------- */
function viewFirms() {
  clearBanner();
  const head = el("section", "block");
  head.appendChild(frag(
    '<p class="eyebrow">Pick a firm</p>' +
    "<h1>One card per real candidate, not a feed of notices.</h1>" +
    '<p class="muted">Biddesk pulled SAM.gov for each firm, filed what the owner\u2019s own rules reject, ' +
    "and read the rest closely. Everything below is the committed snapshot from " +
    esc(snapshotDate()) + "." +
    "</p>"
  ));

  const list = el("section");
  (S.index.firms || []).forEach((f) => {
    const p = f.profile || {};
    const t = f.tier_counts || {};
    const a = el("a", "firm-row");
    a.href = "#/f/" + p.slug;
    a.appendChild(el("h2", null, p.name || p.slug));
    a.appendChild(el("p", "fict", "Fictional firm. Real SAM.gov notices."));
    a.appendChild(el("p", "firm-where", [p.city, p.state].filter(Boolean).join(", ")));
    a.appendChild(el("p", null, p.what || ""));
    a.appendChild(sieve(t, false));
    list.appendChild(a);
  });

  mount([head, list]);
  animateSieves();
}

/* ---- the signature element: the tier sieve ------------------------------- */
function sieve(counts, withLegend) {
  const t0 = Number(counts.tier0_filed || 0);
  const t1 = Number(counts.tier1_filed || 0);
  const t2 = Number(counts.tier2_sent || 0);
  const total = Number(counts.total || t0 + t1 + t2) || 1;

  const wrap = el("div", "sieve");
  const bar = el("div", "sieve-bar");
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label",
    total + " notices pulled: " + t0 + " filed by the rules, " + t1 +
    " filed by a cheap model, " + t2 + " read by the full desk.");

  const segs = [
    [t0, "filed by the rules", "seg-0"],
    [t1, "filed by a cheap model", "seg-1"],
    [t2, "read by the full desk", "seg-2"]
  ];
  segs.forEach(([n, label, cls], i) => {
    const s = el("div", "seg " + cls);
    s.appendChild(el("div", "seg-n", n));
    s.appendChild(el("div", "seg-l", label));
    s.dataset.pct = String((n / total) * 100);
    s.style.transitionDelay = i * 90 + "ms";
    if (n === 0) s.classList.add("seg-zero");
    bar.appendChild(s);
  });
  wrap.appendChild(bar);

  const lg = el("div", "sieve-legend");
  segs.forEach(([n, label, cls]) => {
    const s = el("span", "lg-seg");
    s.appendChild(el("i", "sw " + cls));
    s.appendChild(el("b", null, n));
    s.appendChild(document.createTextNode(" " + label));
    lg.appendChild(s);
  });
  if (withLegend) {
    lg.appendChild(frag("<span><b>" + total + "</b> pulled from SAM.gov</span>").firstChild);
    lg.appendChild(frag("<span><b>" + num(counts.model_calls) + "</b> model calls to get here</span>").firstChild);
  }
  wrap.appendChild(lg);
  return wrap;
}
function animateSieves() {
  requestAnimationFrame(() => {
    document.querySelectorAll(".seg").forEach((s) => {
      s.style.width = (Number(s.dataset.pct) || 0).toFixed(3) + "%";
    });
  });
}

/* ---- view 2: one firm ---------------------------------------------------- */
function viewFirm(slug) {
  const f = firmBySlug(slug);
  if (!f) return notFound("No firm with the id " + slug + " in the snapshot.");
  const p = f.profile || {};
  const t = f.tier_counts || {};
  clearBanner();

  const head = el("section", "block");
  head.appendChild(frag(
    '<p class="eyebrow"><a href="#/">All firms</a></p>' +
    "<h1>" + esc(p.name || slug) + "</h1>" +
    '<p class="fict">Fictional firm. Real SAM.gov notices.</p>' +
    '<p class="firm-where">' + esc([p.city, p.state].filter(Boolean).join(", ")) + "</p>" +
    "<p>" + esc(p.what || "") + "</p>"
  ));

  /* sieve + live pull */
  const sec = el("section", "block");
  sec.appendChild(el("p", "eyebrow", "Overnight pull"));
  const sv = sieve(t, true);
  sec.appendChild(sv);
  const row = el("div", "btn-row");
  const pull = el("button", "btn btn-go", "Run the pull live");
  pull.addEventListener("click", () => runLivePull(slug, sec, pull, (live) => {
    /* the rules block below must describe the same pull as the counts above */
    const fresh = rulesBlock(live, p, "counts from the live pull" +
      (live.days ? ", last " + live.days + " days" : ""));
    rules.replaceWith(fresh);
    rules = fresh;
  }));
  row.appendChild(pull);
  sec.appendChild(row);
  const note = el("p", "muted mono");
  note.textContent = "counts above are the snapshot from " + snapshotDate();
  sec.appendChild(note);

  /* rules in plain English */
  let rules = rulesBlock(t, p, null);

  /* candidates */
  const cand = el("section", "block");
  const allCases = f.cases || [];
  const openCases = allCases.filter((c) => !isClosed(c));
  const closedCases = allCases.filter(isClosed);
  cand.appendChild(frag(
    '<div class="block-head"><h2>What reached the desk</h2>' +
    "<p class=\"muted\">" + allCases.length + " of " + num(t.tier2_sent) +
    " sent to the full desk are in this snapshot" +
    (allCases.length
      ? " \u00b7 " + openCases.length + " open, " + closedCases.length + " closed" : "") +
    "</p></div>"
  ));
  if (!allCases.length) {
    cand.appendChild(el("p", "muted",
      "Nothing reached the desk for this firm: every notice was filed by the rules."));
  }
  const caseRow = (c) => {
    const card = c.card || {};
    const rfi = isRfi(c);
    const closed = isClosed(c);
    const body = el("div");
    const a = el("a", null, firstSentence(card.situation, 150));
    a.href = "#/f/" + slug + "/c/" + c.notice_id;
    const h = el("h3");
    h.appendChild(a);
    body.appendChild(h);
    /* the row says what kind of notice it is; the full sentence lives on the case view */
    if (rfi) body.appendChild(el("p", "note-rfi", "Sources Sought (RFI)"));
    const meta = el("p", "muted");
    meta.textContent = [
      card.recommendation ? "recommendation: " + recWord(card.recommendation, rfi) : "",
      card.matrix_summary || c.matrix_summary || "",
      card.fit_score !== undefined && card.fit_score !== null
        ? "fit " + num(card.fit_score, 2) + (c.matrix_kept ? " over " + c.matrix_kept + " validated rows" : "") : "",
      !closed && c.days_to_close !== undefined && c.days_to_close !== null
        ? num(c.days_to_close, 1) + " days to close" : ""
    ].filter(Boolean).join(" \u00b7 ");
    body.appendChild(meta);
    if (c.evidence_basis) body.appendChild(el("p", "mono muted", c.evidence_basis));
    const keyExtra = closed ? closedLine(c)
      : (card.decide_by ? "decide by " + dayOf(card.decide_by) : "");
    return ledgerLine(c.notice_id, body, keyExtra);
  };
  openCases.forEach((c) => cand.appendChild(caseRow(c)));
  if (closedCases.length) {
    cand.appendChild(el("p", "sub-quiet",
      "already closed when the desk read them (" + closedCases.length + ")"));
    closedCases.forEach((c) => cand.appendChild(caseRow(c)));
  }

  const nodes = [head, sec, rules, cand, weekBlock(slug), ledgerBlock(f)];
  if (slug === "plains-med" || Number(t.tier2_sent || 0) === 0) nodes.push(refusalBlock(f));
  mount(nodes.filter(Boolean));
  animateSieves();
  if (slug === "plains-med" || Number(t.tier2_sent || 0) === 0) loadRefusal(f);
}

/* "Why N were filed without a model": the tier-0 rule counts of one pull (snapshot or live). */
function rulesBlock(t, p, sourceLine) {
  const counts = normCounts(t.tier_counts || t.counts || t.tiers || t);
  const rules = el("section", "block");
  rules.appendChild(frag(
    '<div class="block-head"><h2>Why ' + (Number(counts.tier0_filed || 0)) +
    " were filed without a model</h2></div>" +
    '<p class="muted">The owner wrote these rules. The desk applies them before it spends a token.</p>'
  ));
  if (sourceLine) rules.appendChild(el("p", "muted mono", sourceLine));
  const byRule = t.by_rule || {};
  const ruleText = {};
  (p.rules || []).forEach((r) => { ruleText[r.id] = r.text; });
  const ruleIds = Object.keys(byRule).sort((a, b) => byRule[b] - byRule[a]);
  if (!ruleIds.length) {
    rules.appendChild(el("p", "muted", "No rule breakdown in this pull."));
  }
  ruleIds.forEach((id) => {
    const body = el("div");
    body.appendChild(el("h3", null, RULE_LABEL[id] || id));
    body.appendChild(el("p", null, ruleText[id] || "Rule text not in the snapshot for " + id + "."));
    rules.appendChild(ledgerLine(byRule[id] + " filed", body, id));
  });
  return rules;
}

async function runLivePull(slug, sec, btn, onLive) {
  btn.disabled = true;
  btn.textContent = "Pulling SAM.gov\u2026";
  try {
    const live = await callApi("triage", { firm: slug });
    const counts = normCounts(live.tier_counts || live.counts || live.tiers);
    if (!counts) throw Object.assign(new Error("shape"), { reason: "the live answer carried no tier counts" });
    const old = sec.querySelector(".sieve");
    const fresh = sieve(counts, true);
    old.replaceWith(fresh);
    animateSieves();   /* a fresh sieve starts at width 0; grow it like the first render */
    const note = sec.querySelector(".mono");
    const routeText = live.route === "public" ? " via the public sam.gov route (API key throttled)"
      : live.route === "keyed" ? " via the keyed SAM.gov API" : "";
    note.textContent = "live pull at " + (live.produced_at ? stamp(live.produced_at) : localStamp(Date.now()))
      + routeText + (live.days ? ", last " + live.days + " days" : "");
    clearBanner();
    btn.textContent = "Live counts shown";
    if (onLive) onLive(live);
  } catch (err) {
    showCached(err.reason || "the live call failed");
    btn.disabled = false;
    btn.textContent = "Run the pull live";
  }
}

/* ---- background week ----------------------------------------------------- */
function weekBlock(slug) {
  const sec = el("section", "block");
  sec.appendChild(frag('<div class="block-head"><h2>The background week</h2></div>'));
  const week = S.index.week || (firmBySlug(slug) || {}).week;
  if (!week || !week.length) {
    sec.appendChild(el("p", "muted",
      "The week view arrives with the background run. This snapshot holds a single overnight pull, so there are no days to show."));
    return sec;
  }
  const strip = el("div", "week");
  week.forEach((d) => {
    const box = el("div", "day");
    box.appendChild(el("b", null, dayOf(d.date || d.day)));
    box.appendChild(el("div", "mono", num(d.filed) + " filed"));
    box.appendChild(el("div", "mono", num(d.surfaced) + " surfaced"));
    if (d.amendment) box.appendChild(el("div", "mono", "deadline moved"));
    strip.appendChild(box);
  });
  sec.appendChild(strip);
  return sec;
}

/* ---- firm ledger summary ------------------------------------------------- */
function ledgerBlock(f) {
  const ls = f.ledger_summary || {};
  const sec = el("section", "block");
  sec.appendChild(frag('<div class="block-head"><h2>Ledger</h2>' +
    '<p class="muted">every action the desk took, with an undo</p></div>'));
  const body = el("div");
  const byAction = ls.by_action || {};
  const keys = Object.keys(byAction);
  if (!keys.length) {
    body.appendChild(el("p", "muted", "No ledger rows for this firm in the snapshot."));
  } else {
    const ul = el("ul", "tight");
    keys.forEach((k) => ul.appendChild(el("li", null, byAction[k] + " \u00d7 " + k.replace(/_/g, " "))));
    body.appendChild(ul);
  }
  const n = Number(ls.total || 0);
  sec.appendChild(ledgerLine(n + (n === 1 ? " row" : " rows"), body, "recorded " + snapshotDate()));
  const mine = S.localLedger.filter((x) => x.firm_slug === f.profile.slug);
  if (mine.length) sec.appendChild(localLedgerView(mine));
  return sec;
}
function localLedgerView(rows) {
  const list = Array.isArray(rows) ? rows : S.localLedger;
  const wrap = el("div");
  wrap.appendChild(el("p", "eyebrow", "This browser only \u00b7 not sent, no live endpoint"));
  list.slice().reverse().forEach((r) => {
    const body = el("div");
    body.appendChild(el("p", null, r.why));
    body.appendChild(el("p", "muted", "not sent, no live endpoint"));
    const btns = el("div", "btn-row");
    const undo = el("button", "btn", "Undo filing");
    const age = (Date.now() - r.at) / 60000;
    if (age > VETO_MINUTES) {
      undo.disabled = true;
      undo.textContent = "Veto window closed";
    }
    undo.addEventListener("click", () => {
      S.localLedger = S.localLedger.filter((x) => x !== r);
      rerender();
    });
    btns.appendChild(undo);
    body.appendChild(btns);
    wrap.appendChild(ledgerLine(localStamp(r.at), body, r.notice_id));
  });
  return wrap;
}

/* ---- the refusal scene --------------------------------------------------- */
function refusalBlock(f) {
  const sec = el("section", "block");
  sec.id = "refusal";
  sec.appendChild(frag('<div class="block-head"><h2>What the desk refuses to do</h2></div>' +
    '<p class="muted">Every notice below was filed against an owner rule. Ask the desk to submit one anyway.</p>'));
  const loading = el("p", "muted", "Loading the filed notices\u2026");
  loading.id = "refusal-loading";
  sec.appendChild(loading);
  return sec;
}
/* days to close, derived only when the bench row carries a deadline */
function daysToClose(evidence, asOf) {
  const m = /responseDeadLine=(\S+)/.exec(String(evidence || ""));
  if (!m || !asOf) return "";
  const dl = new Date(m[1]).getTime();
  const now = new Date(asOf).getTime();
  if (!isFinite(dl) || !isFinite(now)) return "";
  const d = (dl - now) / 86400000;
  if (d < 0) return "closed " + num(-d, 1) + " days before the pull";
  return num(d, 1) + " days to close";
}
async function loadRefusal(f) {
  const sec = document.getElementById("refusal");
  if (!sec) return;
  const slug = f.profile.slug;
  let bench = S.bench[slug];
  if (!bench) {
    try {
      bench = await getJSON(P.bench(slug));
    } catch (err) {
      bench = { error: err.message };
    }
    S.bench[slug] = bench;
  }
  const loading = document.getElementById("refusal-loading");
  if (loading) loading.remove();

  if (bench.error || !bench.rows) {
    sec.appendChild(el("p", "muted",
      "The filed-notice detail lives in data/bench/tiering_" + slug +
      ".json, which this deploy does not serve (" + bench.error + ")."));
  } else {
    const rows = (bench.rows || []).filter((r) => r.outcome === "filed");
    const asOf = dayOf(bench.as_of || bench.snapshot_written_at);
    rows.forEach((r) => {
      const body = el("div");
      body.appendChild(el("h3", null, r.title || r.notice_id));
      body.appendChild(el("p", null,
        (RULE_LABEL[r.rule_id] || r.rule_id) + ": " + (r.why || "")));
      const ev = el("p", "mono muted");
      ev.textContent = [r.type, daysToClose(r.evidence, bench.as_of), r.evidence]
        .filter(Boolean).join(" \u00b7 ");
      body.appendChild(ev);
      sec.appendChild(ledgerLine(r.notice_id, body, "filed " + asOf));
    });
    if (!rows.length) sec.appendChild(el("p", "muted", "No filed rows in the bench file."));
  }

  const row = el("div", "btn-row");
  const ask = el("button", "btn btn-go", "Ask the desk to submit this offer");
  const out = el("div");
  ask.addEventListener("click", async () => {
    ask.disabled = true;
    const bench2 = S.bench[slug] || {};
    const first = (bench2.rows || []).find((r) => r.outcome === "filed");
    out.innerHTML = "";
    try {
      const live = await callApi("refusal", { firm: slug, notice_id: first ? first.notice_id : null });
      /* the runtime answers {scene: {denied_row, primary_text, title, model_used, model_calls}} */
      const scene = live.scene || {};
      const denied = scene.denied_row || live.ledger_row || null;
      const body = el("div");
      if (scene.title) body.appendChild(el("h3", null, scene.title));
      if (denied && denied.why) {
        body.appendChild(el("p", "mono muted", "guard hook \u00b7 " + denied.why));
      }
      const said = (scene.primary_text || live.reply || live.message || "").trim();
      body.appendChild(el("p", null, said
        ? said.replace(/\*\*/g, "").split(/\n\s*\n/)[0].slice(0, 600)
        : "The desk declined."));
      const meta = [];
      if (scene.model_used) meta.push("said by " + modelName(scene.model_used));
      if (scene.model_calls) meta.push(scene.model_calls + " model call" + (scene.model_calls === 1 ? "" : "s"));
      meta.push("nothing was sent");
      body.appendChild(el("p", "mono muted", meta.join(" \u00b7 ")));
      out.appendChild(ledgerLine("live \u00b7 " + (live.produced_at ? stamp(live.produced_at) : localStamp(Date.now())),
        body, denied ? (denied.action || "denied_tool") : "no denial recorded"));
      clearBanner();
    } catch (err) {
      showCached(err.reason || "the live call failed");
      out.appendChild(recordedRefusal(f));
    }
    ask.disabled = false;
  });
  row.appendChild(ask);
  sec.appendChild(row);
  sec.appendChild(out);
}
function recordedRefusal(f) {
  const ls = f.ledger_summary || {};
  const denied = (ls.by_action || {}).denied_tool;
  const body = el("div");
  if (denied) {
    body.appendChild(el("p", null,
      "The desk refused to submit an offer " + denied +
      (denied === 1 ? " time" : " times") +
      " in the recorded run: the submit tool is denied by a guard hook, so nothing was ever sent."));
    body.appendChild(el("p", "muted",
      "The snapshot carries the count, not the row text, so the desk\u2019s own words are not shown here."));
  } else {
    body.appendChild(el("p", "muted", "No denied_tool row in this snapshot."));
  }
  return ledgerLine("recorded " + snapshotDate(), body, "denied_tool");
}

/* ---- view 3: one case ---------------------------------------------------- */
async function viewCase(slug, id) {
  const f = firmBySlug(slug);
  if (!f) return notFound("No firm with the id " + slug + " in the snapshot.");
  const key = slug + "/" + id;
  let d = S.cases[key];
  if (!d) {
    mount([el("p", "loading", "Loading the card\u2026")]);
    try {
      d = await getJSON(P.case(slug, id));
      S.cases[key] = d;
    } catch (err) {
      return notFound("That case file is not in this deploy (" + err.message + ").");
    }
  }
  clearBanner();
  const card = d.card || {};
  const idxCase = caseInIndex(f, id) || {};
  const rfi = isRfi({
    type: d.type || idxCase.type,
    response_kind: d.response_kind || idxCase.response_kind
  });
  const dtc = (d.deadlines && d.deadlines.days_to_close !== undefined && d.deadlines.days_to_close !== null)
    ? d.deadlines.days_to_close
    : (idxCase.days_to_close !== undefined && idxCase.days_to_close !== null ? idxCase.days_to_close : null);
  const closed = (d.closed !== undefined && d.closed !== null) ? !!d.closed : !!idxCase.closed;
  const closeInfo = { closed: closed, days_to_close: dtc };

  const head = el("section", "block");
  head.appendChild(frag(
    '<p class="eyebrow"><a href="#/f/' + esc(slug) + '">' + esc(f.profile.name) + "</a></p>" +
    '<div class="card-head"><span class="verdict">' +
    esc(recWord(card.recommendation, rfi) || "no recommendation") +
    '</span><span class="mono muted">' + esc(id) + "</span></div>" +
    '<p class="fict">Fictional firm. Real SAM.gov notices.</p>' +
    "<h1>" + esc(firstSentence(card.situation, 110)) + "</h1>" +
    (rfi ? '<p class="note-rfi">' + esc(RFI_NOTE) + "</p>" : "") +
    (closed ? '<p class="note-closed">' + esc(closedLine(closeInfo)) + "</p>" : "")
  ));

  /* decision card */
  const dc = el("section", "block");
  const modelId = d.model_used || idxCase.model_used || "";
  const fellBack = (d.fallback_used !== undefined && d.fallback_used !== null)
    ? !!d.fallback_used : !!idxCase.fallback_used;
  const forced = (d.forced_model !== undefined && d.forced_model !== null)
    ? !!d.forced_model : !!idxCase.forced_model;
  dc.appendChild(frag('<div class="block-head"><h2>Decision Card</h2>' +
    '<p class="muted" title="' + esc(modelId) + '">read by ' +
    esc(modelId ? modelName(modelId) : "the desk, model id not recorded in this snapshot") +
    (fellBack ? " (fallback)"
      : (forced ? " (this model on purpose, not a fallback)" : "")) +
    "</p></div>"));
  dc.appendChild(ledgerLine("situation", el("p", null, card.situation || ""),
    closed ? closedLine(closeInfo)
      : (card.decide_by ? "decide by " + dayOf(card.decide_by) : "")));

  const reasons = el("div", "two");
  [[rfi ? "Reasons to respond" : "Reasons to bid", card.reasons_for],
   ["Reasons not to", card.reasons_against]].forEach(([t, arr]) => {
    const c = el("div");
    c.appendChild(el("h3", null, t));
    const ul = el("ul", "tight");
    (arr || []).forEach((r) => ul.appendChild(el("li", null, r)));
    if (!(arr || []).length) ul.appendChild(el("li", "muted", "none recorded"));
    c.appendChild(ul);
    reasons.appendChild(c);
  });
  dc.appendChild(ledgerLine("reasons", reasons));

  const fitBody = el("div");
  fitBody.appendChild(el("p", null, card.matrix_summary || idxCase.matrix_summary || ""));
  const fitBits = [];
  const keptRows = Array.isArray(d.matrix) ? d.matrix.length : idxCase.matrix_kept;
  if (card.fit_score !== undefined && card.fit_score !== null) {
    fitBits.push("fit " + num(card.fit_score, 2) + (keptRows ? " over " + keptRows + " validated rows" : ""));
  }
  if (d.fit_score_code !== undefined && d.fit_score_code !== null) {
    fitBits.push("code-scored fit " + num(d.fit_score_code, 2));
  }
  if (fitBits.length) fitBody.appendChild(el("p", "mono muted", fitBits.join(" \u00b7 ")));
  /* meets=1, partial=0.5, gap=0 over the rows that survived validation in this run; the row set varies run to run */
  if (keptRows) {
    fitBody.appendChild(el("p", "muted", "scored in code over the " + keptRows + " rows that survived validation in this run; not comparable across runs"));
  }
  /* a closed notice says so once under the title and once on the situation line; not a third time */
  if (!closed && dtc !== null) {
    fitBody.appendChild(el("p", "mono", num(dtc, 1) + " days to close"));
  }
  if (d.evidence_basis || idxCase.evidence_basis) {
    fitBody.appendChild(el("p", "muted", "evidence: " + (d.evidence_basis || idxCase.evidence_basis)));
  }
  if (d.deadlines && d.deadlines.amendment_note) {
    fitBody.appendChild(el("p", "muted", d.deadlines.amendment_note));
  }
  dc.appendChild(ledgerLine("fit", fitBody));

  const kd = (card.key_dates) || (d.deadlines && d.deadlines.key_dates) || {};
  const kdBody = el("div");
  const kdUl = el("ul", "tight");
  [["questions due", kd.questions_due],
   [rfi ? "response due" : "proposal due", kd.proposal_due],
   ["site visit", kd.site_visit], ["period of performance", kd.period_of_performance]]
    .forEach(([k, v]) => { if (v) kdUl.appendChild(el("li", null, k + ": " + v)); });
  if (!kdUl.children.length) kdUl.appendChild(el("li", "muted", "no dates recorded"));
  kdBody.appendChild(kdUl);
  (kd.source_quotes || []).forEach((q) => {
    kdBody.appendChild(quote(q.quote || q.text || q, q.source_file, q.page));
  });
  dc.appendChild(ledgerLine("key dates", kdBody));

  if (card.default) {
    const dflt = el("p", "default-line", "If nobody answers: " + card.default);
    dc.appendChild(ledgerLine("the default", dflt));
  }
  if (card.evidence_link) {
    const lnk = el("p");
    const a = el("a", null, "Open this notice on sam.gov");
    a.href = card.evidence_link;
    a.rel = "noopener";
    a.target = "_blank";
    lnk.appendChild(a);
    dc.appendChild(ledgerLine("the notice", lnk));
  }

  /* answer buttons */
  const ans = el("div", "btn-row");
  const out = el("div");
  ["bid", "no-bid"].forEach((a) => {
    const b = el("button", "btn" + (a === "bid" ? " btn-go" : ""),
      a === "bid" ? (rfi ? "Respond to this RFI" : "Bid on this") : "File as no-bid");
    if (closed) {
      b.disabled = true;
      b.setAttribute("aria-disabled", "true");
    } else {
      b.addEventListener("click", () => answerCard(slug, id, a, out));
    }
    ans.appendChild(b);
  });
  dc.appendChild(ans);
  if (closed) {
    dc.appendChild(el("p", "why-disabled",
      "Answers are off: this notice " + closedLine(closeInfo) + "."));
  }
  dc.appendChild(out);

  mount([head, dc, matrixBlock(d, idxCase), draftsBlock(d), caseLedgerBlock(idxCase, d)]);
}

async function answerCard(slug, id, answer, out) {
  out.innerHTML = "";
  try {
    const live = await callApi("answer_card", { firm: slug, notice_id: id, answer: answer });
    clearBanner();
    const rows = live.ledger || live.rows || (live.row ? [live.row] : []);
    const box = el("div");
    box.appendChild(el("p", "eyebrow", "Live ledger"));
    rows.forEach((r) => box.appendChild(liveLedgerRow(slug, r)));
    out.appendChild(box);
  } catch (err) {
    showCached(err.reason || "the live call failed");
    S.localLedger.push({
      at: Date.now(),
      notice_id: id,
      firm_slug: slug,
      action: "card_answered",
      why: "answered " + answer + " \u2014 held in this browser"
    });
    out.appendChild(localLedgerView(S.localLedger.filter((x) => x.notice_id === id)));
  }
}
function liveLedgerRow(slug, r) {
  const body = el("div");
  body.appendChild(el("p", null, r.why || r.action || ""));
  if (r.undo) {
    const btns = el("div", "btn-row");
    const u = el("button", "btn", "Undo filing");
    u.addEventListener("click", async () => {
      u.disabled = true;
      try {
        await callApi("undo", { firm: slug, ts: r.ts, reason: "owner changed their mind" });
        body.appendChild(el("p", "muted", "undone: " + r.undo));
      } catch (err) {
        showCached(err.reason || "the live call failed");
        u.disabled = false;
      }
    });
    btns.appendChild(u);
    body.appendChild(btns);
  }
  return ledgerLine(stamp(r.ts), body, r.action);
}

/* ---- compliance matrix --------------------------------------------------- */
function matrixBlock(d, idxCase) {
  const rows = d.matrix || [];
  const sec = el("section", "block");
  const counts = { meets: 0, partial: 0, gap: 0, unknown: 0 };
  rows.forEach((r) => { if (counts[r.status] !== undefined) counts[r.status] += 1; });

  sec.appendChild(frag('<div class="block-head"><h2>Compliance matrix</h2>' +
    '<p class="muted">' + esc(d.card && d.card.matrix_summary ? d.card.matrix_summary : (idxCase.matrix_summary || "")) +
    "</p></div>"));

  const droppedCount = typeof d.matrix_dropped === "number" ? d.matrix_dropped
    : (Array.isArray(d.matrix_dropped) ? d.matrix_dropped.length : null);
  const meta = el("p", "mono muted");
  meta.textContent = rows.length + " rows kept" +
    (droppedCount !== null ? ", " + droppedCount + " dropped for a bad quote" : "");
  sec.appendChild(meta);
  const basis = d.evidence_basis || idxCase.evidence_basis;
  if (basis) sec.appendChild(el("p", "muted", "evidence: " + basis));

  if (!rows.length) {
    sec.appendChild(el("p", "muted",
      "No matrix row survived validation for this notice: every requirement the model wrote back failed the quote check, so the desk shows none of them."));
  } else {
    const filters = el("div", "filters");
    const list = el("div");
    const order = ["all", "meets", "partial", "gap", "unknown"];
    order.forEach((st) => {
      const n = st === "all" ? rows.length : counts[st];
      const b = el("button", null, st + " " + n);
      b.setAttribute("aria-pressed", st === "all" ? "true" : "false");
      b.addEventListener("click", () => {
        filters.querySelectorAll("button").forEach((x) =>
          x.setAttribute("aria-pressed", x === b ? "true" : "false"));
        drawMatrix(list, rows, st);
      });
      filters.appendChild(b);
    });
    sec.appendChild(filters);
    sec.appendChild(list);
    drawMatrix(list, rows, "all");
  }

  if (Array.isArray(d.matrix_dropped_rows) && d.matrix_dropped_rows.length) {
    const dr = el("div");
    dr.appendChild(el("p", "eyebrow", "Dropped rows"));
    d.matrix_dropped_rows.forEach((r) => {
      const body = el("div");
      body.appendChild(el("p", null, r.reason || "quote did not verify"));
      if (r.quote) body.appendChild(quote(r.quote, r.source_file, r.page));
      dr.appendChild(ledgerLine(r.requirement_id || "", body));
    });
    sec.appendChild(dr);
  }
  return sec;
}
function drawMatrix(list, rows, status) {
  list.innerHTML = "";
  rows.filter((r) => status === "all" || r.status === status).forEach((r) => {
    const body = el("div");
    const h = el("div", "card-head");
    h.appendChild(el("span", "chip chip-" + r.status, r.status));
    body.appendChild(h);
    if (r.firm_answer) body.appendChild(el("p", null, r.firm_answer));
    if (r.quote) body.appendChild(quote(r.quote, r.source_file, r.page));
    if (r.evidence) body.appendChild(el("p", "muted", r.evidence));
    list.appendChild(ledgerLine(r.requirement_id || "", body));
  });
  if (!list.children.length) list.appendChild(el("p", "muted", "No rows with that status."));
}

/* ---- drafts -------------------------------------------------------------- */
function draftsBlock(d) {
  const dr = d.drafts || {};
  const sec = el("section", "block");
  sec.appendChild(frag('<div class="block-head"><h2>Drafts</h2>' +
    '<p class="muted">written from the notice, not from a template</p></div>'));

  const tabs = el("div", "tabs");
  const panel = el("div");
  const defs = [
    ["Capability statement", () => capPanel(dr)],
    ["Questions for the CO", () => questionsPanel(dr)],
    ["Calendar", () => calendarPanel(dr, d)]
  ];
  defs.forEach(([label, build], i) => {
    const b = el("button", null, label);
    b.setAttribute("aria-selected", i === 0 ? "true" : "false");
    b.addEventListener("click", () => {
      tabs.querySelectorAll("button").forEach((x) =>
        x.setAttribute("aria-selected", x === b ? "true" : "false"));
      panel.innerHTML = "";
      panel.appendChild(build());
    });
    tabs.appendChild(b);
  });
  sec.appendChild(tabs);
  panel.appendChild(defs[0][1]());
  sec.appendChild(panel);
  return sec;
}
function capPanel(dr) {
  const wrap = el("div");
  if (!dr.capability_statement) {
    wrap.appendChild(el("p", "muted", "No capability statement in this snapshot."));
    return wrap;
  }
  const pre = el("pre", "draft", dr.capability_statement);
  wrap.appendChild(pre);
  return wrap;
}
function questionsPanel(dr) {
  const wrap = el("div");
  const qs = dr.questions_for_co || [];
  if (!qs.length) {
    wrap.appendChild(el("p", "muted", "No questions in this snapshot."));
    return wrap;
  }
  qs.forEach((q, i) => {
    const body = el("div");
    body.appendChild(el("p", null, typeof q === "string" ? q : (q.question || "")));
    if (q && q.source_quote) body.appendChild(quote(q.source_quote, q.source_file, q.page));
    wrap.appendChild(ledgerLine("Q" + (i + 1), body));
  });
  return wrap;
}
function calendarPanel(dr, d) {
  const wrap = el("div");
  let entries = dr.calendar_entries || [];
  if (!entries.length && d.deadlines && d.deadlines.calendar_entries) {
    entries = d.deadlines.calendar_entries;
  }
  if (!entries.length) {
    wrap.appendChild(el("p", "muted",
      "No calendar entries: the desk found no dated milestone it could quote from this notice."));
    return wrap;
  }
  entries.forEach((e) => {
    const body = el("div");
    body.appendChild(el("h3", null, e.title || "Untitled entry"));
    if (e.note) body.appendChild(el("p", null, e.note));
    if (e.source_quote) body.appendChild(quote(e.source_quote, e.source_file, e.page));
    const btns = el("div", "btn-row");
    const b = el("button", "btn", "Add to calendar (.ics)");
    b.addEventListener("click", () => downloadIcs(e, d));
    btns.appendChild(b);
    body.appendChild(btns);
    wrap.appendChild(ledgerLine(dayOf(e.date), body));
  });
  return wrap;
}
function icsDate(v) {
  const m = String(v || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return null;
  return m[1] + m[2] + m[3];
}
function icsEscape(s) {
  return String(s || "").replace(/\\/g, "\\\\").replace(/\n/g, "\\n").replace(/([,;])/g, "\\$1");
}
function downloadIcs(e, d) {
  const day = icsDate(e.date);
  if (!day) return;
  const next = new Date(Date.UTC(+day.slice(0, 4), +day.slice(4, 6) - 1, +day.slice(6, 8) + 1));
  const end = next.toISOString().slice(0, 10).replace(/-/g, "");
  const now = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+/, "");
  const lines = [
    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Biddesk//EN", "CALSCALE:GREGORIAN",
    "BEGIN:VEVENT",
    "UID:" + (d.notice_id || "biddesk") + "-" + day + "@biddesk",
    "DTSTAMP:" + now,
    "DTSTART;VALUE=DATE:" + day,
    "DTEND;VALUE=DATE:" + end,
    "SUMMARY:" + icsEscape(e.title || "Biddesk milestone"),
    "DESCRIPTION:" + icsEscape([e.note, e.source_quote, d.card && d.card.evidence_link]
      .filter(Boolean).join("\n")),
    "END:VEVENT", "END:VCALENDAR"
  ];
  const blob = new Blob([lines.join("\r\n")], { type: "text/calendar" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = (e.title || "biddesk").toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 40) + ".ics";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---- ledger rows for one notice ------------------------------------------ */
function caseLedgerBlock(idxCase, d) {
  const rows = idxCase.ledger_rows || [];
  const sec = el("section", "block");
  sec.appendChild(frag('<div class="block-head"><h2>Ledger for this notice</h2>' +
    '<p class="muted">one row per run, newest last</p></div>'));
  if (!rows.length) {
    sec.appendChild(el("p", "muted", "No ledger rows for this notice in the snapshot."));
  }
  rows.forEach((r) => {
    const body = el("div");
    body.appendChild(el("p", null, r.why || ""));
    let ev = null;
    try { ev = JSON.parse(r.evidence); } catch (err) { ev = null; }
    if (ev && typeof ev === "object") {
      const bits = [];
      if (ev.model_used) bits.push(ev.model_used + (ev.fallback_used ? " (fallback)" : ""));
      if (ev.fit_score !== undefined) bits.push("fit " + num(ev.fit_score, 2));
      if (ev.matrix_kept !== undefined) bits.push(ev.matrix_kept + " kept / " + ev.matrix_dropped + " dropped");
      if (ev.quotes_dropped !== undefined) bits.push(ev.quotes_dropped + " quotes dropped");
      if (ev.model_calls !== undefined) bits.push(ev.model_calls + " calls");
      if (ev.tokens_in !== undefined) bits.push(ev.tokens_in + " in / " + ev.tokens_out + " out");
      body.appendChild(el("p", "mono muted", bits.join(" \u00b7 ")));
    } else if (typeof r.evidence === "string" && r.evidence) {
      body.appendChild(el("p", "mono muted", r.evidence));
    }
    if (r.undo) body.appendChild(el("p", "muted", "undo available: " + r.undo));
    sec.appendChild(ledgerLine(stamp(r.ts), body, r.action + " \u00b7 tier " + r.tier));
  });
  const mine = S.localLedger.filter((x) => x.notice_id === (d.notice_id || idxCase.notice_id || ""));
  if (mine.length) sec.appendChild(localLedgerView(mine));
  return sec;
}

/* ---- view 4: bench ------------------------------------------------------- */
function viewBench() {
  clearBanner();
  const head = el("section", "block");
  head.appendChild(frag(
    '<p class="eyebrow"><a href="#/">All firms</a></p>' +
    "<h1>What the run cost</h1>" +
    '<p class="muted">Counts from the committed snapshot built ' + esc(stamp(S.index.built_at)) + ".</p>"
  ));

  const t1 = el("section", "block");
  t1.appendChild(frag('<div class="block-head"><h2>Tiering</h2></div>'));
  const w1 = el("div", "tablewrap");
  let h = "<table><thead><tr><th>firm</th><th>pulled</th><th>filed by rules</th>" +
    "<th>filed by cheap model</th><th>read by desk</th><th>model calls</th></tr></thead><tbody>";
  (S.index.firms || []).forEach((f) => {
    const t = f.tier_counts || {};
    h += "<tr><td class=\"wrapcell\">" + esc(f.profile.name) + "</td>" +
      '<td class="num">' + num(t.total) + "</td>" +
      '<td class="num">' + num(t.tier0_filed) + "</td>" +
      '<td class="num">' + num(t.tier1_filed) + "</td>" +
      '<td class="num">' + num(t.tier2_sent) + "</td>" +
      '<td class="num">' + num(t.model_calls) + "</td></tr>";
  });
  h += "</tbody></table>";
  w1.appendChild(frag(h).firstChild);
  t1.appendChild(w1);

  const t2 = el("section", "block");
  t2.appendChild(frag('<div class="block-head"><h2>Per case</h2>' +
    '<p class="muted">' + num(S.index.case_count) + " cards in this snapshot</p></div>"));
  const w2 = el("div", "tablewrap");
  let h2 = "<table><thead><tr><th>notice</th><th>firm</th><th>kept</th><th>dropped</th>" +
    "<th>calls</th><th>tokens in</th><th>tokens out</th><th>seconds</th><th>model</th><th>fallback</th>" +
    "</tr></thead><tbody>";
  (S.index.firms || []).forEach((f) => {
    (f.cases || []).forEach((c) => {
      h2 += "<tr>" +
        '<td class="num"><a href="#/f/' + esc(f.profile.slug) + "/c/" + esc(c.notice_id) + '">' +
        esc(String(c.notice_id).slice(0, 8)) + "</a></td>" +
        '<td class="wrapcell">' + esc(f.profile.name) + "</td>" +
        '<td class="num">' + num(c.matrix_kept) + "</td>" +
        '<td class="num">' + num(c.matrix_dropped) + "</td>" +
        '<td class="num">' + num(c.model_calls) + "</td>" +
        '<td class="num">' + num(c.tokens_in) + "</td>" +
        '<td class="num">' + num(c.tokens_out) + "</td>" +
        '<td class="num">' + num(c.seconds, 1) + "</td>" +
        '<td title="' + esc(c.model_used || "") + '">' + esc(shortModel(c.model_used)) + "</td>" +
        "<td>" + (c.fallback_used ? "yes" : "no") + "</td></tr>";
    });
  });
  h2 += "</tbody></table>";
  w2.appendChild(frag(h2).firstChild);
  t2.appendChild(w2);

  mount([head, t1, t2]);
}

function notFound(msg) {
  mount([frag('<section class="block"><h1>Not in this snapshot</h1><p class="muted">' +
    esc(msg) + '</p><p><a href="#/">Back to the firms</a></p></section>').firstChild]);
}

/* ---- index shape adapter -------------------------------------------------
   The gallery index is written in two shapes: the flat SPEC shape
   (firm fields at the top level, `tiers`, `produced_at`) and the earlier
   nested shape (`profile`, `tier_counts`, `built_at`). Everything below this
   function reads the nested shape, so the flat one is mapped onto it here.
   Missing optional fields stay missing; nothing is invented. */
function normCounts(t) {
  if (!t) return {};
  const out = Object.assign({}, t);
  if (out.tier0_filed === undefined && t.tier0 !== undefined) out.tier0_filed = t.tier0;
  if (out.tier1_filed === undefined && t.tier1 !== undefined) out.tier1_filed = t.tier1;
  if (out.tier2_sent === undefined && t.tier2 !== undefined) out.tier2_sent = t.tier2;
  return out;
}
function normIndex(ix) {
  if (!ix || !Array.isArray(ix.firms)) return ix || {};
  if (ix.built_at === undefined && ix.produced_at !== undefined) ix.built_at = ix.produced_at;
  if (ix.case_count === undefined) {
    ix.case_count = ix.firms.reduce((n, f) => n + ((f.cases || []).length), 0);
  }
  ix.firms = ix.firms.map((f) => {
    if (f.profile) {
      f.tier_counts = normCounts(f.tier_counts || f.tiers);
      return f;
    }
    const profile = {
      slug: f.slug, name: f.name, city: f.city, state: f.state,
      what: f.what, rules: normRules(f.rules), fictional: f.fictional
    };
    return {
      profile: profile,
      tier_counts: normCounts(f.tiers || f.tier_counts),
      week: f.week || [],
      cases: f.cases || [],
      ledger_summary: f.ledger_summary || {}
    };
  });
  return ix;
}
function normRules(rules) {
  /* rules may be objects {id,text} or plain strings; strings carry no id */
  return (rules || []).map((r) => (typeof r === "string" ? { id: "", text: r } : r));
}
/* ---- boot ---------------------------------------------------------------- */
function rerender() {
  const r = route();
  if (r.view === "bench") return viewBench();
  if (r.view === "firm") return viewFirm(r.slug);
  if (r.view === "case") return viewCase(r.slug, r.id);
  return viewFirms();
}

async function boot() {
  try {
    S.index = normIndex(await getJSON(P.index()));
  } catch (err) {
    document.getElementById("main").innerHTML =
      '<section class="block"><h1>The snapshot did not load</h1>' +
      '<p class="muted">' + esc(err.message) + "</p></section>";
    return;
  }
  const meta = document.getElementById("foot-meta");
  meta.textContent = "snapshot built " + stamp(S.index.built_at) + " \u00b7 " +
    num(S.index.case_count) + " cards \u00b7 " +
    (BIDDESK_API ? "live endpoint configured" : "no live endpoint configured");
  window.addEventListener("hashchange", rerender);
  rerender();
}
boot();
