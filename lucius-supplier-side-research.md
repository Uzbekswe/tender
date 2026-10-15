# Lucius AI: supplier-side backend and UX research

Capture date: 2026-10-14. Target: https://ailucius.com (Lucius AI Systems, Inc., Delaware).
Status: workstreams A–E done, plus follow-ups C2 and A2. **Workstream F, part 1 (free paste checker, 3 runs) is done, with your approval. Part 2 (guest upload analysis) is waiting for you to approve a specific tender file.** No account was created, no real bid was used, and nothing else was submitted.

Confidence tags: **CONFIRMED** = observed directly · **VENDOR** = stated by Lucius, not independently verifiable · **STRONG** = strong inference from several clues · **SPEC** = speculation.
"Re-verified" = the orchestrator re-opened the source itself.

---

## 1. Product summary (supplier view)

Lucius is a **tender-analysis-first** product for bid teams in the UK, EU, US, AU, NZ, CA and India. A supplier finds or uploads a tender. Lucius extracts a page-cited "requirement map" or compliance matrix, gives a severity (High/Medium/Low) and a one-line reason for each requirement, works out a 0–100 risk score, and returns a **Bid / Bid with caveats / Skip** verdict scored against the company profile. It then drafts English answers from the company's "Bid Library" (past bids, certifications and policies), marking gaps instead of inventing facts.

Checking on the bid side is **narrow**:
- A free paste-based checker takes one draft answer and marks each pasted requirement addressed or missing, quoting evidence from the draft. *(VENDOR, re-verified)*
- A Word add-in "checks a draft against the tender's requirements". It is described in one sentence only. *(VENDOR)*
- The paid compliance list carries a **status the user sets manually**: open, addressed or not met. *(VENDOR, re-verified verbatim in llms-full.txt)*
- Required certifications are checked against what the company holds in its library. *(VENDOR)*

No evidence was found that Lucius takes a **finished multi-document bid package** and checks each requirement against supporting evidence, document validity, signatures, or consistency between documents.

The company is very small: one founder, with a LinkedIn headcount of 1 to 2–10. Its stack is all Google: Cloud Run, AlloyDB, GCS, Gemini.

---

## 2. Evidence report by workstream (condensed; full subagent output is in the session log)

**A – Product and capabilities**
- Formats are PDF, DOC/DOCX, XLS/XLSX, images and ZIP, "scanned pages included". *(VENDOR, re-verified)*
- File limits: the free preview takes up to 25 MB. FAQ and API allow 40 MB per file, 25 files per upload in the UI, and 80 documents per pack through the API. *(VENDOR)*
- The Pack plan allows 12 files or 500 pages for €149 per tender. *(VENDOR)*
- Pricing: Free is 2 analyses a month with the verdict and 10 key requirements. Team is €2,400 a year. Enterprise is €2,000 a month. *(VENDOR)*
- Requirement fields are a verbatim quote, page, category (Legal, Compliance, Financial, Commercial, Technical, Delivery, Administrative, Quality), severity, and a reason. *(VENDOR; example page /examples/rossendale-play-area)*
- Disqualifiers sit inside the same list, with high severity and titles like "Budget Ceiling Disqualification". They are not a separate list. *(VENDOR example)*
- Lucius reads tenders in "any language" but drafts **in English only**. The product is called "English-first by design". *(VENDOR)*
- No mention of Russian, Uzbek or Uzbekistan was found.
- Tender sources are 13–15 portals: SAM.gov, TED, Find a Tender, Contracts Finder, AusTender, CanadaBuys, GeBIZ, World Bank, UNGM and others. *(VENDOR)*
- Other free tools: the Bid Viability Scorer (inputs typed by the user, not document analysis) and the Tender Readiness Checker (16 Yes/No questions producing a percentage plus a gap checklist). *(VENDOR)*

