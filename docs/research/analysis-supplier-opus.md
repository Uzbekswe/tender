# Lucius research: supplier-side analysis and build recommendation

Date: 2026-10-14. Model: Claude Opus 5.5 (analysis agent). No web browsing. Inputs: `lucius-supplier-side-research.md` (read in full), `docs/lucius-supplier-prompt.md` (its brief and hard rules), `docs/spec-en.md`, `docs/requirements-short.md`, `docs/research/analysis-opus.md`, `docs/research/doubletapp-buyer-report.md`. No raw Lucius sources or screenshots are in the repo, so the research was checked only against itself, its brief and our spec. The analysis agent's file write was blocked; the orchestrator saved this file from the agent's returned text.

Labels: CONF, VENDOR, STRONG, SPEC, SUB. **The research never uses SUB.** Its legend says unverified quotes "are marked as subagent-sourced", but no claim carries that mark. So in this file, every VENDOR claim that is not tagged "re-verified" and not in the §12 re-verified list is treated as **SUB**. The analyst's own reasoning is marked **[inference]**.

---

## 1. Bottom line

1. Lucius shows, almost entirely through its own claims, that a very small team can ship supplier-side tender analysis with page-cited requirement quotes that are string-verified, using one hosted model. The only part anyone observed directly is a free checker that compares one pasted answer with pasted requirements.
2. That checker is a warning, not a model to copy. It has two statuses only, shows paraphrased "evidence" as if it were quoted, counts claims delivered by a partner or covering only some staff as met, and misses a contradiction inside the bid.
3. The research shows no check of a finished multi-document bid package (supporting documents, validity, signatures, consistency). But that rests on public pages saying nothing about it: the paid product, the Word add-in and the guest upload were never tested.
4. Nothing transfers on Uzbek, Russian, OCR quality or accuracy.
5. The evidence strengthens Option B and leaves the MVP plan as it was (supplier first, one shared engine). It adds three concrete requirements: verify quotes on the bid side as well, record why each item is "Does not meet", and check who a claim belongs to.

---

## 2. Confirmed vs. inferred vs. unknown

| Claim | Research label | Correct label | Note |
|---|---|---|---|
| Free checker takes pasted requirements plus one pasted answer; there is no file input | CONF | CONF | Page fields plus F runs |
| Checker statuses are binary (addressed / NOT FOUND), with no reason given | CONF | CONF | Only 8 requirements, 1 synthetic draft |
| Checker "evidence" is a paraphrase and its wording changes between identical runs | CONF | CONF | This refutes the vendor's "exact phrase" claim |
| The checker does no verbatim check of its evidence | CONF | **STRONG** | Inferred from paraphrases being accepted; no check was observed directly |
| Score = round(addressed ÷ total × 100) | CONF | **STRONG** | Only two data points (4/8 → 50, 3/8 → 38), which rounding up would also fit |
| Statuses stay the same when the run is repeated | CONF | CONF, n = 1 | One repeat of one input |
| Credit for partner-delivered and partial-coverage claims | CONF | CONF, n = 2 items | Observations, not error rates |
| One synchronous POST taking 5.8–14.7 s | CONF | CONF | Whether that is one LLM call or several behind it is unknown |
| Tender requirements carry a verbatim quote, page and `quote_verified` | VENDOR | VENDOR (re-verified docs) | Not observed in a run |
| The citation verifier is string-based, with normalisation added for whitespace and hyphenation | VENDOR / STRONG | VENDOR | Tender side only |
| Extraction count varies 35–61 between runs; a silent drop from 366 to 184 | VENDOR | **SUB** | Not in the re-verified list |
| Chunked best-of-N extraction, results merged | VENDOR | **SUB** | Same |
| Paid compliance list status is set by the user | VENDOR | VENDOR (re-verified verbatim) | |
| Word add-in "checks a draft against the tender's requirements" | VENDOR | **SUB** | One sentence. D2 says "integrations re-verified", but §12 does not list it |
| Certifications are checked against the company library; capability-fit check during drafting | VENDOR | VENDOR (fit check re-verified); certifications **SUB** | |
| No check of a multi-document bid, its validity, signatures or consistency | "not found" | Absence only | No test of the paid product |
| Risk score is a formula over severities | STRONG | **SPEC** | Based on one vendor sentence |
| Bid / no-bid verdict decided by an LLM | SPEC | SPEC | |
| Gemini is the only AI provider | VENDOR | VENDOR (re-verified) | |
| A Gemini Pro-tier model | STRONG | **SPEC** | One clue only ("pending regional Pro-model deployment") |
| OCR is Gemini multimodal, not Document AI | STRONG | **SPEC** | Document AI is a GCP service, so the "GCP" subprocessor entry would cover it; "no other AI provider" does not rule it out |
| Uzbek or Russian support | not found | Unknown | "Reads any language" is VENDOR; drafting is English only |
| Retention 30–3,650 days; no training on paid data; global inference endpoint | VENDOR | VENDOR (re-verified) | The free tier's training policy is not addressed |
| Disqualifiers sit inline in the list, not separately | CONF on the example | VENDOR | It is a marketing example page |
| Pack reruns for 60 days; draft history | VENDOR | **SUB** (pricing) / VENDOR (trust) | No score-change history found |

