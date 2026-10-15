# Doubletapp research: analysis and build recommendation

Date: 2026-10-14. Model: Claude Opus 5.5 (analysis subagent). Inputs: `docs/spec-en.md`, `docs/requirements-short.md`, `docs/research/doubletapp-buyer-report.md`. No web browsing; no raw sources were in the repo, so the report was checked only against itself and our spec. The subagent's file write was blocked; the orchestrator saved this file from the agent's returned text.

Labels from the report are kept: CONF, VENDOR, STRONG, SPEC, SUB. Where the analyst adds its own reasoning or general knowledge, it is marked **[my inference]**.

---

## 1. Bottom line

1. Doubletapp shows that someone has shipped and sold a Russian-language, buyer-side LLM tender checker (one LLM call per requirement and bidder, with a quote and a document link per verdict), deployable on-prem with open-weight models. That is all VENDOR, not tested.
2. It does not show anything about Uzbek text, 150-dpi or phone-photo OCR, page-level evidence, quote verification, numeric correctness, a human approval gate, deterministic scoring, the supplier side, cost or hardware. Its 92/93/95% figures have no test-set size and cannot be compared with anything.
3. Its published design falls short of four of our hard rules: page plus quote for every status, numbers in code, reproducible scoring and ranking, and five statuses.
4. We should build Option B, a hybrid. The LLM does extraction and semantic judgement over page-anchored retrieved text. Code verifies every quote and computes all numbers, dates, statuses, scores and rankings. The model is chosen by our own Uzbek bake-off, not by Doubletapp's choice.
5. Before we commit to hosting or a model, run three cheap experiments: Uzbek OCR, a model bake-off on a small gold set, and a numeric-comparison test. Also check ZRU-684 on lex.uz.

---

## 2. Confirmed vs. inferred vs. unknown (design-relevant only)

| Claim | Status | Note |
|---|---|---|
| One LLM call per (requirement × bidder) | VENDOR | Doubletapp describes it on Habr; we have not seen it work |
| Bidder documents sit in a cached prompt prefix (whole-document context, no retrieval) | STRONG at most | Section 8 of the report states it as fact, but section 4 rates it STRONG |
| Three statuses: meets / does not meet / insufficient data | VENDOR | The wording is CONF from Habr. The labels in the gold set may differ (see §8) |
| Quote plus document link per verdict | VENDOR | Page level is not mentioned |
| Quotes are checked against the source | Unknown | Not mentioned. The report's "they don't check" is an argument from absence (SPEC) |
| Numbers compared in code | Unknown | Not mentioned anywhere |
| LLM produces the ranking and a sign / shortlist / reject outcome | VENDOR | |
| Committee keeps the final decision | VENDOR | From an author comment |
| No human approval of the checklist | VENDOR (absence) | The phrase "без участия человека" is CONF, but it may describe automatic generation rather than a missing review step |
| Accuracy: extraction 93%, matching 92%, case page 95%/93% | VENDOR | No test-set size, metric names differ, Russian only |
| 50 pages in 5–7 min | VENDOR | Unit unclear: per document, per bidder or per tender |
| Best models: DeepSeek V3.1, Qwen3-235B-A22B on vLLM | VENDOR | Tested on Russian documents. Uzbek unknown |
| OCR engine, table handling | Unknown | |
| Hardware, cost per tender, pricing | Unknown | The report's 4–8 × 80 GB GPU estimate is SPEC |
| Any Uzbek-language support | Unknown | Nothing found |
| Supplier-side capability | Unknown | Only a landing-page card. Lucius run pending |
| Registry checks (EIS/RNP) are manual | VENDOR | Conflicts with the landing page's "sanctions screening" claim (see §8) |
| ZRU-547 Art. 27-1: in-country storage required only for biometric, genetic and telecom-subscriber data | CONF | Re-read on lex.uz, amended by ZRU-1125 |
| ZRU-684 Art. 20 / 36 / 41 contents | SUB | Not re-checked; do not encode yet |
| RU Decree 2604 (price weight ≥ 70%) | SUB | Relevant only if we target Russia |

