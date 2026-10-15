# Supplier side: gap review against Lucius AI

Date: 2026-10-15. Scope: the supplier side of Tender-Sverka only.
Sources: Lucius research (2026-10-14), our analysis of it, our build inventory (2026-10-15), spec sections 2 and 6 to 9.

Tags, as in the research:
- **CONFIRMED**: seen directly.
- **VENDOR**: Lucius claims it. Not verified. Written as "Lucius claims".
- **STRONG**: strong inference from several clues.
- **SPEC**: guess.
- **SUB**: vendor claim seen only in a subagent summary. Our analysis moved some VENDOR items to SUB.

---

## 1. Summary

Lucius is tender-first. It reads a tender, builds a requirement list with quotes and severity, and gives a verdict on the tender. Its bid check is narrow: a free checker that takes one pasted answer, and a paid list where the user sets each status by hand. Tender-Sverka already does what Lucius does not show: a finished, multi-document bid checked line by line. Quotes are verified in code on both sides, there are five statuses, fixes carry action types and impact, and documents are checked for validity, identity and attribution. We lack Lucius's entry points: no free checker, no example button, no tender-level verdict, and no printable or Word output. Our most serious gap is not a feature. In our own tests, 27 to 37 percent of items change status between two fresh runs. Fix that before any pitch. The top three UX wins are: a Load example button, a proof-vs-claim line in the split view, and a plain reason line per requirement.

Table count: **9 yes, 6 partly, 10 no** (25 rows).

---

## 2. Feature gap table

| # | Lucius feature / flow | Have it? | Decision | Why |
|---|---|---|---|---|
| 1 | Requirement map with verbatim quote and page (VENDOR) | **Yes.** Quote checked in code; source page; category | Keep | Lucius claims verbatim quotes. Ours are checked in code. |
| 2 | Severity (High/Med/Low) and one-line reason per requirement (VENDOR) | **Partly.** We have type (mandatory/scored/desirable) and consequence, but no short reason line | Improve | Users read the "why" at a glance. Build it from fields we already have, with no LLM call. |
| 3 | Disqualifiers inside the same list, as high severity (VENDOR) | **Yes.** Mandatory plus consequence "Rejection" | Keep | Same effect, and no separate list is needed. |
| 4 | Bid / Caveats / Skip verdict on the tender (VENDOR; can be null) | **No** | Skip | Needs a company profile first. Our bid verdict covers the need for now. |
| 5 | Tender risk score 0 to 100 (VENDOR; formula is SPEC) | **No** | Skip | A second number built from severity labels. Our % and bid verdict are the numbers users act on. |
| 6 | Free paste checker: one answer vs pasted requirements (CONFIRMED) | **No** | Skip | One answer skips document checks. Lucius's checker also gave lenient matches (CONFIRMED). Not worth a second pipeline. |
| 7 | Word add-in that checks a draft (VENDOR; one sentence, not tested) | **No** | Skip | Scope is unknown. Outside the build. |
| 8 | Full multi-document bid checked line by line | **Yes (ours).** Lucius: not found | Keep | Our main differentiator. |
| 9 | Statuses: 2 in checker (CONFIRMED), 3 set by hand in paid list (VENDOR) | **Yes.** 5 statuses, each with reason codes | Keep | Separates "claimed" from "proven". Adds "Needs review". |
| 10 | "Claimed but not proven" state | **Yes.** Not confirmed | Keep | Lucius has no such state. A bare claim counts as addressed (CONFIRMED). |
| 11 | Drafting answers from a bid library and company profile (VENDOR) | **No** | Skip | Drafting invites invented facts. Our fixes are template-based. |
| 12 | "[TO COMPLETE]" markers in drafts (VENDOR) | **No** | Skip | Follows from row 11. Our equivalent is the "still to collect" list. |
| 13 | Banner count of items without evidence (VENDOR) | **Partly.** "Mandatory met X of Y" and the to-collect list. No unproven count on the top bar | Improve | One line on the top bar: "N not confirmed, M needs review". Effort S. |
| 14 | Verdict names the items that decided it (VENDOR) | **Yes.** Top bar on the Comparison screen | Keep | Users trust a verdict they can trace. |
| 15 | Two-speed results: quick preview, then full run (VENDOR: under 1 min, then 4 to 10 min) | **Partly.** Live progress per stage; no early results | Improve | Show the checklist as soon as the tender stage ends. Partial bid results are deferred (section 5). |
| 16 | "Load NHS example" button on the checker (CONFIRMED) | **No** | Add | First result with zero setup. Ranked 1 in section 5. |
| 17 | Readiness self-check: 16 yes/no questions; Bid Viability Scorer (VENDOR) | **No** | Skip | Our checklist and bid check give the same answer from real files. A second flow is out of scope. |
| 18 | Tender search across 13 to 15 portals (VENDOR) | **No** | Skip | Portal scraping, terms risk and upkeep. Not in the spec. |
| 19 | Exports: Word, PDF, Excel, ZIP (research section 4) | **Partly.** XLSX with 6 sheets. Clarification letter as .txt. No PDF, Word or ZIP | Improve | Print stylesheet for the Summary (S). Word letter later (M). ZIP: skip. |
| 20 | Re-runs and draft history (VENDOR: 60-day reruns, history with restore) | **Yes.** Run history, trend, changed items | Keep | Stronger on what changed. Restore is not in our inventory. |
| 21 | Retention and privacy statements (VENDOR: retention 30 to 3,650 days; no DPA page found) | **No** | Add later | Needed before launch. Publish the statement only after the retention rule and DPA exist. |
| 22 | English-only drafting; "reads any language" (VENDOR) | **Partly.** RU/EN screens. UZ UI is in the spec but not built. No drafting | Keep | Our RU/UZ/EN lead is a claim until the Uzbek test runs. |
| 23 | Gemini only, global inference endpoint (VENDOR) | **Partly.** Claude models via Claude CLI. No region or DPA decided | Skip (do not copy) | A global endpoint is not acceptable for supplier files. Analysis default: one named region, no training. |
| 24 | Gaps first in the list (marketing page VENDOR; input order seen in test, CONFIRMED) | **Yes.** Comparison sorts deciding items first, then Does not meet, Not confirmed, Partial, Needs review, Meets; Summary is critical-first | Keep | Already done (checked in the browser). |
| 25 | Verified-quote tick and click-to-open source (VENDOR) | **Yes.** Split view highlights quotes on page text and page images | Keep | Same idea. Our quotes are checked in code. |