**B – Frontend surface (REA method; REA itself not installed)**
- The site is Next.js behind Google Frontend. *(CONFIRMED, re-verified)*
- The browser security policy (CSP) lets the page connect to `api.ailucius.com`, `*.run.app` and `storage.googleapis.com`, and to PostHog (US), Sentry (EU, de.sentry.io), GA4 and Google OAuth. *(CONFIRMED, re-verified)*
- The checker page has no file input. Its fields are "The tender's requirements", "Your draft answer" and "Word limit (optional)". *(CONFIRMED)*
- **Bundle inspection, done before the scope was cut back because of the terms (your call whether to keep it):**
  - The guest-upload component enforces 25 MiB.
  - It accepts pdf, docx, doc, xlsx, xls, odt, ods, png, jpg, jpeg and webp.
  - It reads the response with a streaming reader and shows a client-side progress ticker.
  - API paths seen: `/api/v1/guest/claim`, `/api/v1/auth/refresh` and others.
  - Auth is first-party plus Google OAuth. No Clerk, Auth0 or Supabase was seen.
  - No model or OCR names appear in the bundles. No source maps were found.

**C – Legal and trust**
- Gemini is the only AI provider: "We don't send documents to any other AI provider." *(VENDOR, re-verified)*
- Production is GCP europe-west1, with a dedicated AU stack in australia-southeast1. Inference uses the **global** Gemini endpoint, "pending regional Pro-model deployment". *(VENDOR, re-verified)*
- Subprocessors are GCP, Gemini API, Stripe, Resend, Sentry, PostHog and GA4. *(VENDOR, re-verified)*
- Certifications: SOC 2 and ISO 27001 "not yet"; Cyber Essentials "in preparation"; GDPR "aligned". *(VENDOR, re-verified)*
- No DPA page and no ICO registration number were found.
- Terms §5 forbids "reverse engineer, decompile, or extract source code", automated access, and any use other than tender analysis. *(CONFIRMED verbatim, re-verified)*

**C2 – Google Cloud blog, 2026-09-18, "solo founder runs a global tender platform on AlloyDB and MCP"**
- AlloyDB holds relational data, audit logs and embeddings in one engine.
- Embeddings use `ai.initialize_embeddings` and `ai.refresh_embeddings`, with "the Gemini embedding model". Reranking uses `ai.rank` (77 ms mean).
- The vector index is ScaNN: 1.14 s → 24 ms.
- There is an internal ops agent via MCP Toolbox, with read-only access plus one table it can update.
- Ingestion runs nightly from 13 sources.
- The post says nothing about OCR, extraction or bid checking. *(VENDOR)*

**D – Public content and team**
- The engineering blog by the founder is the richest source:
  - **Extraction:** chunk by page range, extract each chunk independently, union the results, best-of-N with rubric selection, and a vision pass for tables.
  - **Run-to-run variance:** the requirement count on one benchmark document ranged from 35 to 61 across runs.
  - **Silent regression:** one week the 99-page NHS contract dropped from 366 to 184 extracted requirements with no error raised. *(VENDOR)*
  - **Citation verifier:** it "confirms the quote exists in the source". It produced false negatives on whitespace and hyphenation, was rebuilt, and has "its own eval set". A hand check found 27 of 27 citations grounded. *(VENDOR, re-verified)*
  - **Drafting safeguards:** drafting is **gated on the verdict**, a capability-fit check runs against the profile, and requirements the bidder can't meet go to a "partner slot", with a banner showing the count. *(VENDOR, re-verified)*
- No public reviews were found. Capterra, GetApp and SoftwareAdvice returned 403, and there's nothing on G2, Trustpilot or Reddit. Product Hunt has 2 upvotes.
- No job ads, GitHub or UK Companies House entity were found. The company is US (Delaware) with a location in Mostar, Bosnia and Herzegovina.

**E – Infrastructure**
- The main site and `api.` share one Google IP (34.8.103.212). `au.` is on a separate Google IP. `track.api.` is a CNAME to Resend, which sits behind CloudFront. *(CONFIRMED, re-verified)*
- Mail is Google Workspace, SPF includes Amazon SES, and DMARC is set to p=reject. *(CONFIRMED)*
- The domain was registered on 2025-06-29 at Namecheap. *(CONFIRMED)*
- No status page was found. crt.sh was down, so certspotter was used, giving 5 hostnames. *(CONFIRMED)*

