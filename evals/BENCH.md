# Biddesk eval bench

Run `2026-09-14T07:50:37.616703-05:00` (America/Chicago). Every row below traces to a committed file: `data/bench/tiering_<slug>.json`, `data/snapshot/<slug>.json`, `gallery/index.json`, `gallery/cases/<slug>/<id>.json`, `data/attachments/<id>/extracted.json`, and the hand-marked list in `_runs/2026-09-13_phase3_donecheck/hand_marked/`.

Rebuild: `PYTHONIOENCODING=utf-8 python evals/run_evals.py --parts 1 2 3`.

## 1. Tiering expectations (deterministic, no model call)

Command: `python evals/run_evals.py --parts 1`. Tier 0 is re-run in code against every notice in the snapshot at the bench's own `as_of`, so the deadline rule decides the same way it did at bench time. Tier 0 is first-kill-wins: the expected rule id is the first owner rule that fires. Only tier 0 is asserted here; tier 1 is a model call and is not re-run.

| firm | as_of | must file by rule | pass | should surface | pass | unexpected filings | hand-checked |
|---|---|---|---|---|---|---|---|
| red-cedar | 2026-09-13T21:54:20-05:00 | 178 | 178 | 20 | 20 | 0 | 10 |
| sooner-systems | 2026-09-13T21:55:24-05:00 | 220 | 220 | 15 | 15 | 0 | 10 |
| plains-med | 2026-09-13T20:14:45-05:00 | 10 | 10 | 0 | 0 | 0 | 10 |

Result: **PASS** (408/408 rule filings, 35/35 surfacing notices clear of every rule). Ten filings per profile were read by hand against the notice fields; the ids and what settles each one are in `evals/expected_<slug>.json` under `hand_checked`.

## 2. Matrix completeness against a hand-marked list (deterministic)

Command: `python evals/run_evals.py --parts 2`. The phase-3 hand-marked list covers ten attachment pages, marked before the extractor was read. A statement counts as carried when a kept matrix row cites a requirement whose text contains it, or when the row's quote and the statement contain one another after whitespace and quote folding, on the same file and page. Three hand-marked notices produced no gallery card, so they carry no score.

| notice | firm | hand statements | carried | completeness | matrix rows |
|---|---|---|---|---|---|
| 4a580c56 | (no card) | 5 |  |  |  |
| 4f8dba02 | red-cedar | 8 | 8 | 1.0 | 36 |
| 54a51abc | sooner-systems | 5 | 0 | 0.0 | 29 |
| 5ad10c73 | sooner-systems | 5 | 4 | 0.8 | 37 |
| 6b21b73e | (no card) | 3 |  |  |  |
| 784d242f | (no card) | 11 |  |  |  |
| 907ad2ce | sooner-systems | 13 | 10 | 0.769 | 31 |
| a06d61e1 | red-cedar | 20 | 3 | 0.15 | 26 |
| e1d2da67 | red-cedar | 5 | 4 | 0.8 | 30 |
| e4ca493c | sooner-systems | 13 | 4 | 0.308 | 37 |

Total over the 7 scored cases: 33/69 = **0.478**. This is a coverage number against one human's marking of ten pages, not a claim about every attachment.

Most of the gap is the desk's cap, not a failed read: the matrix keeps 26-37 ranked rows out of every requirement the extractor found, so a hand-marked page the ranking never reached scores near zero by construction. `54a51abc` is that case: its 29 kept rows cite the 394-requirement PWS attachment, while the hand-marked page is the 5-requirement RFI description document (404 requirements extracted in all).

## 3. Matrix completeness, judge model (an opinion, not a measurement)

Command: `python evals/run_evals.py --parts 3`. One structured-output call per gallery case on `us.anthropic.claude-sonnet-4-6`, temperature 0. The judge sees the kept matrix rows and the case's extracted requirements capped at the 150 highest-ranked, ranked by `desk.select_requirements`: category priority (submission, evaluation, schedule, scope, staffing, compliance, other) then document order, which is the same ordering the desk hands its specialists. The score is the model's opinion of coverage; it is reported as such and is never mixed into the deterministic numbers above.