---

## 3. Architecture options

**A. Mirror Doubletapp, with a bigger model.** Documents become plain text. An LLM extracts the checklist. One LLM call per (requirement × bidder) with the whole bid in a cached prefix. The LLM also ranks.

**B. Hybrid, deterministic core.** OCR and parsing produce a page-anchored text layer (file, page, character offsets, OCR confidence, table cells). The LLM extracts requirements and bid facts as typed JSON with quote spans. Code then verifies each quote against the page text. Per-item retrieval over the bid feeds one LLM call per (requirement × bidder), for semantic items only. Code handles units, numbers, dates, document validity, status derivation, scoring, the rejection flag and ranking. The LLM provider sits behind an interface.

**C. Small open-weight model on-prem, with a strict rule layer.** Same pipeline shape as B. The LLM is a mid-size open model (roughly 30B class) on 1–2 GPUs. More checks are pushed into rules and templates, and anything ambiguous goes to "Needs human review".

| | A: Mirror | B: Hybrid | C: Small on-prem |
|---|---|---|---|
| **Five statuses** | The LLM picks a label. "Partial" (an expired document) and "Not confirmed" (claimed in the proposal, no supporting document) depend on dates and document type, which the LLM would judge implicitly. Doubletapp only has 3 statuses. | Statuses are derived by code from signals. Partial: counts or expiry by date. Not confirmed: the fact appears only in the technical proposal. Does not meet: the value violates the condition. Review: low confidence or failed quote check. The LLM judges only semantic fit. | Same as B, but the weaker model sends more items to Review. |
| **No quote, no status** | Hard to enforce. Plain text loses pages; Doubletapp links to the document, not the page. | Enforced in code: a quote that is not found on the cited page sends the item to Review. | Enforced the same way. More quotes fail, so more items go to Review. |
| **Approval gate** | Can be added, but nothing in the design needs it | Required by design: matching runs only on the approved checklist version | Same as B |
| **Cost / hardware** | Highest. Every call carries the whole bid. On-prem with DeepSeek V3.1 (671B) at 8-bit is about 671 GB of weights, more than 8 × 80 GB; Qwen3-235B at 8-bit is about 235 GB **[my inference, arithmetic]** | Lower context per call. Works with a hosted API or a large open model | Lowest hardware. Cost moves into human review time |
| **Uzbek quality risk** | High and untested; the whole judgement depends on the LLM | Medium. Numbers and dates are protected by code; semantic items still depend on the model | Highest. Smaller models are usually weaker in low-resource languages **[my inference]** |
| **Data residency fit** | On-prem possible with costly hardware | Either mode. The model is swappable once the hosting decision is made | Best fit for strict on-prem |
| **Time to MVP** | Fastest to a demo | Medium. The text layer and rules engine are real work | Slowest. Many rules to write; Uzbek tuning |
| **Main failure mode** | A confident wrong "Meets" with an unverifiable quote; LLM ranking that changes between runs | Retrieval misses the evidence, giving a false "Does not meet" (missing item) | "Needs human review" share above our 15% cap, so the tool saves little time |

**Where Doubletapp's published design fails our spec:**
- A single plain-text version per document loses page anchors and table structure (spec §3.2, §10).
- No page-level citation or quote check (§10).
- Numbers are not stated to be compared in code (§6).
- The LLM ranks and recommends, which is not reproducible (§7, §10 reproducibility).
- Three statuses (§6).
- No documented checklist approval (§4.3).
- No re-check or version history (§2).
- Whole-bid context will not fit our 1000-page, 500 MB package limit without retrieval (§3.1) **[my inference]**.

---

## 4. Recommendation

