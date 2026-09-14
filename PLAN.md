# Biddesk (Professional track): the bid desk that reads SAM.gov so a small contractor does not have to

**What it is.** A small firm gives Biddesk its profile once: what it does, NAICS codes, certifications (8(a), HUBZone, SDVOSB, WOSB), past performance, states it serves, and the owner's rules. From then on Biddesk watches SAM.gov in the background. Every new solicitation is scored against the profile, the attachments are read, the compliance matrix and a draft capability statement are produced, question and submission deadlines go on the calendar, and the owner sees one card per real candidate: bid or no-bid, with the reasons and the deadline. Everything below the bar is filed silently with a one-line reason.

**The wow.** The judge picks one of three real firm profiles. Biddesk queries the live SAM.gov API on camera, pulls this week's actual solicitations, reads a real attachment, and produces a bid/no-bid card with a compliance matrix drawn from the real solicitation text. The judge can open the solicitation on SAM.gov and check every row.

**Why it matters (sourced, read 2026-09-13).** The federal government's statutory goal is 23 percent of prime contract dollars to small businesses; in FY2024 the government-wide figure was 21.65 percent, down from 22.66 percent the year before (SBA FY2024 Small Business Procurement Scorecard, via congress.gov and smallgovcon.com; verify the primary scorecard page before the video). Small firms miss set-aside opportunities because reading SAM.gov every day, screening hundreds of notices, and building a compliance matrix per solicitation is a full-time job they do not have. Biddesk does that job.

**Who it is for.** Owner-operated firms with 1 to 50 people that hold or could hold a set-aside status: IT services, construction, janitorial, landscaping, medical staffing, engineering. The SAM.gov API is public, so the product works for any firm.

## The demo gallery is the product

Real data on the opportunity side, three fictional-but-realistic firms on the profile side. The video says so.

**Real data:**
- SAM.gov Get Opportunities public API, production endpoint `https://api.sam.gov/opportunities/v2/search`, parameters `postedFrom`, `postedTo` (required, MM/dd/yyyy, max one-year span), `ptype` (o = solicitation, p = presolicitation, k = combined synopsis/solicitation), `ncode` (NAICS), `typeOfSetAside` (SBA, 8A, HZC, SDVOSBC, WOSB), `limit` up to 1000, `offset`. Confirmed at open.gsa.gov/api/get-opportunities-public-api on 2026-09-13.
- API key: self-serve. Log in to SAM.gov (login.gov account), Profile, Account Details, "Public API Key," reveal. Keys from api.data.gov do not work for SAM.gov (confirmed on the GSA developer forum). No waiting on anyone; the key is created the same day. Daily request limits depend on role and are not published; cache every response.
- Solicitation attachments: the `description` field is a URL to download with the API key appended; attachments come from the resource links in the notice. Cache them as fixtures with fetch date and URL.

**Three firm profiles in the gallery (fictional firms, real-shaped profiles):**

| Firm | What it does | NAICS | Set-aside | Owner rule |
|---|---|---|---|---|
| Red Cedar Facility Services, Oklahoma City | Janitorial and grounds | 561720, 561730 | WOSB | No bids over 200 miles from OKC; no bids under a 12-month base period |
| Sooner Systems LLC, Norman | IT support and cybersecurity staffing | 541512, 541519 | SDVOSB | Only bid where the firm has two past-performance matches |
| Plains Med Staffing, Tulsa | Nursing and allied health staffing for VA and IHS | 561320, 621399 | 8(a) | Never bid on a solicitation with under 10 days to close |

Each profile has a past-performance list, a capabilities paragraph, and the owner's rules written in plain English. The agent enforces the rules in code (a filter before any model call), not by asking the model to remember them.

**Fixture snapshot.** One committed snapshot of a real week of SAM.gov results per profile (say 300 notices), plus the attachments for the ten that score highest, so the bench and the offline fallback are deterministic. On camera the live query runs first; if SAM.gov is slow the page says "showing the cached snapshot from <date>" rather than pretending.

## What the judge sees, scene by scene