| notice | firm | score | matrix rows | reqs shown / total | calls | tokens in | tokens out | error |
|---|---|---|---|---|---|---|---|---|
| 23123dbc | red-cedar | 0.76 | 35 | 128 / 128 | 1 | 15781 | 1047 |  |
| 3d699772 | red-cedar | 0.72 | 22 | 72 / 72 | 1 | 9164 | 1074 |  |
| 4585b18b | red-cedar |  | 0 | 0 / 0 | 0 | 0 | 0 |  |
| 4f8dba02 | red-cedar | 0.72 | 36 | 109 / 109 | 1 | 13849 | 1354 |  |
| 605b8074 | red-cedar | 0.78 | 35 | 125 / 125 | 1 | 15319 | 876 |  |
| 6d5f0cc8 | red-cedar | 0.68 | 37 | 91 / 91 | 1 | 11725 | 846 |  |
| 6df14e55 | red-cedar | 0.72 | 35 | 93 / 93 | 1 | 12258 | 1191 |  |
| 808e070c | red-cedar | 0.62 | 27 | 70 / 70 | 1 | 9017 | 1299 |  |
| 81935751 | red-cedar | 0.72 | 39 | 130 / 130 | 1 | 15290 | 1404 |  |
| 83debc9c | red-cedar | 0.55 | 38 | 134 / 134 | 1 | 15622 | 1815 |  |
| 8918bdf8 | red-cedar | 0.62 | 31 | 150 / 279 | 1 | 17247 | 1265 |  |
| 954c262f | red-cedar | 0.42 | 31 | 150 / 331 | 1 | 16545 | 1336 |  |
| a06d61e1 | red-cedar | 0.42 | 26 | 150 / 298 | 1 | 19125 | 1417 |  |
| be4f7e41 | red-cedar | 0.85 | 20 | 66 / 66 | 1 | 8717 | 985 |  |
| bfe39928 | red-cedar | 0.72 | 36 | 138 / 138 | 1 | 15030 | 1419 |  |
| ccf5e965 | red-cedar | 0.62 | 32 | 150 / 449 | 1 | 18531 | 1429 |  |
| d698e029 | red-cedar | 0.62 | 38 | 142 / 142 | 1 | 17622 | 848 |  |
| d8c230fb | red-cedar | 1.0 | 5 | 5 / 5 | 1 | 1520 | 153 |  |
| e1d2da67 | red-cedar | 0.72 | 30 | 71 / 71 | 1 | 9524 | 1088 |  |
| fbaa93b9 | red-cedar | 0.72 | 36 | 150 / 207 | 1 | 17995 | 1565 |  |
| 07b8aa34 | sooner-systems | 0.32 | 37 | 150 / 547 | 1 | 18797 | 882 |  |
| 475ec029 | sooner-systems | 0.82 | 38 | 59 / 59 | 1 | 8053 | 446 |  |
| 48a0b93b | sooner-systems | 0.74 | 23 | 35 / 35 | 1 | 6233 | 1004 |  |
| 53912490 | sooner-systems | 0.28 | 32 | 147 / 147 | 1 | 16672 | 1626 |  |
| 54a51abc | sooner-systems | 0.38 | 29 | 150 / 404 | 1 | 18778 | 882 |  |
| 5ad10c73 | sooner-systems | 0.52 | 37 | 150 / 183 | 1 | 20009 | 1864 |  |
| 633f42f2 | sooner-systems | 0.72 | 38 | 87 / 87 | 1 | 12352 | 1123 |  |
| 907ad2ce | sooner-systems | 0.63 | 31 | 50 / 50 | 1 | 8188 | 1346 |  |
| a707b8a1 | sooner-systems | 0.32 | 35 | 150 / 725 | 1 | 16471 | 942 |  |
| a9ef3e73 | sooner-systems | 0.82 | 30 | 57 / 57 | 1 | 8625 | 934 |  |
| bb76a0d2 | sooner-systems | 0.87 | 14 | 16 / 16 | 1 | 3380 | 664 |  |
| c4480de5 | sooner-systems | 0.38 | 33 | 150 / 454 | 1 | 18494 | 858 |  |
| e4ca493c | sooner-systems | 0.78 | 37 | 150 / 242 | 1 | 19621 | 1481 |  |
| e6f67857 | sooner-systems | 0.72 | 34 | 150 / 202 | 1 | 20732 | 1474 |  |
| ee15f287 | sooner-systems | 0.92 | 35 | 38 / 38 | 1 | 7055 | 1436 |  |

Scored 34 of 35 cases; mean score **0.653**. Cost: 34 calls, 463341 tokens in, 39373 out, all on `us.anthropic.claude-sonnet-4-6`.

## What is and is not proven here

- Parts 1 and 2 are deterministic: no model runs, and re-running them on the committed files reproduces the numbers exactly.
- Part 3 is one model's opinion of another model's output. It is useful as a relative signal across cases and worthless as ground truth.
- The hand-marked list is ten attachment pages marked by one person. It is the only human ground truth in this repo.