---

## 3. Where we are already better

| Area | Lucius | Ours (evidence: build inventory) |
|---|---|---|
| Bid package | No check of a finished multi-document bid found. Free checker takes one pasted answer (CONFIRMED) | Several bid packages per project, each checked line by line |
| Quotes | Checker "evidence" is a paraphrase. Wording changed between identical runs (CONFIRMED, Run 1 vs Run 3) | Verbatim quotes checked in code, on the tender and the bid |
| Statuses | 2 in checker: addressed or NOT FOUND (CONFIRMED). A wrong value and a missing item look the same | 5 statuses with reason codes |
| Fixes | Gap list only. No action type, no impact (CONFIRMED) | Action types, criticality and impact per item |
| Document checks | None found beyond certifications vs library (VENDOR, SUB) | Validity on the submission date, identity (name, STIR), document type |
| Attribution | Checker credited a partner's delivery as the bidder's (CONFIRMED, n=1) | Evidence about another organisation never counts |
| Tender scoring | None found | Scoring table checked against pass mark; inconsistency (e.g. 14 vs 15) sent as a question |
| Platform items | None found | Items such as e-signature (ЭЦП) and account balance are excluded from % and verdict until a human confirms |
| Checklist review | No documented review step (unknown, not absent) | Approve step, suggestions with Apply buttons, versions, audit log |
| Mandatory count | % shown with no mandatory-fail count (CONFIRMED) | "Mandatory met X of Y" next to the % |
| Languages | Drafts English only (VENDOR). No Uzbek or Russian found | RU and EN screens now; UZ in spec. A claim until measured |

---

## 4. Where Lucius is better, or we are missing something