**Developers page** (orchestrator re-verified)
- The analysis API returns `202` with an `analysis_id`, and the client then polls. There are no webhooks.
- Each requirement carries `original_text` (in the source language), `document`, `page`, `citation`, `quote_verified`, `page_is_approximate` ("as it is in Word files") and `severity` (CRITICAL/HIGH/MEDIUM/LOW).
- The `verdict` field is "the bid call, or null when the analysis made none". *(VENDOR)*

---

## 3. Test run results (workstream F, free paste checker, 3 runs, 2026-10-14)

**Setup.** Tool: `/tools/compliance-check`. The cookie banner was declined; there was no sign-up and no error or rate limit.
- **Inputs:** the vendor's own "Load NHS example" (8 NHS contract requirements plus an example draft answer that fills both fields), and one synthetic draft I wrote for a fictional company, "Example Care Services Ltd (TEST DATA ONLY)".
- **Runs:** Run 1 = the vendor example unchanged. Run 2 = the synthetic probe with a 50-word limit. Run 3 = an identical repeat of Run 1.
- **Capture method:** the browser pane's network list didn't show cross-origin calls, so the subagent added a passive in-page `fetch` observer that recorded request and response. It didn't inspect or alter any JS bundles. You should know about this, since it sits close to the terms' §5 clause.

**Network flow (CONFIRMED)**
- On page load the page calls `GET api.ailucius.com/api/v1/plans`.
- Each check is **one synchronous call**, `POST api.ailucius.com/api/v1/growth/guest-compliance`, with the JSON body `{requirements_text, draft_text, word_limit}`.
- It returns a single `application/json` response after 5.8–14.7 s. There's no job ID, polling or streaming.
- While waiting, the page shows only the text "Checking your draft…".
- The `/growth/` path suggests the tool sits in the marketing-funnel service, separate from the main analysis pipeline (SPEC).

**Response schema (CONFIRMED)**
```
{tender_title, score, scored, checklist:[{requirement, addressed:bool, evidence:str|"NOT FOUND"}],
 gaps:[requirement…], addressed_count, total_checked, word_count, word_limit, word_limit_ok, word_limit_over_by}
```

**What the page shows (CONFIRMED)**
- A summary row: "50% requirements met · 4/8 addressed · 123 words".
- When over the word limit, a red warning: "Over the 50-word limit by 41 words."
- The checklist in input order. Each item has either a green check plus "Evidence: …" or a red ✕ plus "NOT FOUND".
- A footer, "This is one answer checked against requirements you pasted", and the call to action "Analyse a full tender free".

**Results by requirement**

| Requirement | Run 1 (vendor example) | Run 2 (synthetic probe) | What Run 2 probed | Verdict on the tool |
|---|---|---|---|---|
| SC11.5 discharge summary to GP within 24 h | NOT FOUND | **addressed**: "Partner organisation Sample Health Partners issues discharge summaries…" | Partner-framed claim with no 24 h commitment | **False positive**: credited a partner's activity and ignored the missing time limit |
| SC21 CQC registration for all regulated activities | addressed | addressed | Registration claimed without "all activities" | Lenient |
| SC28.1 DSPT "Standards Met" | addressed | NOT FOUND | Vague "good data protection practice" | Correct |
| GC9 EL insurance ≥ £5 m | NOT FOUND | NOT FOUND | **Wrong value (£2 m)** | Correct status, but just "NOT FOUND", not "does not meet", and no reason given |
| SC3.2 all clinical staff registered | addressed | **addressed**: "…nurses hold current professional registration with the NMC" | Only nurses covered | **False positive**: no "partial" exists |
| SC12 monthly KPI reports | addressed | NOT FOUND | Omitted | Correct |
| IG7 breach to Commissioner **and** ICO in 72 h | NOT FOUND | NOT FOUND | **Internal contradiction**: Commissioner "5 working days" vs ICO "72 hours" | Status defensible, but the contradiction isn't surfaced |
| SC4 complaints procedure | NOT FOUND | NOT FOUND | Generic "comply with all applicable NHS regulations" | Correct |
| (none) | – | – | Irrelevant claim "We hold ISO 9001" | Ignored correctly; no requirement invented |
| **Summary** | 4/8 · 50% | 3/8 · **38%** · 91/50 words | | `score` = round(addressed ÷ total × 100) |

