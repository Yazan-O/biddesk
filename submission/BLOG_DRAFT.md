# Agents for Humans: an agent that reads SAM.gov every morning

Federal agencies post every solicitation on SAM.gov. If you run a 20-person janitorial firm in Oklahoma City, that feed is your pipeline, and it is a reading job: screen the notices every morning, build a compliance matrix out of the attachments, put two deadlines per bid on a calendar. Large firms employ that person. Owner-operated firms do the reading at 10 p.m. and miss things.

The law sets a goal of 23 percent of federal prime contract dollars going to small businesses (15 U.S.C. 644(g)(1)(A)(i)). The firms that miss out are often not the ones that cannot do the work, but the ones that cannot read the feed.

I built Biddesk for the AWS Agents for Humans hackathon: a background agent on Strands and Amazon Bedrock that reads SAM.gov, applies the owner's rules first, and interrupts a human with one thing, a bid or no-bid card, when a human has to decide.

## Start by not calling the model

The first design decision was where the model is not allowed to go.

A firm's profile carries its NAICS codes, set-aside status, past performance, and the owner's rules in plain English: "no work over 200 miles from Oklahoma City", "never bid under ten days to close". Those rules are Python, not prompt text, and they run before any model call, in tier 0. Tier 1 is one Haiku 4.5 call with structured output asking one narrow question: is this the kind of work this firm does, and where is the work performed. Tier 2 is the full desk.

On one real 30-day window of SAM.gov, notices posted 2026-08-14 to 2026-09-13, across three firm profiles, that is 608 notices in. The owners' rules filed 178 of 231, 220 of 367, and 10 of 10 before any model call. Of the 200 survivors, one Haiku call each filed 161 more, and 39 reached the full desk. 6.4 percent of the feed cost a desk run, and showing an owner how often the agent did not need a model is the cheapest trust you can buy.

## Geography is not a language problem

The distance rule looked like the easy one and was not. SAM.gov's place of performance is sometimes a ZIP, sometimes a city, sometimes a state, sometimes junk ("0", in Utah), sometimes nothing. So distance uses 41,197 real GeoNames ZIP centroids, every distance carries a precision label, and a state-precision distance must exceed twice the owner's limit before it files anything. An unknown fact never fires a rule: that is the difference between an agent that is quiet and one that is quietly wrong.

## Three specialists in parallel, then one desk

For a surviving notice, three Strands specialists run as parallel entry nodes on a `Graph`. The graph setup in `desk.py` (abridged):

```python
from strands.multiagent.graph import GraphBuilder, GraphState
from strands.multiagent.base import MultiAgentBase, MultiAgentResult, NodeResult, Status

def build_graph(specialists: dict[str, SpecialistNode], merge: DeskMerge):
    builder = GraphBuilder()
    for name, node in specialists.items():
        builder.add_node(node, name)
    builder.add_node(merge, "desk_merge")
    names = list(specialists)
    condition = all_dependencies_complete(names)
    for name in names:
        builder.add_edge(name, "desk_merge", condition=condition)
    for name in names:
        builder.set_entry_point(name)
    builder.set_max_node_executions(len(names) + 1)
    return builder.build()
```

One specialist picks the requirements the bid turns on, one builds the compliance matrix against the firm's profile, one extracts every date with the sentence it came from. A desk agent then writes the Decision Card.

Two things I learned in the SDK. `Graph` runs entry nodes in parallel while `Swarm` does not, which is the reason to use it. And `Graph` is fail-fast for plain `Agent` nodes: an exception inside a node takes the whole graph with it and leaves no result to inspect. Wrapping each specialist in a `MultiAgentBase` node fixes that, so one can fail and the desk still writes a card saying so.

The attachment reader makes no model calls: download, extract per page, lift binding requirement sentences with a file and a page number. On the snapshot, 1,328 files across 361 notices, 15,622 pages, 52,483 requirement sentences. 118 of those files are scanned images with no text to lift, which the reader labels rather than guesses at.

## The bar is a function, not a better model

Every matrix row quotes the sentence it came from. The validator in `desk.py` (abridged):

```python
def validate_matrix(rows, requirements):
    by_id = {r.id: r for r in requirements}
    kept, dropped = [], []
    for row in rows:
        if not _norm(row.quote):
            drop(row, "empty_quote"); continue
        req = by_id.get(row.requirement_id)
        if req is None:
            drop(row, "unknown_requirement"); continue
        if _norm(row.quote) not in _norm(req.text):
            drop(row, "not_verbatim"); continue
        if row.source_file != req.source_file or int(row.page) != int(req.page):
            drop(row, "wrong_source"); continue
        kept.append(row)
    return kept, dropped
```

A row is kept only when the quote is a verbatim substring of the requirement it cites and the file and page match. Rows that fail are dropped and counted, never repaired: 1,072 kept and 125 dropped across 35 cases. The same validator runs on the Sonnet 4.6 primary and the Haiku fallback, so the cheap model gets no easier exam, and on the two cases nearest the bar the recommendation agreed on both models.

The fit score is worth saying plainly. It is computed in code from the rows that survived validation in that run, and that row set moves: the same notice on the same model scored 0.64 over 23 rows and 0.56 over 35. The card reads "fit N over K validated rows", and the number is not comparable across runs or models. The probe I wrote to assert otherwise was retired, not loosened.

The eval bench keeps the same separation. Its two deterministic parts run with no model call: the tier-0 rules re-run against every notice and match on 408 of 408 filings and 35 of 35 surfacing, and ten hand-marked attachment pages find 33 of 69 "shall" statements in the kept rows. The third part is a judge model's opinion, reported as such and never folded into the deterministic table.

## The refusal

Biddesk drafts. The owner submits. That is a product rule, so it is code. The guard hook in `hooks.py` (abridged):

```python
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

class NoSubmitGuard(HookProvider):
    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        tool_use = getattr(event, "tool_use", None) or {}
        name = tool_use.get("name", "")
        reason = deny_reason(name, tool_use.get("input", {}), self.deny_tools)
        if reason is None:
            return
        event.cancel_tool = DENIAL_MESSAGE.format(tool=name, reason=reason)
        self.ledger.write(notice_id=_notice_id(tool_use.get("input", {})),
                          action="denied_tool", why=f"{name}: {reason}",
                          evidence=_evidence(tool_use.get("input", {})), tier=0, undo=None)
```

The same rule exists as an `InterventionHandler` with `on_error="deny"`, so a crash inside the matcher blocks the call instead of letting it through. Ask the desk to submit an offer and you get a `denied_tool` ledger row.

Every silent action is a ledger row holding what, why, when, and the undo, behind a ten-minute veto window. An undo appends a row; nothing is rewritten.

## Deploying on AgentCore Runtime

The desk runs on Bedrock AgentCore Runtime in `us-east-1`, direct code deployment (zip, `PYTHON_3_13`, ARM64). CloudFront and S3 serve the static gallery; a Lambda Function URL handles CORS and forwards to the Runtime over SigV4. The page renders first from committed gallery JSON, so nothing waits for a cold start. Live actions replace the cached panel with the real result, and when SAM.gov rate-limits the pull, the page says so instead of pretending.

## What I would tell another builder

Put the deterministic layer first and its counter on screen. Keep the rules in code, where they cannot be argued with. Write one validator both models must clear. And when the upstream API rate-limits you mid-demo, which it did, show the cached banner with its date instead of pretending, then find the second real route (the unkeyed search the sam.gov site itself uses) and let the page name which route answered. Honest degradation reads as competence.

Repo, MIT licensed: (not published yet). Live demo: <https://dpnzgd4gtjs45.cloudfront.net/web/>.