| Gap | Evidence | Size |
|---|---|---|
| Status stability | 27 to 37 percent of items change status between two fresh runs, mostly Meets vs Not confirmed or Partial. Lucius's one repeat gave identical statuses on 8 items (CONFIRMED, n=1) | **Highest.** Quality work, not UX. Not in section 5 |
| Needs-review share | 11 to 20 percent of items land in "Needs review" | High. Heavy load for the reviewer |
| No free entry point | Lucius has a free checker that answers in 5.8 to 14.7 s with no sign-up (CONFIRMED) | High |
| Speed | About 24 min per 220-page test set, before parallelisation. Lucius claims under 1 min preview and 4 to 10 min full (VENDOR) | High |
| No tender-level view | No bid / no-bid verdict or tender risk score | Medium. Deliberate for now |
| No printable or Word output | Lucius lists Word, PDF, Excel and ZIP exports (research section 4) | Medium |
| No public trust page | Lucius publishes retention, subprocessors and certifications (VENDOR). We have none | Medium. Needed before launch |
| Word add-in | Lucius claims one (VENDOR, one sentence) | Low. Skipped |
| Drafting | Lucius claims drafting from a bid library (VENDOR) | Low. Deliberate skip |

---

## 5. Prioritised improvements for the remaining days

Scope: small UX wins only. Effort S = up to 2 hours. M = up to half a day. Impact is H or M.

| Rank | What | Why | Effort | Impact |
|---|---|---|---|---|
| **1 (recommended)** | **Load example button** on the Projects page. Loads the sample tender and bids (a sample set, marked as such) with their saved results, so it opens instantly | New users and demos reach a result with zero setup. Lucius does this (CONFIRMED). For any public deployment, swap in a fully fictional set | M | H |
| **2 (recommended)** | **Proof-vs-claim line in the split view.** Not confirmed shows "Your bid states this. No document proves it yet." Other statuses show a plain reason | Users see why an item is not green. Most status flips are proof vs claim, so this helps users read a status that may change | S | H |
| **3 (recommended)** | **Plain reason line per tender requirement**, built from type, consequence and proof document, e.g. "Mandatory, missing means rejection, proof: certificate" | Gives a Lucius-style severity and reason with no LLM call | S | H |
| 4 | **Verdict banner on the Projects list**: verdict plus top deciding item | Decision at a glance across projects. Not in the inventory | S | M |
| 5 | **Two-speed**: show the checklist as soon as the tender stage ends. Show bid progress per package | User can review the checklist while matching runs. Partial bid results are deferred: M and risky | S | M |
| 6 | **Count line on the top bar**: "N not confirmed, M needs review" | Shows the unproven count, not only "mandatory met X of Y" | S | M |
| 7 | **Deadline countdown** on the project page. Warning when under 3 days and any mandatory item is red | Missed deadlines are a common loss. No Lucius equivalent found | S | M |
| 8 | **Print stylesheet for the Summary** (browser PDF) | Managers want a paper or PDF summary | S | M |
| 9 | **Clarification letter as DOCX**, not .txt | Customers expect Word. Spec section 9 asks for DOCX | M | M |

Top 3 are recommended: 1, 2 and 3. (Gaps-first ordering was a candidate but is already built.)

Not ranked, because they are quality work and not UX: status stability (27 to 37 percent), needs-review share (11 to 20 percent), run time (about 24 min), and the Uzbek OCR and matching test (V1). These must happen anyway.

---

## 6. Explicitly skipped

| Feature | Why |
|---|---|
| Tender search across portals | Portal scraping, terms risk and upkeep. Not in the spec |
| Drafting answers from a bid library | Invites invented facts. Breaks our no-invented-facts rule |
| Word add-in draft check | A second client. Lucius's scope is unknown (VENDOR) |
| Free paste checker | One-answer checks skip document checks and repeat Lucius's lenient matching |
| Readiness self-check quiz | Duplicates the checklist built from real files |
| Tender-level bid/no-bid verdict and risk score | Needs a company profile. A second number with no validation |
| ZIP export and full Word report | Low value for the effort. XLSX covers the data |
| Gemini-only, global endpoint, English-only output | We do not copy these (analysis section 6) |
| Company document vault | Opt-in later (analysis section 8) |

---

## 7. Limits

- Lucius facts come from public pages and 3 runs of its free checker, on 8 requirements and one synthetic answer. Most paid features were not tested.
- Lucius's terms (section 5) restrict automated access. No further testing is planned.
- Our analysis moved some tags down (for example, the risk-score formula to SPEC, extraction variance to SUB).
- The build inventory describes features. It does not measure quality.
