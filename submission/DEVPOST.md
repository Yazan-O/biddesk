# Devpost entry: Biddesk

Track: Professional. Paste each section into the matching Devpost field.

---

## Tagline (one line)

The bid desk that reads SAM.gov every morning so a small contractor does not have to.

---

## Inspiration

Federal agencies post every solicitation on SAM.gov. The law sets a goal of 23 percent of federal prime contract dollars going to small businesses (15 U.S.C. 644(g)(1)(A)(i)). The firms that hit that goal are usually the firms with somebody whose whole job is reading SAM.gov, screening notices, and building a compliance matrix per solicitation. A firm of 1 to 50 people does not have that person, so the owner reads notices at night and misses deadlines.

The hackathon theme is agents that run in the background and interrupt a human only when a human must decide. Bid or no-bid is exactly that decision. Everything before it is reading.

---

## What it does

A firm loads its profile once: what it does, its NAICS codes, its set-aside status, its past performance, and the owner's rules in plain English ("no work over 200 miles", "never bid under ten days to close"). From then on Biddesk watches SAM.gov.

Every new notice goes through three tiers. The owner's rules run first, in code, with zero model calls. What survives gets one cheap Haiku call that asks only whether the work is the kind the firm does. What survives that goes to the full desk: three specialists read the real attachments in parallel and produce a compliance matrix where every row quotes the sentence it came from, with the file and the page.

The owner sees one Decision Card per real candidate: the situation in two lines, three reasons for, three reasons against, the default if nobody answers, and the deadline. Everything below the bar is filed silently with a one-line reason, visible in a ledger with undo.

On one real 30-day window (notices posted 2026-08-14 to 2026-09-13 for the janitorial and IT firms, pulled 2026-09-14; the medical staffing firm stays on a 7-day keyed window, 09/06/2026 to 09/13/2026), across three firms: **608 notices in**. The owners' rules decided 178 of 231 for the janitorial firm, 220 of 367 for the IT firm, and 10 of 10 for the medical staffing firm before any model call: 408 notices filed at zero model cost. One cheap Haiku call each then filed 161 more (31 and 130), and **39 reached the full desk**, which produced 35 Decision Cards; 4 were held back by the no-evidence gate. Source: `data/bench/tiering_<slug>.json` and `gallery/index.json`.

The attachment reader holds 1,328 files across 361 notices, 303 of which carry attachments, and has read 15,622 pages into 52,483 requirement sentences. Source: `python -m biddesk.reader report`.

---

## How we built it

Strands Agents on Amazon Bedrock, deployed on Bedrock AgentCore Runtime in `us-east-1`.

- **Triage** is plain Python. Notice type, set-aside eligibility, days to close, minimum base period, past-performance overlap, and real distance from the firm's city using 41,197 GeoNames ZIP centroids. A rule fires only on a known fact, so an unknown place of performance never files a notice.
- **The cheap tier** is one `Agent` call on Haiku 4.5 with `structured_output_model=ScopeCheck`.
- **The desk** is a Strands `Graph` built with `GraphBuilder`: three structured-output specialists as parallel entry nodes (reader, fit, deadlines), joined by a merge node. Each specialist is wrapped in a `MultiAgentBase` node so one failure does not kill the graph.
- **The bar** is `validate_matrix()`. A matrix row survives only if its quote is a verbatim substring of the requirement it cites and the file and page match. The same validator runs on the Sonnet primary and the Haiku fallback, so the fallback cannot lower the bar.
- **Guards** are Strands hooks, not prompt text: `NoSubmitGuard` on `BeforeToolCallEvent` cancels any tool call that would submit or sign in the owner's name and writes the denial to the ledger. `NoSubmitIntervention` is the same rule as an `InterventionHandler` with `on_error="deny"`, so a crash in the matcher fails closed. `ProvenanceStamp` on `AfterToolCallEvent` stamps every read with its notice id, file and page.
- **The ledger** is append-only JSONL with a ten-minute veto window and undo by appending, never by rewriting.
- **The attachment reader** makes no model calls. It downloads, extracts per page, and lifts binding requirement sentences deterministically.
- **The page** is CloudFront and S3 for the static gallery, a Lambda Function URL (`biddesk-proxy`, SigV4) calling AgentCore Runtime `biddesk_runtime-sBS5N4G0Fo` for the live actions. Deployment is direct code deploy (zip, `PYTHON_3_13`, ARM64), because the build machine has no Docker.
- **The models** are `us.anthropic.claude-sonnet-4-6` primary and `us.anthropic.claude-haiku-4-5-20251001-v1:0` fallback on Bedrock. All 35 shipped cases ran on the primary with no fallback. The whole desk round, 38 runs including the re-runs and the two forced-Haiku legs, cost 2,371,969 input and 512,922 output tokens.

---

## Challenges we ran into