**Repeat test (Run 1 vs Run 3, identical inputs).** All 8 statuses were identical, and so were the score and word count. The **evidence text differed on all 4 addressed items**, e.g. "Commits to monthly activity…" vs "Submits monthly activity…".

**Key conclusions (CONFIRMED by the test)**
1. **The "evidence" is an LLM paraphrase, not a quote.** The input says "Our organisation is fully registered with the Care Quality Commission for all regulated activities we deliver". The tool shows "States full registration with the Care Quality Commission for all regulated activities delivered." The wording also changes between identical runs. This **refutes** the page's claim that it shows "the exact phrase in your draft" (VENDOR claim refuted). No string check of the evidence is applied in this tool.
2. **Only two statuses, both binary:** addressed or NOT FOUND. There's no partial, no "does not meet" (a wrong value looks the same as an omission), no "claimed but not proven", no "needs review", and no reason given for a NOT FOUND.
3. **The score is a fixed formula over the LLM's booleans:** 3 of 8 gives 38. Statuses were stable across the repeated run. The word count is deterministic.
4. **No fix guidance:** a gap list is returned (and shown first, per the marketing page), but there are no actions, no criticality and no score impact. It ends with an upsell.
5. **Lenient matching causes false positives:** an activity delivered by a partner, and partial coverage (nurses only), both counted as "addressed". That's the riskiest kind of error for a bidder, because the checker passes a requirement that an evaluator would fail.
6. **Contradictions inside the bid aren't detected.**

The guest upload analysis (`/guest-analysis`) **has not been tested yet.** No public tender PDF could be verified, because the UK portals returned 403 to automated requests. That test needs your approval of a specific file.

---

## 4. Inferred architecture

```
 [Browser: Next.js on Cloud Run behind Google Frontend]
     |  Google OAuth / first-party JWT (/api/v1/auth/refresh)
     |  upload (guest ≤25MB; paid ≤40MB/file)  --(SPEC: signed URL)-->  [GCS bucket, AES-256]
     v
 [api.ailucius.com  (Cloud Run, europe-west1 | AU stack)]
     |  POST pack -> 202 + analysis_id      <-- client polls status (no webhooks)
     v
 [Job queue (tech unnamed; SPEC Cloud Tasks) -> worker, 167ms pickup; LLM off request path (120s gateway limit)]
     |
     v
 [Parsing] PDF text layer + Gemini multimodal ("Google's vision models") for scans/tables
     |      page-range chunking; Word = approximate pages
     v
 [Extraction] Gemini (Pro tier, STRONG), best-of-N per chunk -> union -> dedupe/conflict pass (LLM, semantic)
     |      fields: original_text, doc, page, category, severity(+reason)
     v
 [Citation verifier] string search of quote in pack text -> quote_verified flag (+ eval set)
     v
 [Scoring] risk 0–100 = f(#requirements, severity)  |  verdict Bid/Caveats/Skip vs company profile (nullable)
     v
 [Drafting] gated on verdict -> capability-fit vs Bid Library (Gemini per-company doc store, STRONG: File Search)
     |      gaps -> "[TO COMPLETE: …]" markers, partner-slot banner
     v
 [Exports] Word / PDF / Excel matrix / ZIP compliance pack (CSV + audit trail)

 Side systems: AlloyDB (catalog + embeddings 3072-d, ScaNN, ai.rank) for tender search;
 nightly ingestion from 13–15 portals; MCP/REST Agent API; ops agent via MCP Toolbox.
 Telemetry: Sentry (EU), PostHog (US), GA4. Email: Resend + SES.
```