**Build Option B.** Reasons tied to the evidence:
- Every Doubletapp advantage that we can actually use is architectural, and B keeps it: per-item checking (their stated main quality lever, VENDOR), separate measurement of extraction and matching, and a benchmark gate on every prompt or model change.
- None of their accuracy evidence transfers to Uzbek, so we must not pick a model before our own test. B is the only option that lets the model be chosen after the bake-off.
- Four hard constraints (quote and page, numbers in code, reproducibility, five statuses) are code properties, not model properties.

**First two things to build:**
1. **A page-anchored text layer with a quote verifier.** OCR and parse every file to page, offset and confidence. Add a fuzzy matcher that confirms a returned quote exists on the cited page and that its OCR confidence is above the threshold. This is the base for "no quote, no status", and the same harness runs the OCR bake-off.
2. **A typed checklist and rules engine with the approval gate.** Implement the §4.3 schema, the approve/lock step, unit normalisation, numeric/date/validity comparison, status derivation, the §7 scoring and the rejection-risk flag. Every test case from the numeric experiment (§6, V3) becomes a unit test.

**What we deliberately do not copy:**
- **Ranking or sign/shortlist/reject decided by an LLM.** Ranking is computed from the committee's weights; the LLM only writes the explanation.
- **"Discard the others right away."** Rejection risk needs a failed mandatory item, a verified quote and human confirmation.
- **Checklist without a human gate.** This holds whether or not Doubletapp really lacks one.
- **Document-level links and unverified quotes.**
- **Three statuses.**
- **Accuracy figures without a test-set size.** We publish ours with sizes and confidence bounds.
- **Their generic privacy policy** (unlimited processing period plus a cross-border clause, CONF). We need configurable retention and a no-training statement.

**What would change this recommendation:**
- No open-weight model passes the Uzbek bake-off and the user requires on-prem. Then on-prem Uzbek is not viable yet: launch hosted, or limit the scope to Russian-language tenders first.
- A mid-size open model passes the Uzbek thresholds. Then C becomes the on-prem variant of B (same code, different model).
- No OCR engine meets thresholds on 150-dpi Uzbek Cyrillic. Then raise the minimum input quality or route those pages to manual transcription.
- The target market becomes Russia. Then Doubletapp is a direct incumbent, 152-FZ hosting applies, and differentiation must come from the supplier side and evidence quality.
- The Lucius run finds a strong supplier-side tool in our market.

---

## 5. Decisions for the user

