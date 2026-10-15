# Tender-Sverka: customer side decisions

Date: 2026-10-15 · Status: draft for review · Reference: [research/doubletapp-buyer-report.md](research/doubletapp-buyer-report.md) · Engine: supplier side, as built (see [ARCHITECTURE.md](ARCHITECTURE.md))

Tags follow the research report: **CONF** = seen in a primary source · **VENDOR** = Doubletapp claims it, not checked · **STRONG** = strong inference · **SPEC** = speculation · **SUB** = reported by a subagent, not re-checked.

## 1. Summary

Customer mode is one tender plus N bid packages, all checked against one approved checklist, then compared side by side. Doubletapp Tender AI does a similar job, but an LLM does almost every step: it builds the checklist, judges each bidder, ranks the bidders and recommends sign, shortlist or reject. Its described design has no checklist approval step, no quote check, no numeric check in code and no ranking formula. We copy its overall chain and its committee principle (the committee decides; a rejection needs a quote and a failed mandatory item). Everything else we keep from our spec. The new work is a comparison layer on top of the existing engine: a requirement-by-bidder matrix, a per-bidder summary with a code-computed ranking, and committee wording. Each bidder runs through the same matching and scoring code as a supplier bid. Estimated effort: about 2 days.

## 2. Doubletapp vs our spec: decision per item

| # | Doubletapp (source, confidence) | What our spec asks | Decision | Why (one line) |
|---|---|---|---|---|
| 1 | Pipeline: parse, LLM checklist, one LLM call per requirement × bidder, LLM ranking, committee report (CONF text, VENDOR claims) | Same chain, with checks in code (§6–§9) | Copy the chain | Same shape as our engine; Doubletapp claims it works in market |
| 2 | Checklist built by LLM; no approval step found. Case page says "without human involvement" (CONF) | Checklist approved before comparison (§4.3) | Improve | Our engine already freezes an approved ChecklistVersion; all bidders use it |
| 3 | One LLM call per requirement × bidder, bidder documents in a cached prompt prefix (VENDOR; prefix detail STRONG) | Candidate pages per requirement, capped (§4.2 stage 8) | Improve | Smaller context per call; we retrieve pages instead of sending whole bids |
| 4 | Three statuses: meets / doesn't meet / insufficient data (CONF text, VENDOR claim). "Insufficient data" probably means "need more information" (STRONG) | Five statuses (§6) | Improve | Separates "claimed" from "proved" and "missing" from "wrong value" |
| 5 | Quote and document link per verdict. No check that the quote exists in the source (VENDOR) | Verbatim quote with page, verified by code (§10) | Improve | An unverifiable quote weakens the appeal argument the author himself makes |
| 6 | LLM ranks bidders and recommends sign / shortlist / reject. No formula, weights or tie rule found (VENDOR) | Score and ranking in code (§7, §9) | Improve | Same inputs give the same order; auditable |
| 7 | Author advice: "others can be discarded right away" (VENDOR, Habr comment) | Rejection needs a failed mandatory item, a quote and committee confirmation | Skip | Rejection grounds are legal; we never reject automatically |
| 8 | Committee keeps the rejection decision; a flagged rejection needs a quote (author reply on Habr, CONF text) | Advisory output; committee decides (§13) | Copy | Same principle; we also record the committee's own decision |
| 9 | "Documents to request" list only; no letters drafted (VENDOR) | Still-to-collect list (§8) and clarification letter | Improve | Per-bidder list in committee wording; draft letter to bidders is later (§4.4) |
| 10 | Ranking shown; no requirement × bidder matrix mentioned (research, workstream C) | Requirement × bidder matrix with filters (§9) | Copy the idea, add the matrix | Committee needs to see one requirement across all bidders |
| 11 | SaaS action audit, on-prem log control (VENDOR). No versioning or re-evaluation found | Audit log, versioned runs, re-check (§5, §8) | Improve | Committee needs a frozen snapshot per decision and a diff when bids change |
| 12 | Expert gold set: 93% extraction, 92% compliance match (VENDOR). No test-set size given | Acceptance on a gold set, 100% targets (§12) | Copy | We need a gold set too; their figures cannot be compared with ours |
| 13 | Integrations claimed: EIS, 1C, SAP, BI tools (VENDOR) | REST API now; TenderZone later (§11) | Skip (MVP) | Not needed for the first customer demo; API already exists |
| 14 | On-prem with open-weight models via vLLM; SaaS on Cloud.ru (VENDOR) | Claude via the Claude CLI (demo) or Anthropic API (production); no on-prem yet (ARCHITECTURE §6.2) | Skip (MVP) | Data-residency answer is still open (spec §13) |
| 15 | EIS and unreliable-supplier registry checks are a manual step (VENDOR) | Registry checks in phase 2 (§5) | Skip (MVP) | Phase 2 in spec |
| 16 | Generic 152-FZ policy for the website; no policy for the tender product. Claims 152-FZ compliance on Cloud.ru (CONF / VENDOR) | Isolation, retention, no training on customer data (§10) | Improve | We write our data rules as testable requirements, not marketing claims |
| 17 | Pricing not published; sales through demo and pilot (VENDOR). Bidder-side templates on landing page (CONF) | Not in spec | Skip | Not part of this plan |