| Stage | What it does | Likely technology | Evidence | Confidence |
|---|---|---|---|---|
| Ingestion and parsing | Takes PDF, Word, Excel, images and ZIP, scans included; previews only the first pages of very large packs | PDF text layer plus Gemini multimodal for scans and tables; Word gets estimated page numbers | FAQ, llms-full, extraction blog, `page_is_approximate` in API | Formats VENDOR; Gemini vision for OCR STRONG; Document AI not seen (SPEC: not used) |
| Requirement extraction | Splits by page range, runs best-of-N extraction, merges, flags empty chunks | Gemini, likely a Pro-tier model, with context caching shared across passes | Extraction blog; trust page ("pending regional Pro-model deployment"); 24 h cache | Method VENDOR; model tier STRONG; version not found |
| Cross-references ("see Annex 3") | Reads every annex; cites by document part | Not described | FAQ, extraction blog | Resolution method not found |
| Bid document intake and matching | Free checker: pasted requirements vs one pasted answer. Word add-in: draft vs requirements. Paid list: user sets status manually | LLM comparison of draft text with requirements (SPEC); no matching of uploaded bid files found | Checker page, llms-full, integrations | VENDOR; full bid-package check **not found** |
| Citations | Each requirement carries a quote and page; a checker confirms the quote exists in the pack text. **Bid side, CONFIRMED by F:** the checker's "evidence" is an LLM paraphrase that changes between identical runs, with no verbatim check | LLM outputs quote and page; deterministic string search, normalisation added after whitespace and hyphenation false negatives | "Refuse to lie" blog, `quote_verified` field | VENDOR, re-verified; matching algorithm STRONG (string-based) |
| Disqualifiers | Pass/fail and eligibility gates, flagged as high severity inline | LLM classification ("shall/must" → high) | how-it-works, example page | VENDOR; no separate list (CONFIRMED on example) |
| Bid document checks (validity, signatures, consistency) | Not found | – | A, D, A2 | Not found |
| Readiness and bid/no-bid | Risk score from requirement count and severity; verdict "scored against your profile", which can be null | Risk score: STRONG, a formula over LLM-assigned severities. Verdict: SPEC, LLM judgement (a nullable output suggests the model may decline) | how-it-works, API docs | See D1 |
| Fix recommendations | Gap list shown first; inline "[TO COMPLETE]" markers; blog advises clarification questions for conflicts | – | Checker page, how-it-works, conflicts blog | VENDOR; no action types or score impact |
| Re-check and versions | Pack plan allows reruns for 60 days; draft history with one-click restore | – | Pricing, trust | VENDOR; no score-delta history found |
| Counterparty checks | Not found | – | A | Not found |
| Async jobs | 202 plus analysis_id plus polling; dedicated queue; "real-time progress"; 4–10 min full analysis, under 1 min free preview | Queue tech unnamed (SPEC Cloud Tasks); the guest preview likely streams (bundle getReader) | Developers page, what-breaks blog, llms-full, bundle | VENDOR; streaming SPEC |
| Hosting and retention | GCP EU (AU on request); GCS plus AlloyDB; retention configurable 30–3,650 days; unsaved guest files deleted after 7 days | GCP | Trust, privacy, DNS, CSP | VENDOR plus CONFIRMED (Google IPs, `*.run.app` CSP) |

---

## 5. Models and vendors

| Item | Known (VENDOR / CONFIRMED) | Suspected |
|---|---|---|
| LLM | Google Gemini only, via Vertex AI global endpoint and AI Studio (trust page) | A Gemini **Pro** model for analysis (STRONG, from "pending regional Pro-model deployment"); exact version not found |
| Embeddings | "The Gemini embedding model", 3,072 dimensions | `gemini-embedding-001`, whose default output is 3,072-d (STRONG) |
| OCR | "Google's vision models"; vision pass for tables | Gemini native multimodal reading, not Document AI or Tesseract (STRONG: no other provider named, "no other AI provider") |
| Bid Library store | "document store in Google's Gemini API", per company | Gemini API File Search store (STRONG) |
| Reranking | AlloyDB `ai.rank` | Vertex ranking model behind it (SPEC) |
| Other | Praex: separate early-stage product on self-hosted open models (blog) | Not used in Lucius today (VENDOR says no other provider) |
| Conflicting claim | A DolphinVoice directory lists "Claude models" | **Rejected**: contradicts the vendor's trust page |

Cost signals: an embedding rebuild of 115,820 records cost about $3 (VENDOR). Cost per tender or per page was not found.