---

## 3. Is the "line-by-line bid check" gap real?

**Evidence for it:**
- The free tool checks one pasted answer (CONF).
- In the paid list, the user sets the status (VENDOR, verbatim).
- No page describes matching supporting documents or checking document validity or consistency (absence, across workstreams A, A2 and D).
- Lucius positions itself on tender analysis plus drafting (VENDOR).

**What it does not show:**
- The paid analysis, the guest upload and the Word add-in were never run. The add-in's scope is one sentence (SUB).
- Lucius does match requirements against company evidence. Its capability-fit check runs against the Bid Library during drafting (VENDOR), and certifications are checked against the library (SUB). The gap is therefore narrower than "Lucius only analyses the tender". What is missing is a check of the **submission as it will be submitted**: the documents actually attached, their validity on the deadline, signatures, consistency and contradictions **[inference]**.
- Lucius does not target Uzbekistan. Even if the gap is real at Lucius, that says nothing about local tools, and they were not researched.

**What would settle it:**
1. Read the AppSource listing for the Word add-in (passive, 0.5 day).
2. The user runs a paid trial or demo with a synthetic multi-document bid. The user would create the account, and the terms would need checking first.
3. Scan Uzbek tender tools and platform-built checks, and interview 5 or more Uzbek bid specialists.

**Verdict: not settled.** It is confirmed for the free tool. For the paid product the only support is absence of evidence, and in part it is contradicted by the evidence-fit matching Lucius does during drafting. The narrower claim (no document validity or consistency checks on a submission package) is better supported, but it still rests on absence.

---

## 4. Supplier-side features: Lucius vs. what we need