1. **Open (20 s).** A screen recording of SAM.gov's search page with hundreds of results. "This is what a small contractor is supposed to read every morning."
2. **Pick a firm (10 s).** Judge opens the live page from a QR, picks Red Cedar.
3. **Live pull (20 s).** "Querying SAM.gov for the last 7 days, NAICS 561720 and 561730, WOSB and total small business set-asides." Count of notices returned, real, on screen.
4. **Tiered triage (20 s).** A counter shows the three tiers: N killed by owner rules with zero model calls (distance, base period), N killed by a cheap model (wrong scope), N sent to the full reader. This scene is the Google tiered-routing pattern made visible.
5. **Deep read (40 s).** For the top candidate: the agent downloads the real attachment, produces the compliance matrix (every "shall" in the solicitation as a row, with the firm's answer or a gap), the key dates, the evaluation criteria, and the incumbent if an award history exists.
6. **Decision Card (20 s).** Bid or no-bid, three reasons each way, the owner's default, the questions deadline, the submission deadline. The judge taps "bid."
7. **Drafts (20 s).** Capability statement tailored to the solicitation, questions-to-the-contracting-officer list, calendar entries, all from the real text.
8. **Background scene (20 s).** Fast-forward: a simulated week. New notices arrive, most are filed silently with one-line reasons in the ledger, two cards surface. An amendment to a tracked solicitation moves a deadline and the card updates. This is the "runs in the background, surfaces only for a decision" requirement, on camera.
9. **Refusal scene (10 s).** The owner rule says never bid under 10 days; a tempting notice with 8 days to close is filed, not surfaced, and the ledger explains why. Rules win over the model.
10. **Bench, diagram, AgentCore endpoint (30 s).**
11. **Pitch close (30 s).**

## Architecture

```
SAM.gov API (scheduled poll) ──▶ Normalizer (typed Notice events on a queue)
                                      │
                                      ▼
                         Tier 0: owner rules in code (no model)
                         Tier 1: cheap model scope check (Haiku on Bedrock, ~50 tokens)
                         Tier 2: full reader
                                      │
                                      ▼
            Strands Graph, parallel per surviving notice:
              ├─ Attachment reader (PDF/Word → requirements list, "shall" statements)
              ├─ Fit scorer (profile vs requirements, past-performance matching)
              └─ Deadline and history agent (dates, amendments, prior awards via the API's award fields)
                                      │
                                      ▼
            Bid desk agent: compliance matrix, bid/no-bid card, drafts (structured output)
                                      │
                                      ▼
            Ledger, calendar feed, owner's page with cards; MCP server exposing score_notice()
```

- **Strands pieces, by name:** `Agent` with structured output for Notice, Requirements, Matrix, Card; `@tool` for `sam_search`, `sam_fetch_attachment`, `profile_rules`, `calendar_add`, `draft_capability_statement`; `Graph` for the parallel specialists; a scheduled loop (the poll) feeding an asyncio queue of typed events so independent notices are processed at the same time (the Google event-driven pattern); hooks: a `BeforeToolCall` hook that refuses any tool call that would submit or sign anything on SAM.gov (Biddesk drafts, the owner submits), and a hook that stamps every finding with the notice ID and the attachment page it came from; AgentCore Memory for the owner's rules and past decisions so a "no-bid, too far" once becomes a standing rule.
- **Same-bar fallback:** Sonnet primary, Haiku fallback, one `validate_matrix()` that requires every matrix row to quote the solicitation text it came from.
- **AgentCore:** Runtime for the deployed agent and the live demo link; Memory for rules; Observability for the tier counters shown in scene 4. Gateway can wrap the SAM.gov API as a tool target if time allows (`../14_AGENTCORE_GATEWAY.md`); it is a nice-to-have.
- **Evals:** deterministic bench on the fixture snapshot: for each profile, the set of notices that should surface and the set that must be filed by rule; a judge model scores matrix completeness against the real attachment.

## Build phases

1. **Key and pull phase.** Create the SAM.gov API key, run `sam_search` for the three profiles over the last 7 days, save the snapshot with dates. Done-check: a script prints counts per profile and one full notice.
2. **Tiering phase.** Owner rules in code, cheap scope check, counters. Done-check: bench shows the rule-filed notices never reach the model.
3. **Reader phase.** Attachment download and requirements extraction with page citations. Done-check: for the ten cached attachments, every "shall" statement in a hand-marked list is captured.
4. **Desk phase.** Matrix, card, drafts, calendar, ledger, refusal hook. Done-check: scenes 2 through 9 run on a phone from the QR.
5. **Background phase.** The simulated week with amendments. Done-check: the fast-forward shows only cards above the bar.
6. **Deploy phase.** AgentCore Runtime endpoint, MCP server. Done-check: the judge URL works from cellular.
7. **Ship phase.** Video, README with bench table and SOURCES.md, diagram, MIT license in About, builder.aws post ("Agents for Humans: an agent that reads SAM.gov every morning"), Devpost form.

## Cut list

Gateway wrapping, the MCP server, the amendment scene, the third profile. Never cut: the live SAM.gov query on camera, the tier counters, the compliance matrix with quotes, the refusal scene.

## Risks

- **API rate limit or outage on camera.** Cached snapshot with an honest "cached from <date>" banner. Record the live pull for the video when it works.
- **Attachments are scanned PDFs.** Bedrock multimodal reads page images; the fixture set includes one scanned attachment on purpose.
- **Fictional firms.** Say so on screen and in the README. The solicitations are real; the firm is a stand-in for any owner who loads their own profile, which the live page allows.