---

## 6. Spec comparison (our supplier-side MVP vs Lucius)

| Our feature | How Lucius does it | Evidence | What we'd add |
|---|---|---|---|
| Requirements checklist from the tender | Yes: requirement map with category, High/Med/Low (API also has CRITICAL), and a reason | how-it-works, API | IDs T-### / U-###, Mandatory/Scored/Desirable typing, AI confidence |
| Quote and page per item | Yes: verbatim quote and page, `quote_verified`, `page_is_approximate` | API, blog | Copy the verified flag. Also verify quotes on the **bid** side, not only the tender side |
| Bid vs requirement check, line by line | **Partial.** Paste checker: one answer, addressed/missing, with quoted evidence from the draft. Word add-in: draft vs requirements. Paid list: status set **by the user** | Checker page, llms-full, integrations | Automatic matching of a **multi-document bid package** (proposal, certificates, passports, licences) to each requirement, with page and quote on both sides. This is the core differentiator |
| Five statuses, including "Needs human review" | Two statuses (addressed/missing) in the checker; open/addressed/not met (manual) in the list | Same | All five statuses, with "Not confirmed" and "Needs human review" as separate states |
| OCR for scans and phone photos | Scans and images accepted; Gemini vision | FAQ, llms-full | Phone-photo quality handling (150 dpi), deskewing, page-level OCR confidence |
| Compliance % and readiness verdict | 0–100 **risk** score plus a Bid/Caveats/Skip **bid decision** (about the tender, not bid readiness). The free Readiness Checker is a 16-question self-assessment | how-it-works, tools | A readiness verdict on the **bid** (Ready / Needs revision / Rejection risk), a hard rule on mandatory items, and weighted % split into goods and bidder |
| Fix recommendations with action types | Gap list first; "[TO COMPLETE]" markers; clarification advice in the blog | Checker, how-it-works | Action types, criticality, score impact, and a forecast score after fixes |
| Re-check loop and version history | Pack reruns for 60 days; draft history with restore | Pricing, trust | Recalculate only affected items; score history (e.g. 72% → 94%) |
| Document validity and consistency checks | Not found (only certifications vs the library) | A, A2 | All of it: validity on the submission date, signatures and stamps, name and STIR consistency, translations, contradictions between documents |
| Uzbek and Russian | Not found; reads "any language" but drafts English only | FAQ, EU/DE pages | Full support in UZ Latin and Cyrillic, RU and EN, plus STIR. A market gap |
| Multi-bidder comparison | Not shown | – | Out of scope for the supplier side |

---

## 7. Decisions D1–D6