| Our feature | What Lucius does (evidence) | What we add |
|---|---|---|
| Checklist from the tender, with quote and page | Requirement map with verbatim quote, page, `quote_verified`, `page_is_approximate` for Word files, severity and reason (VENDOR, re-verified docs) | T-/U- IDs, Mandatory/Scored/Desirable type, confirmation method, approval step, and a stable page anchor for DOCX/XLSX (render to PDF) |
| Per-item bid check | Free checker only: one pasted answer, one call, binary statuses (CONF). Paid: status set by the user (VENDOR) | Matching of a multi-document package, with a **verified quote and page on both sides** |
| Five statuses | Two in the checker (CONF); three, set by the user, in the paid list (VENDOR) | All five, derived by code. Add reason codes to "Does not meet" so a **wrong value** is told apart from an **absent** item; the checker merged the two (£2 m vs £5 m) |
| Readiness verdict | A bid / no-bid verdict on the *tender* (VENDOR), which can be null; a 16-question self-assessment tool (VENDOR) | A code rule on the *bid*: any failed or unproven mandatory item gives Rejection risk; the % is shown next to a "mandatory failed" count |
| Fix recommendations with action types | Gap list only (CONF in the checker); "[TO COMPLETE]" markers in drafts (VENDOR) | Action type derived from reason codes; criticality; impact. **No evidence on this at all.** |
| Document validity and consistency checks | **No evidence at all** | All of it: expiry on the deadline, signature and stamp, name and STIR across documents, translations, contradictions |
| "What still needs to be collected" list | Self-assessment checklist (VENDOR); partner-slot banner count (VENDOR) | Built from each item's confirmation method and its status |
| Re-check and version history | Pack reruns; draft history with restore (VENDOR/SUB) | Recalculation that tracks dependencies, and a score history. **No evidence on score changes.** |
| OCR and Uzbek | Scans accepted; OCR engine SPEC; quality unknown; English-only drafting | **No evidence on Uzbek, Cyrillic, 150 dpi or phone photos.** Our own bake-off (V1) |
| Never suggest changing facts | Drafts "never filled with made-up facts", gaps marked, a partner slot (VENDOR, re-verified). Not testable in the checker | Enforced in code: a fix may never introduce a fact value unless it is copied from an authoritative bid document |
| Claim attribution (new) | The checker credited a partner's activity (CONF, n = 1) | Check that the subject of a claim is the bidder (name or STIR); otherwise Not confirmed or Needs review |

---

## 5. Comparison with the buyer-side findings

**Engines.** In Lucius, the checker sits on a synchronous `/growth/` endpoint (CONF), while the main analysis is asynchronous with 202 and polling (VENDOR). So two separate pipelines is likely, but that is SPEC. Lucius has no buyer side. Doubletapp has only a buyer pipeline and a supplier card on its landing page. Neither vendor shows one engine serving both sides.

For us **[inference]**, the supplier self-check is the N = 1 case of the buyer's N-bid check. They share:
- the text layer
- the checklist
- matching
- the statuses
- the document checks

Each side then adds its own layer:
- Supplier only: fixes, forecast, collect list and re-check diff.
- Buyer only: matrix, ranking, clarification requests to bidders, and human confirmation of rejection.

