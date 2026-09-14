# Biddesk (Professional track), AWS Agents for Humans hackathon

Biddesk watches SAM.gov in the background for a small contractor and surfaces one bid/no-bid card per real candidate. The full product, demo, and build plan is `PLAN.md` in this folder; read it first and in full. This session owns Biddesk from first line of code to the Devpost submission: code, demo gallery, live deployment, video, README, architecture diagram, MIT license, builder.aws blog post.

## Goal, in order of what judges score
Real problem with sourced numbers, a demo a judge can run from a QR code with no setup, a complete product (not a proof of concept), a non-obvious use of Strands, and a video under 5 minutes that shows it end to end. The demo gallery built from real public data is the product; nobody uploads their own data.

## Stack facts (verified in the reference folder, do not re-derive)
- Reference: `../00_QUICKSTART.md` through `../22_AGENTCORE_PAYMENTS.md`, every Strands and AgentCore doc page read in full on 2026-09-07. Read the file for whatever you are about to build before writing it. `../19_API_SYMBOLS.md` lists every top-level symbol.
- Installed on this machine: Python 3.13, `strands-agents` 1.55.1, `strands-agents-tools`, `policyengine-us` 2.0.4, `awscli` 1.46.1, Node 23, `@aws/agentcore` CLI 0.29.0.
- Region: `us-east-1` for everything (the only Region where every AgentCore component exists). Model: `us.anthropic.claude-sonnet-4-6` primary (the `global.` profile hit a per-account daily token cap on 2026-09-14; the US cross-region profile is the same model on a separate quota), Haiku 4.5 fallback, both on Bedrock. Model access must be enabled in the Bedrock console; that is the step that blocks a first run.
- Credentials come from `.env` in this folder (copy `.env.example`). Never commit it. Never print a key.
- Strands Graph runs nodes in parallel; Swarm does not. Use Graph for the specialists.
- Deploy with the AgentCore CLI: `agentcore create`, `agentcore dev`, `agentcore deploy`, `agentcore invoke`. The old `bedrock-agentcore-starter-toolkit` is retired.
- Shared design across the three products: `../QUIET_CORE_SPEC.md` (Decision Card, ledger with undo, guard hooks, tiered routing, same-bar fallback). Implement it locally in this repo; do not import from the sibling products.

## Rules for this build
- Real data only. Every fixture carries its source URL and fetch date in `data/SOURCES.md`. Fictional parts (patients, firms, households) are labeled fictional on screen and in the README.
- Every number in the pitch traces to a primary source listed in `data/SOURCES.md`; a number without one stays out of the video.
- A phase is done when its done-check in `PLAN.md` runs and its output is pasted into `STATUS.md`. No "done" without the command and its output.
- The demo must work from a phone on cellular via the live link before the video is recorded.
- Cut in the order the plan's cut list gives. Never cut the scenes the plan marks never-cut.
- Code lives in `src/`, gallery in `gallery/`, data fixtures in `data/`, evals in `evals/`, the web page in `web/`, submission assets (video script, README, diagram, blog post) in `submission/`. Run artifacts and scratch go to `_runs/<date>_<slug>/`.
- Delegation: the session lead (Fable) keeps design, judgment, and verdicts. Mechanical and bulk work (parsers, fixture generation, gallery rendering, web page scaffolding, eval harness, long runs) goes to Opus subagents via the Agent tool with model `opus` at high effort, several in parallel when independent, each with a bounded brief and file scope. Read their output before relying on it. Use Opus without restraint: fan out every independent piece at once, launch a second Opus agent to review and stress-test what the first one built (parsers against edge cases, the reader against every gallery item, the web page on a phone viewport), and re-launch on any defect. Budget is not a constraint on this build; result quality is the only measure.
- Commit only when asked. No AI attribution lines in commits.
- `STATUS.md` gets one line per state-changing turn (what changed, where it landed). It opens with "Where things live".

- Owner directives arrive in `OWNER_DIRECTIVES.md` in this folder; read it at every resume and before the ship phase; it overrides PLAN.md where they conflict.