## 3. Where we do better, and where Doubletapp is ahead

### 3.1 Where Doubletapp is weaker than our spec

| Area | Doubletapp (as described) | Our approach | Effect for the committee |
|---|---|---|---|
| Status scale | 3 statuses | 5 statuses plus reason codes | "Not confirmed" is not the same as "Does not meet" |
| Quote check | None found | Quotes checked by code on both tender and bid side; unverified ones are dropped | Every shown quote exists on the cited page |
| Numbers | Not stated; the LLM judges everything | Code parses value and unit ("2,5 kW" = "2500 W") and compares | Numeric verdicts do not depend on the model |
| Ranking | LLM, no formula | Code: verdict group, then tender points, then % (§4.3) | Same inputs, same order; ties stay tied |
| Tender points vs pass mark | Not described | Scoring table read from the tender; points shown as min–max vs pass mark; a mismatch (test tender: criteria add to 14, stated max 15) becomes a question | Committee sees whether a bidder can pass |
| Deciding items | Not described | Each verdict lists the items that decided it, with quotes | Committee sees the reason, not just the rank |
| Mandatory failures | Not described as a separate flag | Counter "X of Y mandatory met" and a "Subject to rejection" block | A high % cannot hide a missing mandatory item |
| Checklist approval | None found | Approve step; frozen version; every bidder uses it | Fairness: same checklist for all |
| Attribution and documents | Claims a check that experience matches the subject; cannot check whether contracts are real; registries manual (VENDOR) | Attribution (a partner's work does not count), document type, validity vs deadline, identity across documents | Catches wrong-company and wrong-document evidence |
| Clarifications | Document list | Per-bidder list in committee wording; contradictions in the tender become questions for the customer | Committee gets both sides' gaps |
| Reproducibility | Not described | Cache, pinned model and prompt versions, a "reproducible" flag, diff by item | A re-run can be explained |
| Unreadable pages | Not described | Unreadable pages block "absent"; the pages searched are shown | "Not found" is always "not found in pages X–Y" |

### 3.2 Where Doubletapp is ahead (honest view)

| Area | Doubletapp | Us today | Note |
|---|---|---|---|
| Market presence | Named delivery cases (workspace.ru, RUWARD) (CONF) | Prototype, no users | Their case pages are a sales advantage we do not have |
| Published accuracy | 93% / 92% on an expert gold set (VENDOR) | No gold set yet; our target is 100% | Their numbers have no test-set size, so they are not proof either |
| Deployment | On-prem in about 5 days on Ubuntu or Astra Linux (VENDOR) | Claude CLI / Anthropic API; no on-prem | Needed for some buyers |
| Integrations | EIS, 1C, SAP, BI (VENDOR) | None | |
| Speed | 5–7 min for 50 pages (VENDOR) | About 24 min for 220 pages before parallel work (about 5.5 min per 50 pages) | Roughly equal, not a differentiator yet |
| Simplicity | Three statuses are easy to read | Five statuses need the colour legend | Mitigated by short plain-word reasons |
| Holistic judgement | LLM ranking can weigh things a formula cannot | Ranking stays in code; LLM judges single requirements only | We give up flexibility for reproducibility |

## 4. Customer-side MVP

### 4.1 Rules that apply to all bidders

- One approved ChecklistVersion per project. Any checklist change creates a new version and re-runs all bidders.
- Each bidder is a normal bid Run on that version. No customer-only matching logic.
- All bidders in one check use the same model and prompt versions (Run.model_config). A run that used a fallback model is marked not reproducible, and the matrix shows a warning.
- Verdicts, points and % come from the same scoring code as supplier mode.
- Nothing is awarded or rejected by the system. Every customer-side output is labelled "Advisory: the evaluation committee decides".

### 4.2 In MVP (about 2 days, one developer; starts after the supplier side, ARCHITECTURE §12 day 6)

| # | Feature | Built on | Rough effort |
|---|---|---|---|
| 1 | Project with one tender package and N bid packages; bidder name and STIR per bid | Package.role = bid (existing data model) | 0.2 d |
| 2 | Upload screen: "Add bidder" button, one drop zone per bidder | Existing upload screen | 0.2 d |
| 3 | "Check all bidders": N bid runs against the same approved version | Existing runner and run endpoint | 0.2 d |
| 4 | Matrix screen with scope tabs (Goods / Bidder) and filters: only mandatory, only differences, only problems | Comparison screen and status chips | 0.5 d |
| 5 | Click a matrix cell: split view with a bidder switcher | Existing split view | 0.2 d |
| 6 | Per-bidder summary and ranking table; "Subject to rejection" block with deciding items | scoring.py, Summary screen | 0.3 d |
| 7 | Committee wording (strings and verdict labels, §6) | i18n files | 0.1 d |
| 8 | Committee decision per bidder: free text, logged in the audit log | AuditLog | 0.1 d |
| 9 | XLSX export of the matrix and per-bidder summary; no personal-name columns | report.py | 0.2 d |

If time runs short, cut 5 and 8 first. Keep 3, 4, 6 and 9.

### 4.3 Ranking rule (proposed; computed in code, function `rank_bidders()` in scoring.py)

| Step | Rule | Why |
|---|---|---|
| 1 | Group by verdict: "Meets all mandatory" → "Open points" → "Subject to rejection". Same verdict rules as the supplier side (ARCHITECTURE §7.3). | A bid with a missing mandatory item cannot outrank a complete bid, however high its % (spec §7). |
| 2 | Inside a group, by tender points, lower bound (points min), higher first. Platform-scored parts such as price are not in the number. If the tender has no scoring table, skip this step. | The tender's own scoring decides when it exists. The lower bound counts only points we can show. |
| 3 | Then by compliance %, lower bound (S_min), higher first. | Same conservative logic as the forecast. |
| 4 | Equal on steps 2 and 3 → same rank (1, 1, 3). No other tie-breaker. | The commission sets its own criteria (ZRU-684 Art. 20, SUB). We do not invent a tie rule. |
| 5 | If a bidder's range overlaps the next bidder's on step 2 or 3, show "order not certain" on both. | Ranges can flip the order between runs (§7, risk 1). |
| 6 | "Subject to rejection" bidders are listed last, with no rank number, and with their deciding items. | They are not compared with eligible bidders; the committee decides. |

Label on the output: "Preliminary ranking. Advisory: the evaluation committee decides."

Why code and not the LLM: the same inputs always give the same order, and anyone can check the rule. Doubletapp ranks with an LLM and describes no formula (VENDOR).

### 4.4 Later (not in MVP)

| Item | Why later |
|---|---|
| Draft request to each bidder (text or DOCX) | Needs a legal check of what a committee may ask a bidder (ZRU-684 Art. 41 covers customer answers, SUB) |
| Registry checks: TIN, unreliable-supplier register, licences | Phase 2 in spec §5 |
| Version history screen per bidder | Snapshots already stored; screen later |
| Voting, signatures, committee protocol | Commission rules unverified (ZRU-684 Art. 20, SUB) |
| Price and platform-scored parts in the ranking | Price rules are not defined yet |
| Tender amendments that update the checklist | ARCHITECTURE §13 |
| Uzbek UI, PDF and DOCX exports for the customer | ARCHITECTURE §13 |
| EIS, TenderZone and other integrations; on-prem | Spec §11, §13 |
| Project chat ("why is U-007 red for bidder 2?") | ARCHITECTURE §13 |

## 5. Screens in plain words

### 5.1 Bidder comparison (matrix)

| Area | What it shows |
|---|---|
| Top bar | Project, lot, deadline; banner "Advisory: the evaluation committee decides"; per bidder: count of red, yellow, orange and grey cells |
| Filter bar | Scope tabs Goods / Bidder; category list; toggles "Only mandatory", "Only differences" (bidders do not all have the same status), "Only problems" (any bidder Does not meet, Not confirmed or Needs review) |
| First column (sticky) | Requirement code (T-014 / U-007), requirement name, type chip (Mandatory / Scored / Desirable) |
| One column per bidder, header | Bidder name; verdict badge; compliance % with range; tender points X vs pass mark; mandatory met X of Y |
| Body | Rows grouped by scope, then category. Each cell: status chip in the supplier colours (green Meets, yellow Partial, orange Not confirmed, red Does not meet, grey Needs review) and a 3–6 word reason; full reason on hover |
| Narrow screens | First column stays fixed; bidders scroll sideways inside the table; the page does not scroll sideways |

Click any cell to open the split view (5.3).

### 5.2 Ranking and summary per bidder

| Column | Content |
|---|---|
| Rank | From §4.3; "–" for subject to rejection |
| Bidder | Name and STIR (if known) |
| Verdict | Badge in committee wording (§6) |
| Points | Min–max vs pass mark; "platform-scored parts not included" note when relevant |
| Compliance % | Range; "order not certain" flag when it overlaps the next bidder |
| Mandatory met | X of Y |
| Counts | Red / yellow / orange / grey |
| Deciding items | Requirement codes, each with its quote and page |
| Committee decision | Free-text field with the member's name and date |

Below the table: the "Subject to rejection" block, one entry per bidder with its deciding items and quotes.

Clicking a bidder opens that bidder's summary, in the supplier Summary layout: one-sentence conclusion, critical items first, then score-reducing items, risks, and the action plan renamed "Observations for the committee". The "still to collect" list becomes "Not provided by bidder".

### 5.3 Split view from a matrix cell

| Area | What it shows |
|---|---|
| Header | Requirement code and name; status chip; reason in plain words; bidder switcher (tabs or previous/next arrows) |
| Switcher | Keeps the same requirement and moves to another bidder, so one item can be compared across bidders |
| Left panel | Tender quote on its page image, highlighted; page number |
| Right panel | This bidder's evidence quotes on their page images, highlighted; document name and page; for "absent", the list of pages searched |
| Bottom | Short rationale; "Committee override" with a required comment (logged with the member's name) |

## 6. Wording rules for committee mode

| Supplier wording (current UI) | Committee wording |
|---|---|
| Add a document confirming X | Bidder did not provide a document confirming X. |
| Declared, not confirmed | Bidder states X; no supporting document in the package. |
| Rejection risk | Does not meet mandatory requirements — subject to rejection by the committee. |
| Ready to submit | Meets all mandatory requirements (advisory). |
| Needs revision | Open points for the committee. |
| Fix: replace the goods / correct the data | Bid states X; tender requires Y. |
| Not fixable | Committee to assess. |
| Forecast: up to N% if fixed | Removed. |
| Impact +6% | Removed. |
| Action plan / recommendation | Observations for the committee. |
| Still to collect (for you) | Not provided by bidder. |
| Your bid, you | The bidder, the bid. |
| Strengths of the bid | Exceeds requirement: X. |

Rules:
- Never say a bidder "will be rejected". Say "subject to rejection by the committee".
- Never give advice to a bidder in committee outputs.
- Every statement points to a quote and a page, or says "not found in pages X–Y".
- Status chips and colours are the same as the supplier side. Only the text changes.

## 7. Risks and open questions

### 7.1 Risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | Run-to-run variance. On the supplier side, 27–37% of statuses differ between two fresh runs (mostly Meets vs Not confirmed or Partial). In customer mode a flip can change the order. | Reuse the approved checklist (no re-extraction per bidder); cache keyed by inputs; code-defined proof rule (Meets on a mandatory item needs a strong verified quote); "order not certain" flag; test each bidder twice on the gold set before use; the committee sees a frozen snapshot, and a re-run shows a diff |
| 2 | Bidder personal data. Staff lists, contracts with individuals and tax certificates; all committee members see all bidders. | Matrix and XLSX show statuses and short reasons only, no personal names; access by project role; retention setting; lawyer check (ARCHITECTURE §14) |
| 3 | Fairness. A checklist change in the middle of an evaluation favours some bidders. | Frozen ChecklistVersion; any change creates a new version and re-runs all bidders; diff shown |
| 4 | Committee sign-off. Unclear who signs and what is recorded. | MVP: free-text decision per bidder plus audit log. Voting and protocol later. Commission: odd number, majority (ZRU-684 Art. 20, SUB) |
| 5 | Legal limits. Unknown whether a committee may ask bidders for clarification, and what the tie rule is. | Ask a lawyer before any bidder letter is built. Until then no bidder letters. Re-verify ZRU-684 on lex.uz (research §11) |
| 6 | Ranking may look like the award. | Label "non-price points only" where price is excluded; no "winner" wording |
| 7 | Many "Needs review" items make the order weak. Measured share on the supplier side: 11–20%. | Show the count; "order not certain" flag; committee reviews grey cells first |
| 8 | Test data is thin: one tender and two bids. | Ranking logic tested with unit tests in scoring.py; ask for at least three real, anonymised bids |
| 9 | Doubletapp sells a ranking, so buyers may expect one. | Show the ranking as preliminary, with ranges and flags, not as a verdict |

### 7.2 Open questions for the user

| # | Question | Our proposal |
|---|---|---|
| 1 | Is a ranked list acceptable, or only pass / fail with ranges? | Ranked list labelled preliminary, with flags |
| 2 | Should price be part of the ranking? | No in MVP; shown as platform-scored |
| 3 | Who signs the committee decision, and in what form? | Free-text decision with audit log in MVP |
| 4 | Can bids be sent to external AI APIs? (ARCHITECTURE §14, question 6) | Decide before the first real customer run |
| 5 | Can we get at least three real, anonymised bids for the test set? | Yes, needed for customer-mode acceptance |