**Option B.** The Lucius evidence supports it at four points:
1. The LLM's "evidence" drifts and paraphrases unless code verifies it (CONF). This is the strongest direct evidence we have for "no verified quote, no status".
2. The binary status mixed up a wrong value and a missing one (CONF). Numbers compared in code fix this.
3. Lucius itself needed normalisation in its quote verifier (VENDOR). Ours needs it too, plus Uzbek apostrophe variants (oʻ / o' / o‘) and Latin/Cyrillic look-alike letters **[inference]**.
4. Silent drops in extraction (SUB) argue for the benchmark release gate Doubletapp described, plus a monitor on requirement counts.

Nothing contradicts B. Lucius running on a single hosted Gemini says nothing about Uzbek quality, so the bake-off still decides the model.

**Patterns to carry across:**
- Supplier to customer:
  - a "mandatory failed" counter next to the %
  - a verdict that lists the items that decided it
  - a visible tick on verified quotes
  - reason codes on "Does not meet"
  - the claim-attribution check
  - consistency checks inside a bid. A contradiction is grounds for a clarification request to the bidder.
- Customer to supplier:
  - a checklist review step before matching (Lucius has none documented, which is unknown, not absent)
  - measuring extraction and matching separately
  - the gold-set release gate

---

## 6. Recommendation for the MVP

**Option B, supplier side first, on one shared engine.**

**First two builds for the supplier side:**
1. **A page-anchored text layer with a two-sided quote verifier and an approved checklist.**
   - OCR and parse both packages down to file, page, offset and confidence. Render DOCX/XLSX to PDF so page numbers are stable.
   - Verify every tender quote and every bid quote with normalised fuzzy matching.
   - The typed checklist, plus a "what to collect" list built from confirmation methods, already works as the "Tender analysis" scenario before any bid exists.
2. **Bid-package facts, document checks and the status/verdict engine.**
   - Extract typed facts from each bid document (name, STIR, dates, model, values, whether a signature or stamp is present, with confidence).
   - Code then checks expiry against the deadline, STIR and name consistency, contradictions between documents, numeric thresholds and attribution.
   - Statuses are derived from reason codes, and the readiness verdict comes from a fixed rule.
   - Fix action types are mapped from reason codes. The LLM only writes the wording.

The fix forecast, the re-check diff and version history come next.

**What we do not copy from Lucius:**
- paraphrased "evidence" presented as a quote
- binary statuses
- NOT FOUND with no reason
- crediting a partner's activity
- a % shown without a mandatory-fail count
- a verdict an LLM can decide or leave null
- the global inference endpoint
- keeping company documents in a store "until deleted" by default
- English-only output

**What would change this:**
- Supplier interviews show that rejections are mostly formal (expiry, signatures, STIR). Then build the document checks before semantic matching.
- Signature or stamp detection fails the synthetic test. Then those checks always go to "Needs human review", and the spec wording is softened.
- A local tool already checks submission packages in Uzbek or Russian. Then differentiation must come from evidence quality, STIR and the fix loop.
- V4 finds that passport photos count as biometric data. Then documents are stored in-country.
- A buyer-side contract (for example, TenderGPT) is signed first. Then the buyer side ships first on the same engine.

---

## 7. UX patterns

**The research captured no screenshots.** Section 8 says F "hasn't run", yet section 3 reports three runs. There are no image files in the repo.

| Pattern | Source | What it does | Why it works / caveat |
|---|---|---|---|
| Summary row: "50% met · 4/8 addressed · 123 words", with a red over-limit warning | F runs (CONF) | Readiness at a glance | Add a "mandatory failed" count so a high % cannot hide a fatal gap |
| Gaps first | Marketing page only | Unmet items appear at the top | **Conflict:** F observed the checklist in input order. Use it for criticality-ordered fixes anyway |
| Verified-quote tick plus click-to-open source | `/examples/rossendale-play-area` (VENDOR) | Failed verification shows no tick | Makes hallucinations visible; maps to our split-view viewer |
| Verdict names the requirements that decided it | llms-full (VENDOR) | Explains the verdict | Use it for "Rejection risk" |
| "[TO COMPLETE: …]" markers plus a banner count | how-it-works (VENDOR) | Gaps stay visible | Fits "never change facts" |
| "Load example" button | F (CONF) | First result with no setup | Ship a synthetic Uzbek tender and bid |
| Two-speed results (fast preview, then full run with progress) | VENDOR | Something to look at early | Matches the spec's "intermediate results"; their timings are not our targets |
| "Evidence quoted from the bidder's text" | Research §8 pattern 2 | — | **Do not use as stated.** F refuted it |

---

## 8. Decisions for the user

| Decision | Default |
|---|---|
| MVP side | Supplier first, on the shared engine. The buyer N-bid matrix and ranking come as a thin layer later |
| Can "Rejection risk" be final without a human? | On the supplier side, yes, as an advisory flag, because the supplier *is* the human reviewer. Each one lists its deciding items with verified quotes. A fail based only on absence is labelled "not found in pages searched". On the buyer side, committee confirmation stays mandatory |
| "Declared but not proven" | Status: Not confirmed (k = 0.3), never Meets. On a mandatory item whose tender names a confirmation document, it gives Rejection risk with the action "add document". If no confirmation method is specified, it gives Needs revision |
| Mandatory item at Needs human review | It blocks "Ready to submit" and gives Needs revision |
| Partner or subcontractor claims | Not confirmed, unless the tender allows subcontracting and a supporting document exists |
| Meaning of "Correct data" | Only to align with an authoritative bid document (for example, a STIR typo checked against the registration certificate). Never to match the requirement |
| Hosting for supplier documents | Hosted in one named region under a DPA, with no training and no-retention model endpoints (not a global endpoint), configurable retention and deletion on request. Model interface kept swappable. Move storage in-country if V4 finds identity-document photos are biometric |
| Company document vault (like Lucius's Bid Library) | Not in the MVP; opt-in later |
| More Lucius testing | Only the passive AppSource read. Do not run the guest upload (low value, and the terms risk in §10) |

---

## 9. Validation plan

**S1. Synthetic bid with planted errors.** Effort is about 8–10 person-days: 4–5 expert-days and 3–4 engineer-days. It can be prepared during Build 1 and run after Build 2.

**No real company bids.** The tender can be modelled on a public Uzbek tender. The bid must be entirely fictional and watermarked "TEST DATA": fake company, fake STIR in the valid format, and invented stamps that do not imitate any real organisation.

**Set:**
- 1 goods tender of about 30 requirements, mandatory and scored, in Uzbek Latin and Russian.
- 12 bid variants: 3 scripts/languages × 2 capture types (150 dpi scan, phone photo) × 2 placements of the errors. Each variant is about 10 documents.

**Planted errors in each variant:**
- a certificate that expired the day before the deadline
- a STIR that differs by one digit between the application form and the registration certificate
- a model number that differs between the proposal and the product passport
- a missing signature on one form
- a threshold breach (a "minimum" requirement against a lower declared value)

**Extras:**
- a claim that is declared but not proven
- 2 of 3 certificates present
- experience attributed to a partner
- a warranty contradiction inside the tender

**Controls (at least 15 per variant), which should not be flagged:**
- a certificate valid exactly on the deadline
- "2500 Вт" against ≥ 2.5 kW
- the same STIR written with different spacing
- "XG 2500" vs "XG-2500"

**Pass (all must hold):**
- All 60 planted mandatory errors are caught: none marked Meets, and no variant shows "Ready to submit". With 60 out of 60, the 95% upper bound on the miss rate is about 5%. This is a smoke gate, not an accuracy claim.
- A missing signature may show as "Does not meet" or "Needs review", never as Meets.
- At least 95% of the detections carry a verified quote and the correct page on both sides, or a logged list of pages searched for absence-based items.
- The action type is correct in at least 90% of cases (expert check).
- Zero fix texts introduce a new fact value (code check plus expert).
- False alarms on controls are at most 10%, and "Needs review" is at most 15% (proposals).
- Three reruns give identical statuses.

**Fail:** a single planted mandatory error marked Meets.

**Other experiments:**

| # | Question | Experiment | Effort |
|---|---|---|---|
| S2 | Does a competitor already check submissions? | AppSource listing; scan of Uzbek tools; 5+ supplier interviews on real rejection causes | 0.5 day + about 2 weeks |
| S3 | Can we detect a signature or stamp? | 100 synthetic pages, half signed or stamped, scan vs photo; precision and recall | about 1 week |
| S4 | Stable DOCX page anchors | Render 20 DOCX/XLSX files and check that quote pages match across reruns | 1 day |
| V1–V5 | OCR, model, numbers, legal, retrieval | As in `analysis-opus.md`; they apply to both sides | as stated there |

---

## 10. Gaps and weak points in the Lucius research, ranked by how much they would change the decision

1. **The core hypothesis rests on absence of evidence.** The paid analysis, guest upload and Word add-in were not tested, and the AppSource listing was not read. Yet D2 says "largely confirmed".
2. **Sections contradict each other after F.**
   - The header and §3 say F part 1 ran.
   - §8 says "F hasn't run"; §10 says "no hands-on run yet".
   - §11 still lists the paste-checker test as a next step.
   - §1, the §6 spec row and §8 pattern 2 still claim "quoted evidence from the draft", which §3 refutes.
3. **The rules and terms were not fully followed.** *(Checked against the research file: §5 of the terms and the bundle and fetch-observer lines are as described.)*
   - Terms §5 forbids "reverse engineer, decompile, or extract source code", automated access, and any use other than tender analysis. The research marks this verbatim, re-verified. Even so, an agent-driven browser ran the checker. The brief made confirming that the terms allow this use a precondition for F, and the research confirms only the user's approval, not the terms.
   - Bundles were inspected before the scope was cut back. The research flags this itself and asks the user to decide whether to keep those findings.
   - A passive in-page `fetch` observer was injected into the page to record network calls. That goes beyond "passive", and it sits close to the §5 clause.
   - The research complied on: no account created, no real bid, 3 runs.
   - Whether the user approved the synthetic draft's *content* is not stated.
   - Not done: the text-PDF and scanned-PDF tests, any screenshots, REA.
4. **The SUB label is missing.** All unverified VENDOR claims, including the extraction variance, the silent regression, the Word add-in, certification checks and pricing, are really SUB.
5. **Labels stronger than the evidence:**
   - score formula: CONF → STRONG
   - "no verbatim check": CONF → STRONG
   - Gemini Pro: STRONG → SPEC
   - OCR not Document AI: STRONG → SPEC
   - risk-score formula: STRONG → SPEC
   - disqualifiers inline: CONF → VENDOR
6. **The samples are tiny.** 8 requirements, 1 synthetic draft, 1 repeat. "Lenient matching" is based on 2 items.
7. **Unresolved conflicts:**
   - severity has 3 levels on the pages and 4 in the API
   - citations flagged: 68 vs 90 of 158
   - gaps first (marketing page) vs input order (observed)
8. **"Market gap" for Uzbek** is relative to Lucius only. No local competitors were scanned.

---

## 11. Cross-side conflicts and spec weak points

| Conflict | Detail | Resolution |
|---|---|---|
| Per-item calls vs a batch | Doubletapp: one call per item, which they say raised quality (VENDOR). The Lucius checker: one HTTP call for all items (CONF), with lenient errors | Consistent with per-item matching, but it does not prove the point. Keep per-item calls in B |
| Status sets | Doubletapp 3, Lucius 2 (or 3 set manually), us 5 | No competitor validates five. Test with suppliers whether "Partial" and "Not confirmed" lead to different actions |
| Hosting | Doubletapp on-prem / 152-FZ; Lucius hosted, global endpoint | Neither is evidence for Uzbekistan. Follow V4 |
| Spec: "No quote, no status" vs "Does not meet: missing" | Central on the supplier side, where the collect list is built from absences | Tender quote plus a logged list of pages searched |
| Spec: "Does not meet" merges a violated value and a missing item | The F run shows why this matters | Add reason codes |
| Spec: action type "Correct data" vs "never change facts" | Ambiguous | Define it as aligning with an authoritative document only |
| Spec: verdict rule | "Ready = all mandatory green" leaves orange or grey mandatory items undefined for Rejection risk | Adopt the defaults in §8 |
| Spec: "100% working link to the source page" | DOCX has no fixed pages (Lucius uses `page_is_approximate`) | Render to PDF and anchor to that version |
| Spec: forecast after fixes | Assumes every fix succeeds | Leave out "request clarification" and "not fixable" items, or show a range |
| Spec: "recalculate only affected items" | Consistency checks cross documents | Track dependencies; partial and full reruns must match (reproducibility) |
| Spec: no rule on attribution or subcontractors | Gap | Add one |
| Earlier weak points still open | 100% targets, the 80% self-reported confidence trigger, the §10 localisation wording | See `analysis-opus.md` §8 |