- **The SAM.gov keyed API hit its daily quota mid-build** (HTTP 429, code 900804, reset 2026-09-15 00:00 UTC). Rather than fake the data, we switched descriptions to the public notice JSON the sam.gov page itself loads, cached the throttle window so the client stops calling, and built the page to say "showing the cached snapshot from `<date>`" instead of pretending. Then we gave the live pull the same second route: keyed API first, the unkeyed sam.gov search while the key is throttled, and the note under the counts names which route answered.
- **Strands `Graph` is fail-fast for plain `Agent` nodes.** An exception inside a node is re-raised and kills the graph with no `NodeResult` to inspect. We wrapped each specialist in a `MultiAgentBase` node that reports `COMPLETED` and carries the failure as a payload, so the desk still runs with one specialist marked unavailable.
- **Geography beat the model.** A state centroid can sit more than 200 miles from the real work site, so an early version of the distance rule filed notices it should not have. The rule now carries a precision label and needs twice the owner's limit before a state-precision distance files anything.
- **Whole-word matching matters.** "ATO" was matching "operator" and "RN" was matching "furnish", which silently killed real candidates at tier 0.
- **Scanned attachments.** 118 of the 1,328 held files are scanned images with no extractable text, and 24 more are in formats the reader does not support. They are in the fixture set on purpose: the reader labels them instead of guessing at their contents, and all 73 notices that hold one still yield requirements. Separately, 35 notices extract to zero requirements, 28 of them because they carry no attachment at all; those reach the desk only when the notice's own description is long enough to read, and on the one such case that shipped, `validate_matrix` dropped all 9 rows the specialist wrote, so the card reads "0 requirements checked".

---

## Accomplishments

### What is measured, and by whom

The eval bench (`evals/BENCH.md`) has three parts. Two are deterministic and run with no model call: the tier-0 rules re-run against every notice match the hand-checked expectations on 408 of 408 filings and 35 of 35 surfacing notices, and the one human ground truth in the repo, ten hand-marked attachment pages, finds 33 of 69 "shall" statements in the kept matrix rows (0.478 over the 7 marked notices that have a card). The third is a Sonnet judge's opinion of the matrix, mean 0.653 over 34 of 35 cards, with the zero-requirement card left unscored rather than given a vacuous 1.0. The background week on each firm page is a replay of the committed snapshot in posted order, day by day, with zero model calls; every surfaced id is a gallery card and no filed notice surfaces.

### Results we can show

- **Most notices are decided before a model sees them.** The owner's rules filed 178 of 231, 220 of 367, and 10 of 10 notices in code, at zero model cost. The bench asserts that no tier-0 row consumed a model call. Of the 200 survivors, 161 were filed by a single cheap call each and 39 reached the full desk. 6.4 percent of the feed cost a desk run.
- **No quote reaches a human unchecked.** Rows whose quote is not verbatim in the cited document are dropped by code and counted, not repaired: 125 rows dropped, 1,072 kept, across the 35 cases.
- **The refusal is real.** Ask the desk to submit an offer on SAM.gov and the guard cancels the tool call before it runs and writes the denial to the ledger. Biddesk drafts, the owner submits.
- **Every number is traceable.** The repo carries `data/SOURCES.md` with a URL and a fetch date for every fixture, and nothing enters the video or this entry without a row there.

### Two things we will not overclaim

**The fit score is a within-run number.** It is computed in code, never typed by the model: meets = 1, partial = 0.5, gap = 0, unknown excluded, averaged over the matrix rows that survived validation in that run. The set of surviving rows moves run to run, so the score is not comparable across runs or models: the same notice on the same model came out 0.64 over 23 validated rows and 0.56 over 35. That is why the card reads "fit N over K validated rows" instead of a bare percentage.

**The second `bid` card is not a find.** Two cases came back `bid`. One is real: `ee15f287`, an Air Force Sources Sought, where a `bid` means sending a capability statement in response to an RFI, not a proposal, and the card says exactly that. The other, `d8c230fb`, is a janitorial notice that had already closed 3.6 days before the pull, with a 5-row matrix reading 0 met, 1 partial, 4 unknown against a median of 34 rows across the 35 cases. Thin evidence produced a `bid` because there was almost nothing to find a gap in. The page labels it closed and disables the answer buttons, and it is named here rather than left for a judge to find.

---

## What we learned

- Put the deterministic layer first and the tier counter on screen. The cheapest way to make an agent trustworthy is to show how often it did not need a model.
- Rules belong in code, not in a system prompt. A prompt is a request. A `BeforeToolCallEvent` hook is a refusal.
- A validator that both the primary and the fallback model must clear is worth more than a better primary model.
- Honest degradation is a feature a judge can see. The cached banner does more for trust than a demo that hides a rate limit.

---

## What's next for Biddesk

- AgentCore Memory so a repeated "no-bid, too far" becomes a standing rule the owner never answers twice.
- AgentCore Gateway wrapping the SAM.gov API as a tool target with a Cedar policy carrying the same no-submit rule.
- An MCP server exposing `score_notice()` so other agents can call the desk.
- Amendment tracking across a full quarter, not one month.
- Owner-loaded profiles on the live page, so any firm can run its own rules against the same feed.

---

## Built with

`python` · `strands-agents` · `amazon-bedrock` · `claude-sonnet-4.6` · `claude-haiku-4.5` · `bedrock-agentcore` · `agentcore-runtime` · `aws-lambda` · `amazon-s3` · `amazon-cloudfront` · `sam.gov-api` · `pydantic` · `geonames` · `html` · `css` · `javascript`

---

## Links to fill before submitting

- Live demo URL: <https://dpnzgd4gtjs45.cloudfront.net/web/> (CloudFront `EJS7ZC1JONY7Z` over a private S3 bucket; the phone-on-cellular check is still an owner action)
- Video URL: (not recorded yet)
- Repository URL: <https://github.com/Yazan-O/biddesk>
- Architecture diagram: [`submission/architecture.svg`](architecture.svg), [`submission/architecture.png`](architecture.png)
