# Devpost final entry: Biddesk

Paste each section into the matching Devpost field, in order.

---

## Project name

Biddesk

## Elevator pitch

A bid desk that reads SAM.gov so a small contractor does not have to.

## About the project

### Inspiration

If you run a 20-person janitorial firm in Oklahoma City, SAM.gov is your pipeline. Every federal solicitation is posted there. The reading job is real: screen the notices every morning, build a compliance matrix out of the attachments, put two deadlines per bid on a calendar. Large firms employ that person. Owner-operated firms do the reading at 10 p.m. and miss things.

The law sets a goal of [23 percent](https://www.law.cornell.edu/uscode/text/15/644) of federal prime contract dollars going to small businesses (15 U.S.C. 644(g)(1)(A)(i)). The firms that miss out are often not the ones that cannot do the work. They are the ones that cannot read the feed. The hackathon theme is agents that run in the background and interrupt a human only when a human must decide. Bid or no-bid is exactly that decision.

### What it does

A firm loads its profile once: NAICS codes, set-aside status, past performance, and the owner's rules in plain English ("no work over 200 miles", "never bid under ten days to close"). From then on Biddesk watches SAM.gov.

Every new notice goes through three tiers. The owner's rules run first, in code, with zero model calls. What survives gets one cheap Haiku call. What survives that goes to the full desk: three specialists read the real attachments in parallel and produce a compliance matrix where every row quotes the sentence it came from, with the file and page number.

The owner sees one Decision Card per real candidate: the situation in two lines, three reasons for, three reasons against, the default if nobody answers, and the deadline. Everything below the bar is filed silently with a one-line reason, visible in a ledger the owner can open and undo.

Measured on one real 30-day SAM.gov window (notices posted 2026-08-14 to 2026-09-13, three firms): 608 notices in, 408 filed by the owner's rules with zero model calls, 161 filed by one cheap model call each, 39 reached the full desk, [35 Decision Cards](https://dpnzgd4gtjs45.cloudfront.net/web/) shipped. The attachment reader holds 1,328 files across 303 notices, 15,622 pages, 52,483 requirement sentences.

### How we built it

Strands Agents on Amazon Bedrock, deployed on Bedrock AgentCore Runtime in `us-east-1`.

**Triage** is plain Python: notice type, set-aside eligibility, days to close, distance from the firm's city using 41,197 real GeoNames ZIP centroids. A rule fires only on a known fact, so an unknown place of performance never files a notice. The cheap tier is one `Agent` call on Haiku 4.5 with `structured_output_model`. **The desk** is a Strands `Graph` built with `GraphBuilder`: three structured-output specialists as parallel entry nodes (reader, fit, deadlines), each wrapped in a `MultiAgentBase` node so one failure does not kill the graph. **The bar** is `validate_matrix()`: a matrix row survives only if its quote is a verbatim substring of the requirement it cites and the file and page match. The same validator runs on the Sonnet primary and the Haiku fallback, so the cheap model gets no easier exam.

**Guards** are Strands hooks, not prompt text. `NoSubmitGuard` on `BeforeToolCallEvent` cancels any tool call that would submit in the owner's name and writes the denial to the ledger. `NoSubmitIntervention` is the same rule as an `InterventionHandler` with `on_error="deny"`, so a crash in the matcher fails closed. `ProvenanceStamp` on `AfterToolCallEvent` stamps every read with its notice id, file and page.

The page is CloudFront and S3 for the static gallery, a Lambda Function URL (`biddesk-proxy`, SigV4) calling AgentCore Runtime for live actions. Models: `us.anthropic.claude-sonnet-4-6` primary and `us.anthropic.claude-haiku-4-5` fallback, both on Amazon Bedrock. All 35 shipped cards ran on the primary with no fallback.

### Challenges we ran into

The SAM.gov API hit its daily quota mid-build (HTTP 429). Rather than fake the data, we found the unkeyed search route the sam.gov site itself uses, cached the throttle window, and built the page to say "showing the cached snapshot" with its date instead of pretending. The live pull tries the keyed API first and names which route answered.

Strands `Graph` is fail-fast for plain `Agent` nodes: an exception kills the graph with no result. We wrapped each specialist in a `MultiAgentBase` node that carries the failure as a payload, so the desk still runs with one specialist marked unavailable.

Geography was harder than the model. A state centroid can sit more than 200 miles from the real work site. The distance rule now carries a precision label and needs twice the owner's limit before a state-precision distance files anything.

### Accomplishments that we're proud of

**Most notices never reach a model.** The owner's rules filed 408 of 608 notices at zero model cost. The eval bench re-runs tier-0 rules against every notice and matches on 408 of 408 filings and 35 of 35 surfacing notices with no model call.

**No quote reaches a human unchecked.** Matrix rows whose quote is not verbatim in the cited document are dropped and counted: 1,072 kept, 125 dropped across 35 cases.

**The refusal is real.** Ask the desk to submit an offer on SAM.gov and the guard cancels the tool call before it runs. The denial is written to the ledger. Biddesk drafts; the owner submits.

**The fit score is honest.** It is computed in code, not by the model. The card reads "fit N over K validated rows" because the row set varies run to run.

### What we learned

Put the deterministic layer first and its counter on screen. The cheapest way to make an agent trustworthy is to show how often it did not need a model.

Rules belong in code, not in a system prompt. A prompt is a request. A `BeforeToolCallEvent` hook is a refusal.

A validator that both models must clear is worth more than a better primary model. And when the upstream API rate-limits you mid-demo, show the cached banner with its date instead of pretending. Honest degradation reads as competence.

### What's next for Biddesk

AgentCore Memory so a repeated "no-bid, too far" becomes a standing rule the owner never answers twice. AgentCore Gateway wrapping the SAM.gov API with a Cedar no-submit policy. An MCP server exposing `score_notice()` so other agents can call the desk. Amendment tracking across a full quarter, not one month. Owner-loaded profiles on the live page, so any firm can run its own rules against the same feed.

## Built with

python, strands-agents, amazon-bedrock, claude-sonnet-4.6, claude-haiku-4.5, bedrock-agentcore, agentcore-runtime, aws-lambda, amazon-s3, amazon-cloudfront, sam.gov-api, pydantic, geonames, html, css, javascript

## Try it out links

- **Live demo:** https://dpnzgd4gtjs45.cloudfront.net/web/
- **GitHub repo:** https://github.com/Yazan-O/biddesk
- **Gallery JSON:** https://dpnzgd4gtjs45.cloudfront.net/gallery/index.json

## Image gallery

Upload these files in order:

1. `submission/thumbnail.png`: Biddesk thumbnail (project cover image)
2. `submission/architecture.png`: Architecture: firm profile, SAM.gov pull, tiered triage, Strands Graph desk, Decision Card, guard hooks, ledger
3. `submission/media/hero_phone.png`: Decision Card for a real SAM.gov notice on a phone viewport
4. `submission/media/flow.gif`: One continuous take: firm list, tier sieve, ledger, Decision Card with compliance matrix, answer, undo
5. `submission/media/refusal.gif`: Guard hook cancels a submit tool call in code and writes the denial to the ledger
6. `submission/media/qr_live.png`: QR code for the live demo (scan from a phone, no setup)

## Video demo link

[VIDEO_URL]

## Submitter Type

Individual

## Country of Residence

United States

## Organization



## Track

Professional

## PUBLIC URL to your code repo

https://github.com/Yazan-O/biddesk

## Architecture diagram

`submission/architecture.png`

## AWS Builder ID

[OWNER FILLS]

## Live demo link

https://dpnzgd4gtjs45.cloudfront.net/web/

## Testing instructions

1. Open https://dpnzgd4gtjs45.cloudfront.net/web/ on any device. No login, no AWS account, no setup needed.
2. The home page lists three fictional firms (labeled "Fictional firm. Real SAM.gov notices."). Tap **Red Cedar Facility Services** to open it.
3. Read the tier sieve at the top: it shows how many of the 231 real SAM.gov notices were filed by the owner's rules in code (zero model calls), how many by one cheap model call, and how many reached the full desk.
4. Scroll down to the **Decision Cards**. Open any card to see the compliance matrix: every row quotes a sentence from the real solicitation attachment, with the file name and page number. The fit score is computed in code and labeled "over K validated rows".
5. Tap the **Ledger** to see every silent filing with its one-line reason and the undo button. Undo a filing and watch it reverse.
6. Open the **Background week** strip to see the 30-day feed replayed day by day with zero model calls: each day shows what was posted, what was filed, and what surfaced.
7. Go back and tap **Plains Med Staffing**. Tap **"Ask the desk to submit this offer"**. The guard hook cancels the tool call before it runs, writes a `denied_tool` row to the ledger, and the agent tells you it cannot submit. Nothing is sent.
8. Tap **"Run the pull live"** on any firm page. The agent pulls from SAM.gov in real time (about 25 seconds), runs triage, and replaces the cached panel with the live result. The note under the counts names which route served it and the timestamp.

## Bonus blog post URL

[BLOG_URL, post submission/BLOG_DRAFT.md on builder.aws with "Agents for Humans" in the title]
