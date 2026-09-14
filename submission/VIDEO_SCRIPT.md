# Biddesk film: shot list (50 seconds, hard cap)

Owner directive 2026-09-14 03:05 CT: one film per product, at most 50 s. A separate video session (`../video/`)
owns the Flow (Veo) prompts, screen capture and editing; this file is the shot list, the tags, the voice lines,
and the sourced numbers. The previous 5-minute, 11-scene script is archived at
`archive/2026-09-13_video_script_5min/VIDEO_SCRIPT.md`.

Tags: **GENERATED** = cinematic footage from Flow (no on-screen text, logos, readable documents or dollar amounts;
Oklahoma-plausible, ordinary neighbors and businesses). **REAL** = screen capture of the live page with the live
link visible in the frame, recorded from the deployed URL. Every product moment is REAL.

The three product moments that matter most, in order: the tier counters after a live SAM.gov pull, the Decision
Card whose compliance matrix quotes the solicitation verbatim and whose owner taps "bid", and the refusal.

**Number rule.** A spoken number carries its source in the table below. `[N]` means unmeasured today and is not
spoken until its source cell is filled. Nothing here claims the 12-month base-period rule fired (`STATUS.md`
Deviations). The live pull can be recorded now: while the keyed SAM.gov API is throttled (reset 2026-09-15
00:00 UTC, `data/raw/throttle.json`) the runtime takes the unkeyed sam.gov search route and the page says so
under the counts ("via the public sam.gov route (API key throttled), last 7 days"); measured on the live link
2026-09-14 08:41 CT it answered in 23 s. A CACHED banner on screen is still not the shot to ship.

| # | Tag | Time | Shot | Voice-over |
|---|---|---|---|---|
| 1 | GENERATED | 00:00-00:06 | Dawn in a small Oklahoma City facility-services yard: a woman in a work jacket unlocks a truck, a laptop bag on the seat. Slow push-in, warm low sun. | "A small contractor is supposed to read every federal solicitation, every morning." |
| 2 | REAL | 00:06-00:16 | Phone opened from the QR code, live URL in frame. Fictional-firm label legible: "Fictional firm. Real SAM.gov notices." The overnight sieve is on screen (231 / 178 / 31 / 22, the 30-day snapshot). Tap "Run the pull live"; after about 25 s the sieve refills with the last 7 days and the note reads "live pull at <time> via the public sam.gov route (API key throttled), last 7 days". | "Biddesk reads SAM.gov every night. Thirty days for Red Cedar: 231 real notices in its two trade codes. 178 die on the owner's own rules in code, no model call. 31 get a cheap read. 22 reach the desk. And it pulls live: this week's notices, counted on screen." |
| 3 | REAL | 00:16-00:32 | Tap the bid card (notice `ee15f287`, Sooner Systems, "RFI: Air Force Enterprise Rapid Operations Support (AEROS)"). Scroll the compliance matrix: status chip, verbatim quote in mono with `file p<page>`, the firm's answer. Hold on one quote. Then the Decision Card: three reasons each way, each ending in its source. Finger taps "Respond to this RFI"; the ledger row appears. | "The desk reads the actual attachments. Every quote on this card is verbatim from the solicitation, or the code throws the row out. Three reasons for, three against, each with its source. The owner decides. Biddesk drafts; it never submits." |
| 4 | REAL | 00:32-00:42 | Plains Med page, "What the desk refuses to do": filed rows with their rule. Tap "Ask the desk to submit this offer"; after about 15 s the live row appears: `denied_tool`, "guard hook · sam_submit_offer: tool 'sam_submit_offer' is on the no-submit deny list", the model's own first sentence, "nothing was sent". | "Ask it to submit anyway and a hook stops the tool before it runs, and writes the refusal to the ledger. Nothing leaves without the owner." |
| 5 | GENERATED | 00:42-00:50 | Same yard, mid-morning. The woman closes the laptop, tosses the bag in the cab, drives off. Wide, steady, warm. | "One card per real candidate. Everything else stays quiet. Biddesk, built on Strands and Bedrock AgentCore." |

Runtime 50 s. Cut order if it runs long: shorten shot 5 to 5 s, then shot 1 to 4 s. Shots 2, 3 and 4 are never cut.

Production notes. Shot 2 is Red Cedar (the janitorial firm), shot 3 is the Sooner Systems RFI card and shot 4 is Plains
Med, so the capture crosses three firm pages; open each from the firm switcher and keep the live URL in frame. `ee15f287`
is a Sources Sought, which is a market survey: responding means sending a capability statement, not a proposal, the card
and the button say so, and no voice line calls it a proposal. Its fit reads "fit 0.56 over 35 validated rows" on screen;
do not narrate the fit number, it is a within-run figure and not comparable across runs.

## Sources for spoken numbers

| Spoken | Value | Source |
|---|---|---|
| notices pulled (Red Cedar, 30-day window) | 231 | `data/bench/tiering_red-cedar.json` `counts.total`, read 2026-09-14; the live pull on recording day must show the same window or the count is read off the screen |
| filed by rules | 178 | same file, `counts.tier0_filed` (distance 98, wrong notice type 43, ineligible set-aside 37) |
| cheap model read | 31 | same file, `counts.tier1_filed` (out of scope 9, too far once the model resolved the place 22) |
| reached the desk | 22 | same file, `counts.tier2_sent` |
| days to close on the refusal notice | `[N-days]` | `gallery/index.json` plains-med refusal case `days_to_close`, computed by `tiering.days_to_close` from the notice's `response_deadline`; the notice id and its `ui_link` sit next to it. **Still a placeholder on purpose:** the refusal notice `8b795e67` ("Therapeutic Diabetic Shoe Program", Combined Synopsis/Solicitation, NAICS 621399) closes 2026-09-14 10:00 CT, so on recording day it is already closed. Either re-pull plains-med after the SAM.gov key resets 2026-09-15 00:00 UTC and read the new number here, or let the shot read the days off the screen and cut the spoken number. |
| "verbatim or the row is thrown out" | rule | `desk.validate_matrix` (whitespace-normalized, case-sensitive substring of the cited requirement); dropped rows listed in each case file's `matrix_dropped_rows` |
| "a hook stops the tool before it runs" | rule | `hooks.NoSubmitGuard` on `BeforeToolCallEvent`; the `denied_tool` ledger row in `data/ledger/plains-med.jsonl` carries `model_id` |

The bid case in shot 3 is a real notice the desk recommended `bid` on in the 30-day window (owner directive: widen before
any fallback). It is **`ee15f287`** (`ee15f287bd3049349277a9bd31c206df`), Sooner Systems, "RFI: Air Force Enterprise Rapid
Operations Support (AEROS)", notice type Sources Sought, responses due 2026-09-30 17:00 ET, still open, `bid` on all three
runs of it (two Sonnet, one forced Haiku). The fallback plan is no longer needed and is kept only in case the card is
unavailable on recording day: the closest no-bid card where the owner overrides to "bid" with a reason, recorded in the
ledger as an owner decision, with the voice line "The owner can overrule it, on the record."

One other `bid` exists in the gallery, `d8c230fb` (Red Cedar, Robert S. Kerr janitorial). Do not film it: the notice closed
2026-09-10, its matrix is 5 rows reading 0 met / 1 partial / 4 unknown, and the page labels it closed with the answer
buttons disabled.