| # | Answer found | Evidence | What would settle the open part |
|---|---|---|---|
| **D1** Is the verdict rule-based or LLM? | **Hybrid; the score part is now CONFIRMED.** In the free checker the LLM decides each status, and `score` is a fixed formula: round(addressed ÷ total × 100), e.g. 3/8 → 38 (CONFIRMED, F Run 2). For the paid analysis: The risk score is "worked out from how many requirements there are and how serious they are" (VENDOR), so likely a formula over LLM-assigned severities (STRONG). The verdict is "scored against your profile" and can be **null** (VENDOR), which suggests LLM judgement (SPEC). Drafting is gated on the verdict (VENDOR) | how-it-works (re-verified), API docs (re-verified), refuse-to-lie blog | F: run the same tender twice. If the verdict changes between runs, an LLM decides; if the risk score is stable, it's a formula. Check whether one failed CRITICAL forces Skip |
| **D2** Is the finished bid checked line by line? | **Confirmed for the free tool; still open for paid.** The checker takes one pasted answer, gives binary statuses, credits paraphrased evidence without verifying it, and had false positives on partner-delivered and partial claims (F). Beyond that: **Hypothesis largely confirmed.** The main product analyses the tender. Bid-side checks are limited to one pasted answer (free checker), a draft inside Word (add-in), and a **user-set** status list. No check of a full uploaded bid package with evidence documents was found | Checker page, llms-full verbatim, integrations (all re-verified) | F on the paste checker. Also read the Word add-in listing on Microsoft AppSource to see whether it reads attached evidence |
| **D3** Is "claimed, not proven" a separate state? | **No (CONFIRMED in the free checker).** Only addressed or NOT FOUND exist. A bare claim counts as addressed, and a wrong value (£2 m vs £5 m) looks identical to an omission. Closest: the tender-side `quote_verified=false`, and the "partner slot" for requirements the bidder can't evidence while drafting | API docs, refuse-to-lie blog | F: paste an answer that *claims* ISO 9001 without evidence and see whether it's marked "addressed" |
| **D4** Fix recommendations with action types and score impact? | **No, only a list of problems (CONFIRMED in the free checker: `gaps[]` is just the requirement text).** Gap list first, "[TO COMPLETE: 2 references]" markers, generic advice to raise clarification questions on conflicts. Lucius doesn't suggest fabricating ("never filled with made-up facts"), which matches our rule | Checker page, how-it-works | F output screenshots |
| **D5** OCR engine and LLMs | **Gemini only** (VENDOR, re-verified). Likely a Pro tier for analysis, `gemini-embedding-001` (3,072-d) for embeddings, Gemini multimodal as "OCR" (STRONG). No mixed-language evidence beyond "reads any language, drafts English" | Trust, FAQ, blogs | F with a scanned PDF (timing and page accuracy). Version strings would only show up in an error message or the docs |
| **D6** Do they store and reuse bidder documents? | **They store them; they say they don't train on them.** Uploads go to GCS. The Bid Library sits in a per-company Gemini document store "until deleted" and is reused for drafting. Gemini may cache text for 24 h. Retention is configurable from 30 to 3,650 days. Unsaved guest uploads are deleted after 7 days. "Never used to train" covers paid use; the free tier isn't addressed. No public DPA | Trust, privacy (re-verified trust) | Ask for the "policy pack" (only on request; that's a contact request, so it needs your approval) |

---

## 8. UX patterns worth copying

No screenshots yet, because F hasn't run. The references below are public pages.

0. **Two-number summary row** (F, all runs): "50% requirements met · 4/8 addressed · 123 words", with a red inline warning "Over the 50-word limit by 41 words". *Why it works:* readiness at a glance. Copy it, but add a separate "mandatory failed" count so a high % can't hide a rejection risk.
1. **Gaps first** (`/tools/compliance-check`): unmet requirements are listed before the met ones. *Why it works:* the supplier's next action is right at the top. Use it to order our fix list by criticality.
2. **Evidence quoted from the bidder's own text** (checker): each "addressed" mark shows the exact phrase that satisfies it. *Why it works:* the user can check it in seconds and can't be fooled by a bare green tick. Apply it to both sides: the tender quote **and** the bid quote.
3. **Verified-quote tick plus click-to-open source** (`/examples/rossendale-play-area`, how-it-works): a requirement whose quote failed verification shows no tick, and every page reference opens the source. *Why it works:* it makes hallucinations visible. This is our per-item source viewer.
4. **Severity plus a one-line reason** (how-it-works): "shall/must" items are High, each with a short reason. *Why it works:* it explains the ranking without overloading the user.
5. **Verdict that names the requirements deciding it** (llms-full): the verdict lists the items that drove it. *Why it works:* the user trusts it because they can see why. Copy this for "Rejection risk", listing the failed mandatory items.
6. **Inline gap markers in drafts** ("[TO COMPLETE: 2 references]") plus a **banner count** of requirements without evidence. *Why it works:* gaps stay visible and nothing gets invented to fill them. This fits our rule against changing facts.
7. **Two-speed results**: a free preview in under a minute (risk score, key risks), then the full analysis in 4–10 min with "real-time progress". *Why it works:* the user gets something to look at quickly, while the long job runs asynchronously.
8. **"Load NHS example" button** on the checker: try it with zero setup. *Why it works:* it gets people to their first result. Offer a sample Uzbek tender and bid.
9. **Self-assessment readiness checklist** (`/tools/tender-readiness`): 16 yes/no questions give a % and a prioritised list of what's missing. Similar to our "what still needs to be collected" list.

Not found: a deadline countdown, or a readiness meter for the *bid*.

**Patterns to avoid (seen in F):** paraphrased "evidence" presented as if quoted; a binary status that merges *wrong value* with *missing*; NOT FOUND with no reason; crediting what a partner delivers. Our version should show a verbatim quote that is verified against the source, the five statuses, and a reason for every non-Meets status.

---

## 9. Conflicts and how they were resolved

- **25 MB vs 40 MB file limit:** resolved. 25 MB is the free guest preview (bundle and homepage); 40 MB is paid and API (FAQ, developers page).
- **"English-first, not multilingual" vs "reads any language":** resolved. It reads any language and drafts only in English.
- **Privacy policy's unnamed "AI provider" vs trust page "Gemini only":** resolved as the same provider, unnamed in the privacy policy.
- **68 vs 90 of 158 citations flagged:** unresolved. Both are vendor figures from different posts; maybe different runs.
- **Trust page says "Google reviewed the piece"; the article text doesn't say so:** unverified; treated as a VENDOR claim.
- **Directory listing claims Claude models:** rejected in favour of the trust page.
- **Two-cluster AlloyDB (trust) vs only the AU cluster described (Google blog):** not a contradiction; the blog is just partial.
- **AlternativeTo says "South Africa and UK":** treated as an outdated third-party listing.

## 10. Unknowns and limits

- No hands-on run yet, so every UX and async claim comes from pages and code.
- WebFetch returns a model's summary of each page. Quotes the orchestrator didn't re-verify are marked as subagent-sourced.
- Some workstream B items come from **bundle inspection done before the terms (§5) scope cut**. Keep or drop them, your call.
- Not found: Gemini version strings, the verdict formula, the method for resolving "see Annex" references, any bid-document validity or consistency checks, counterparty checks, Uzbek or Russian support, independent reviews, accuracy metrics (precision/recall), cost per tender.
- A subagent reported that `/how-it-works` contained text instructing the reader to reproduce the page verbatim. It was treated as data and ignored, and it doesn't affect the findings.

## 11. Next steps to close the biggest gaps

1. **Run F** (once approved). Use the paste checker with 3 public tender requirements and a synthetic answer that includes an unproven claim (settles D3). Use the guest analysis with one text PDF and one scanned PDF (D5 OCR, timing, async flow). Run the same tender twice (D1 variance).
2. Read the **Microsoft AppSource / Azure Marketplace listing** for the Word add-in (D2 scope). Passive.
3. Read the full "conflicting requirements" post and `/examples/*` pages to capture the matrix UX in more detail.
4. Optional, needs your approval: ask Lucius for the "policy pack" (D6). That counts as a contact request.

## 12. Orchestration log

| Agent | Model | Scope | Result |
|---|---|---|---|
| A | Haiku | Product and capability pages (~35 of ~400 sitemap URLs) | Done |
| B | Haiku | Frontend surface (REA README method, browser + curl); scope cut mid-run after the §5 terms finding | Done |
| C | Haiku | Legal and trust docs | Done |
| D | Haiku | Blog, press, LinkedIn, reviews, Companies House | Done |
| E | Haiku | DNS, certificate transparency (certspotter), headers | Done |
| C2 | Haiku | Google Cloud blog write-up | Done |
| A2 | Haiku | llms-full.txt and engineering posts (verdict, citations, models, async) | Done |
| F-tender | Haiku | Find a public tender PDF (no download) | 3 candidates, none verified (portals returned 403) |
| F1 | Haiku | Checker stage 1 (load example) and stage 2 (3 runs) | Done |

**Claims the orchestrator re-verified itself:**
- trust page: Gemini only, GCP regions, subprocessors, certifications
- terms §5
- headers, CSP and DNS (curl/dig)
- checker page fields and outputs
- llms-full.txt: user-set statuses, scans, verdict, timing
- how-it-works: risk-score basis, verdict, gap markers
- refuse-to-lie blog: citation checker, verdict gating
- developers page: 202 + polling, `quote_verified`, nullable verdict

Downgraded to SPEC: Cloud Tasks; signed-URL uploads; LLM-decided verdict; streaming progress.