| Decision | Default recommendation |
|---|---|
| **Target market:** Uzbekistan or Russia | Uzbekistan first. Doubletapp shows no Uzbek capability, and our spec is built around Uzbek law and languages |
| **MVP side.** The spec (§12) puts the customer N-bid mode in "Expansion", while the brief puts both sides in the MVP; these conflict | One shared engine. Ship supplier self-check first (matches the spec's 5–10 supplier pilot), then the customer N-bid matrix and ranking as a thin layer on top. Drafted clarification letters and the DOCX export come later |
| **Hosting and external LLM APIs** | For private-sector pilots, allow a hosted API under a data processing agreement with no training on customer data. Keep the model interface so on-prem stays possible. Decide state-buyer hosting after the legal check (V4) |
| **Budget ceiling** (per-tender inference cost, or a GPU purchase) | Set a ceiling before the bake-off, so models are judged on cost as well as accuracy |
| **Acceptance targets.** The spec asks for 100% recall and 100% accuracy | Keep zero false "Meets" on mandatory items as the hard gate. Replace the other 100% targets with measured rates and confidence bounds. Do not use Doubletapp's figures as targets |
| **How to evidence "missing from the bid."** "No quote, no status" conflicts with a "Does not meet" based on absence | Accept a tender-side quote plus a logged list of the bid documents and pages searched. For mandatory items, require human confirmation before the rejection flag is final |
| **LLM recommendation to the committee** | None. Show deterministic ranking and flags, plus an "advisory only" disclaimer (spec §13) |
| **First procurement type** | Uzbek goods tenders in 1–2 industries, where numeric checks dominate |
| **Who labels the gold set, and when** | A named tender expert, booked before engineering starts; this is on the critical path |

---

## 6. Validation plan before we commit

The effort figures are the analyst's estimates for one engineer plus one domain expert.

**V1. Uzbek OCR bake-off (about 1.5–2 weeks; 5–6 expert-days for transcription)**
- **Sample:** 120 pages: 40 Uzbek Latin, 40 Uzbek Cyrillic, 40 Russian. In each script, 20 are 150–199 dpi scans and 20 are phone photos. At least 25 pages contain tables, and at least 10 mix languages. Ground truth is transcribed by hand.
- **Engines:** 3–4, for example an open-source engine with Uzbek models, PaddleOCR or Surya, one commercial OCR, and one multimodal LLM.
- **Metrics:**
  - Character error rate (CER) and word error rate per script and capture type.
  - Exact accuracy on numeric tokens (digits, units, dates).
  - Accuracy on Uzbek-specific characters (oʻ, gʻ, ʼ; ў, қ, ғ, ҳ).
  - Table cell accuracy.
  - Calibration: the share of errors that fall in low-confidence spans.
- **Pass:**
  - CER at most 5% on photos (spec).
  - CER at most 5% on 150–199 dpi scans. The spec has no target for this band (its 2% applies to ≥200 dpi), so this threshold is a proposal.
  - Zero numeric-token errors that are not flagged as low confidence.
- **Fail:** no engine passes Uzbek Cyrillic. Then change the input requirements or route those pages to manual work.

**V2. Model bake-off (about 3–4 weeks, mostly expert labelling)**
- **Sample:** 6–8 real tenders, each with at least 3 bids, at least 40% Uzbek-language. That gives about 400 gold (requirement × bidder) pairs with the five statuses, page and quote. At least 300 of the pairs should be mandatory items. With zero errors observed in 300, the 95% upper bound on the false-"Meets" rate is about 1% (rule of three).
- **Models:** one hosted frontier model, one large open model (Qwen3-235B-A22B or DeepSeek V3.1), and one mid-size open model. All run through the B pipeline.
- **Metrics:**
  - Mandatory-requirement recall, scored on extraction separately from matching (Doubletapp's good practice).
  - Five-class confusion matrix.
  - False "Meets" on mandatory items.
  - Quote-verification pass rate.
  - "Needs human review" share.
  - Accuracy gap between Uzbek and Russian.
  - Identical statuses across 3 reruns.
  - Cost and latency per pair.
- **Pass:**
  - Zero false "Meets" on mandatory items.
  - "Needs human review" share at most 15%.
  - Quote verification at least 95% (proposal).
  - Uzbek accuracy within 5 points of Russian (proposal).
  - Rerun stability of at least 98% (proposal).
  - Within the budget ceiling.

**V3. Numeric-comparison test (3–5 days)**
- **Cases:** 250 requirement-and-fact pairs, real and synthetic. They cover:
  - Unit conversion: kW/W (including "≥ 2.5 kW" against "2500 W"), mm/inch, °C/°F, GB/ГБ.
  - Number formats: decimal comma, space and non-breaking-space thousands separators.
  - Ranges and tolerances, including ±.
  - Operators in Russian and Uzbek ("не менее", "kamida", "не более", "ko'pi bilan").
  - Dates in several formats, and validity on the submission deadline exactly.
- **Comparison:** run the code comparator against LLM-only judgement.
- **Pass:** the code is 100% correct on every case it parses, and every unparsed case goes to Review, never a guess. We also record the LLM-only error rate as the evidence for this design.

**V4. Legal check (1–2 days, plus a lawyer review)**
- On lex.uz, read the current consolidated text of ZRU-684 Art. 20, 36, 41 and 42, plus the articles on rejection grounds and evaluation criteria. Record the version date and the exact wording.
- With a lawyer, confirm which ZRU-547 Art. 27-1 condition we would rely on for cross-border processing.
- Also ask whether passport photos in bid packages count as biometric data. If they do, localisation would apply. This is unknown, and it is an open question, not a finding.

**V5. Retrieval vs. whole-bid context (3–4 days, reusing V2 data)**
- On the 3 largest bids, measure token counts for Uzbek against Russian, and compare evidence recall with retrieval against whole-bid context.
- This settles the A-vs-B difference and B's main failure mode (false "Does not meet" from missed evidence).

---

## 7. Gaps in the research, by impact on the decision

1. **Uzbek OCR and LLM quality.** There is no evidence at all. Close with V1 and V2.
2. **The supplier side and the Uzbek competitor landscape.** The report covers one Russian, buyer-side vendor. Close with the Lucius run, plus a scan of Uzbek tender tools and interviews with 5 or more Uzbek tender specialists.
3. **Legal basis.** ZRU-684 is SUB; the cross-border conditions and the biometric question are open. Close with V4.
4. **Cost and hardware.** Nothing is known. Close with V2 cost logging and a quote for in-country GPU hosting.
5. **Doubletapp's actual behaviour:** approval gate, numeric handling, quote verification, page references, statuses. Close with a demo call using the report's §11 questions, plus our numeric edge case.
6. **Willingness to pay**, on the buyer and on the supplier side. Close with pilot interviews.
7. **crt.sh and the Cloud.ru listing.** Low impact; retry later.

---

## 8. Conflicts and weak points in the report

1. **SUB law used as core evidence.** Lessons 6 and 8 (STIR-based registry check, ranking in code) rest on ZRU-684 Art. 36 and Art. 20, which are SUB. The conclusions still hold for engineering reasons, but the legal justification is unverified.
2. **Article list mismatch.** §11 asks to re-verify Art. 42, which appears nowhere else in the report. Its subject is unknown.
3. **Absence treated as fact.** §4 rates "no quote verification" as SPEC, but Lesson 5 states "They don't check that the quote really exists". D2 "leaning toward LLM" and §6 "ranking done by LLM, not described" are the same pattern.
4. **Prefix caching overstated.** §4 rates "bidder documents in the cached prefix" as STRONG, while Lesson 1 presents it as a Habr fact.
5. **The "no human gate" reading.** «Без участия человека» may mean automatic generation, not a missing review step. D5's "states the opposite" goes too far.
6. **The "discard the others" reading.** Q6 says humans check the top candidates and others can be discarded; D1 turns this into "discarding without human review". Those are different claims.
7. **The status set is unclear.** The product has 3 statuses, but D3 cites expert labels «нужно дозапросить» and «неясно». If the gold set used 4 labels, the meaning of the 92% "exact category match" is unclear.
8. **Registry claims conflict.** The landing page (§3.C) claims licence checks and sanctions screening, while the author (Q4) says registry checks are manual. They are from different dates, and the report does not resolve the conflict.
9. **Speed unit unclear.** "50 pages in 5–7 min" does not say per document, bidder or tender, so it cannot be compared with our ≤10 min per 100 pages.
10. **Customer claims rest partly on unverified sources.** The industrial customer comes from a SUB Telegram post, and the 2x speed figure in §9 comes from the SUB 2025-12-02 post.
11. **Decree 2604 is weak.** It rests on a secondary mirror, from subagent D, which "guessed some law IDs".
12. **The hardware estimate is loose.** "4–8 × 80 GB" does not hold for DeepSeek V3.1 at 8-bit (about 671 GB) **[my inference]**.
13. **Weak points in our own spec**, found while comparing:
    - No CER target for the 150–199 dpi band.
    - The 80% "AI confidence" trigger relies on model self-reported confidence, which is poorly calibrated **[my inference]**. Use verifiable signals instead.
    - "No quote, no status" conflicts with "Does not meet: item missing".
    - §10 still assumes general data localisation, which the CONF reading of Art. 27-1 does not require.
