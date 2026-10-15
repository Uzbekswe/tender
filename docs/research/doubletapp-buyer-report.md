# Doubletapp Tender AI: research report on the buyer-side workflow

Source: Haiku 5.5 subagent workstreams A–F plus orchestrator follow-up, as pasted by the user. Saved verbatim for analysis. Not yet re-verified beyond what the report itself states.

**Confidence labels:** **CONF** = seen directly in a primary source · **VENDOR** = Doubletapp says so, not independently checkable · **STRONG** = strong inference from several independent clues · **SPEC** = speculation · **SUB** = reported by a subagent; not re-checked by the orchestrator.

## 1. Product summary (buyer view)

Tender AI is an "AI-оценщик тендерных подрядчиков" (AI evaluator of tender contractors). It is built for the buyer's procurement team. The landing page also has a smaller section offering bidders templates and recommendations for their own documents.

The buyer uploads the tender package and the contractors' packages. The system does five things:
1. Turns the documents into one plain-text version, using OCR where needed.
2. Uses an LLM to extract a checklist of requirements.
3. Checks each requirement for each contractor in a separate LLM call. The verdict is one of three statuses, with a quote and a link to the document.
4. Uses an LLM to rank the contractors and recommend one of three outcomes: sign the contract, add to the shortlist, or reject.
5. Produces a report for the committee.

Deployment is on-prem (open-weight models served with vLLM) or SaaS + API. Doubletapp also says it is listed in Cloud.ru's partner catalog.

**Main sources:**
- Habr article 1018940, "Тендеры без ошибок…", 2026-04-06 (CONF, read from the raw HTML).
- workspace.ru and RUWARD case pages, case delivered Sept 2025.
- tender.doubletapp.ai, captured 2026-10-14.
- Doubletapp Telegram post 1312, 2026-07-06.

## 2. Answers to Q1–Q11

| Q | Answer | Conf. |
|---|---|---|
| **Q1 Pipeline** | 1) Parse to unified text, with OCR for scans. 2) LLM extracts the checklist. 3) Each requirement is checked for each bidder in its own LLM call. 4) LLM ranks and recommends. 5) Committee report (landing page). **Human role:** "человек ставит задачи, контролирует процесс и оценивает результат" ("the person sets tasks, controls the process and judges the result"). The case page says the checklist is built «без участия человека» ("without human involvement"). No approval gate and no status override are described. The committee keeps the rejection decision (author's comment on Habr). | CONF / VENDOR |
| **Q2 Ingestion/OCR** | Formats: PDF, DOCX, Excel, scans, archives, folders. "OCR when needed" plus "preprocessing and normalization". **Not found:** the OCR engine, the parser libraries, merged-cell tables, multi-page TIFF, and any measure of OCR quality. | VENDOR |
| **Q3 Requirement extraction** | The LLM produces "dozens of clearly formulated items" with numbering. **Not found:** how cross-references like "see Annex 3" are resolved, how duplicates or contradictions are found, how scored criteria and weights are read. Extraction quality: 93% accuracy with an LLM judge (Habr), or 95% completeness (case page). | VENDOR |
| **Q4 Matching** | One LLM request per (requirement × company). Three statuses (see D3). **Not found:** "or equivalent" handling, or numbers compared in code. One related comment: the system checks that declared experience matches the procurement subject, but cannot check whether the contracts are real. The registries for that (EIS, and the RNP register of unreliable suppliers) are still a manual step. | VENDOR |
| **Q5 Evidence** | Every verdict comes with "a quote and a link to the specific document". **Not found:** any check that the quote actually appears in the source text. Page-level location is also not mentioned; they only say "document". | VENDOR |
| **Q6 Ranking/decision** | The LLM receives all check results and produces a ranking plus sign / shortlist / reject, with a justification. **Not found:** a numeric scoring formula, weights, tie-breaking, or how disqualification works. The author advises humans to check the top candidates and says "others can be discarded right away". | VENDOR |
| **Q7 Clarifications/audit** | The product "suggests documents to request" (a list only; no drafted letters to bidders or to the customer). SaaS claims an "audit of user actions" and on-prem "full control of logs and access". **Not found:** committee protocol, version history, re-evaluation after a bidder uploads corrected documents. | VENDOR |
| **Q8 Models/infra** | **Supported providers:** DeepSeek, YandexGPT, GigaChat, and vLLM for local deployment. **Best in the project:** DeepSeek V3.1 and Qwen3-235B-A22B, on-prem through vLLM. Requests run in parallel and use prompt caching. A 50-page document takes 5–7 minutes. Installation takes 5 days on Ubuntu or Astra Linux with Docker or Kubernetes. **Not found:** hardware, cost per tender or per page. | VENDOR |
| **Q9 Evaluation** | The gold set was labelled by experts using meets / doesn't meet / needs a follow-up request. Extraction scores 93% (LLM judge) and the compliance check 92% (exact category match against the experts). Every prompt or model change must pass the benchmark before release. The case page gives 95% completeness and 93% accuracy. **Not found:** test-set size, error breakdown, failure cases. | VENDOR |
| **Q10 Buyer context** | Customers named only generically: a "large industrial company" / "industrial companies" (Telegram post, SUB; Habr 1006742, CONF). Segments: industrial, infrastructure, state tenders. **Pricing:** none published; it goes through a demo, pilot and "economics calculation". **Integrations claimed:** EIS, 1C, SAP, BI tools. No electronic trading platforms named. Delivery: SaaS or on-prem. The SLA is 99.5%. | VENDOR |
| **Q11 Legal/trust** | **Tender product:** no privacy policy or terms of its own. **doubletapp.ai privacy policy:** a generic 152-FZ policy for website visitors, with an unlimited processing period and a cross-border transfer clause (CONF). **Telegram 2026-07-06:** "deployed on Cloud.ru… meets 152-FZ requirements" (VENDOR). **Habr:** "meets security requirements for industry, medicine, finance", with no certification cited. **Not found:** FSTEC or ISO certifications, a Russian software-registry listing, a no-training-on-customer-data statement, retention rules, tenant isolation, or any claim of 44-FZ/223-FZ compliance. | CONF / VENDOR |

## 3. Evidence by workstream (highlights)

**A. Habr article.** I re-read the raw HTML and the A findings hold up. Corrections:
- The publication date is **2026-04-06**.
- There are three LLM phases plus a parsing step, not four "stages".
- A commenter's "7–8% error" is just their reading of the 92–93% accuracy figures. It is not a separate number.

Most useful item: the author's reply that a rejection «помечается с цитатой… Решение об отклонении остаётся за комиссией… обоснование… защитимо при жалобе в ФАС» ("flagged with a quote; the commission decides; the justification holds up in an FAS complaint").

**B. Other content.**
- workspace.ru and RUWARD case pages (CONF, re-read): checklist built "without human involvement"; same two models; 95% / 93%; "on-premise and offline".
- The workspace case lists "Клиент: Doubletapp" (Doubletapp itself), so the end customer is unnamed.
- Habr 1006742 (2026-03-06, CONF): describes an "AI-оценщик подрядчиков для промышленных компаний" with 5–7-minute analysis.

**C. Product site (REA method).**
- The site is a static one-page landing: nginx 1.28.3 + Express.
- Its only endpoint is `/api/submit-application`, the demo form. I did not call it.
- No analytics, no source maps, no status labels in the JS, no app or API hosts exposed.
- The four-step flow and the SaaS/on-prem feature list are confirmed against the raw text.
- Extra claims on the page: licence checks, sanctions screening, and the cost of delays and penalties.
- REA was **not run**. Installing it pulls third-party npm code, and the only extra thing it would add is live runtime capture. On a static landing page that capture would show nothing beyond what was already found.

**D. Legal and procurement.**
- The privacy policy exists. D's "empty page" result was wrong; the text is inside the Next.js page data. I verified it.
- **Uzbekistan ZRU-547, Article 27-1** (CONF, lex.uz; amended by ZRU-1125 on 2026-03-26):
  - In-country storage is mandatory only for biometric data, genetic data and data on telecom-service users.
  - Other personal data may go abroad under one of several listed conditions, such as an adequacy finding, standard contract clauses, or the operator following international standards.
- **ZRU-684** (SUB, from lex.uz):
  - Art. 20: the commission sets its own evaluation criteria, has an odd number of members and decides by majority.
  - Art. 36: bidders must have no tax debt and must not be on the unscrupulous-performers register.
  - Art. 41: the customer must answer clarification requests within 2 working days.
- **RU Decree 2604** (SUB, via a Kontur mirror): for goods, price carries at least 70% of the weight; scores are averaged across commission members.

**E. Infrastructure and hiring.**
- `tender.doubletapp.ai` resolves to 87.199.205.249: a VDSina VPS in Amsterdam (AS216071) (CONF). This is the landing page only, not evidence of where SaaS data lives.
- `doubletapp.ai` is hosted on Timeweb in St Petersburg; its email runs on Yandex.
- No ML or OCR vacancies are open. None of the 13 visible GitHub repos (of 39) relate to tenders.
- crt.sh failed with 502 errors three times.
- E said Habr post 870034 described the tender system. That is **refuted**: it is a Dec 2024 ML article digest.

**F. Gap-fill.**
- Telegram post 1312 (CONF, re-read): "listed in the Cloud.ru partner catalog", "10x faster", "procurement costs down up to 40%", "on Cloud.ru… 152-FZ".
- The listing itself was not found on cloud.ru.
- No OCR engine, checklist approval step, code-based numeric comparison, hardware or named customer appears anywhere.

## 4. Inferred architecture

```
[Upload: tender pkg + N bidder pkgs (PDF/DOCX/XLS/scans/archives)]
        │  unpack archives → per-format text extraction → OCR if scan
        ▼
[Unified text per document]  (no layout/table model mentioned)
        │
        ▼  LLM phase 1 (one or few calls)
[Checklist: numbered atomic requirements]   ← no human gate documented
        │
        ▼  LLM phase 2: for each (requirement × bidder) → 1 call
        │  prefix = system prompt + bidder docs (cached); suffix = 1 item
        │  run in parallel on vLLM / provider API
[Verdict ∈ {meets, doesn't meet, insufficient data} + quote + doc link]
        │
        ▼  LLM phase 3: all verdicts for all bidders → 1 call
[Ranking + sign / shortlist / reject + justification]
        │
        ▼
[Committee report w/ source links; "documents to request" list]
        │
        ▼
[Human committee decides]   (EIS/RNP registry checks: manual)
```

| Stage | What it does | Likely tech | Evidence | Conf. |
|---|---|---|---|---|
| Ingestion | Accepts PDF, DOCX, Excel, scans, archives and folders; "assembles a dossier" | Standard parsers (unnamed) | A, B, C | VENDOR |
| OCR/layout | OCR on scans, "normalization" | Unknown engine. Output is plain text, so table structure is probably lost | A, B, F | VENDOR (engine: SPEC) |
| Checklist extraction | Builds dozens of atomic, numbered items | LLM | A, B | VENDOR |
| Human approval | None documented; the case page says "without human involvement" | — | B | VENDOR (absence) |
| Matching | One call per (item × bidder); bidder documents in the cached prefix | vLLM prefix caching. This implies whole-document context rather than retrieval | A | VENDOR; context detail STRONG |
| Verdicts | 3 statuses. "Insufficient data" works as "need to request more" | LLM | A, eval labels | VENDOR |
| Citations | Quote + document link per verdict | LLM-generated. No sign the quote is verified | A, comments | VENDOR; no verification: SPEC |
| Ranking | LLM sees all results and ranks them | LLM, no formula | A | VENDOR |
| Recommendation | Sign / shortlist / reject; committee decides | LLM + human | A, comments | VENDOR |
| Clarifications | List of documents to request | LLM | A, C | VENDOR |
| Report/audit | Committee report with links; SaaS action audit; on-prem log control | — | C | VENDOR |
| Models | DeepSeek V3.1 and Qwen3-235B-A22B on-prem; DeepSeek, YandexGPT and GigaChat APIs supported | vLLM | A, B | VENDOR |
| Evaluation | Expert gold set, LLM-judge for extraction, category match for verdicts, release gate on the benchmark | — | A, B | VENDOR |
| Hosting/privacy | On-prem in 5 days (Ubuntu/Astra, Docker, K8s); SaaS on Cloud.ru, claimed 152-FZ compliant | — | C, F | VENDOR |

## 5. Models and vendors

- **Known (VENDOR, consistent across Habr, workspace.ru and RUWARD):**
  - The two winning models are DeepSeek V3.1 and Qwen3-235B-A22B, running on vLLM inside the customer's environment.
  - Supported providers: DeepSeek, YandexGPT and GigaChat.
- **Suspected:**
  - The Cloud.ru SaaS probably serves the same open-weight models, either on Cloud.ru GPUs or through Cloud.ru's model service (SPEC).
  - Hardware: running a 235B mixture-of-experts model or DeepSeek V3.1 (671B total parameters, from my general knowledge) means a multi-GPU server. Roughly 4–8 × 80 GB GPUs, depending on how the model is compressed (SPEC, my estimate).
  - The OCR engine is unknown. No evidence points to any particular one.

## 6. Spec comparison

| Our feature | Doubletapp | Evidence | What we add |
|---|---|---|---|
| Checklist with quote + page per item | Numbered checklist; no per-item source quote, page or confidence mentioned | A, B | IDs (T-/U-), Mandatory/Scored/Desirable type, source page + quote, confidence |
| Human checklist approval | **None documented**; "without human involvement" | B | An approval gate (a differentiator) |
| Bidder check per item | Yes, one LLM call per item × bidder, with prompt caching | A | Same pattern, plus retrieval for large packages |
| 5 statuses incl. "Needs human review" | **3 statuses**, no review status | A | "Partial", "Not confirmed" vs "Does not meet", "Needs review" routing |
| OCR for scans and phone photos; Uzbek | OCR exists, engine unknown; no Uzbek, phone-photo or dpi claims | A, B | Benchmark OCR on Uzbek Latin and Cyrillic; check the LLMs on Uzbek |
| Compliance %, separate goods and bidder scores, rejection flag | Not described; ranking is done by the LLM | A | Deterministic weighted scoring in code; mandatory-fail flag |
| Clarifications to bidders and customer | Only a "documents to request" list; nothing for contradictions inside the tender | A, C | Draft letters to bidders; contradiction detection plus questions to the customer |
| Multi-bidder matrix + ranking | Ranking yes; no matrix view mentioned | C | Requirement × bidder matrix |
| Committee report + export | "Report for the committee" with links; formats not stated | C | PDF, XLSX, DOCX, JSON |
| Audit trail / reproducibility | SaaS action audit; prompt changes gated by a benchmark; no versioning or re-evaluation | A, C | Versioned runs, pinned models and prompts, re-evaluation diff |
| Tenant isolation / retention | Not stated; RBAC and 2FA claimed | C | Explicit isolation, retention settings, no training on customer data |

## 7. Decisions D1–D6

| # | Answer | Evidence | What would close the gap |
|---|---|---|---|
| **D1** Auto-recommend or assist? | **Both.** The LLM gives sign / shortlist / reject per bidder, and the committee keeps the final decision. The author also suggests discarding low-ranked bidders without human review. | Habr article (VENDOR); author comments (VENDOR) | A demo: is the recommendation editable, and is the committee's decision logged? |
| **D2** Numbers compared by LLM or by code? | **Not stated.** Every check is described as an LLM call, and no code path is mentioned. Leaning toward LLM. | A, F | Ask in a demo, or test with a numeric edge case (e.g. "≥ 2.5 kW" against "2500 W") |
| **D3** Statuses and "insufficient data" | **3**: соответствует / не соответствует / недостаточно данных. "Insufficient data" is not defined; in practice it means "need to request more" (the expert labels used «нужно дозапросить» / «неясно»). | Habr article (CONF text, VENDOR claim) | Their status definitions or labelling guide (not public) |
| **D4** OCR and LLMs (scans, Uzbek) | **OCR: not found.** **LLMs:** DeepSeek V3.1 and Qwen3-235B-A22B (best); YandexGPT and GigaChat supported. **Uzbek:** nothing. Qwen3 advertises broad multilingual coverage (SPEC, my general knowledge; check Uzbek ourselves). | A, B, F | Our own bake-off on Uzbek scans |
| **D5** Human checklist approval | **No.** None is documented, and the case page states the opposite. | B (CONF text) | Demo |
| **D6** Local data vs external APIs | **Both modes.** On-prem with open-weight models via vLLM, "independent of external APIs" (case client). SaaS on Cloud.ru, claimed 152-FZ compliant. Provider APIs supported. No statements on retention or training. | A, B, F (VENDOR); landing page on a Dutch VPS (CONF, landing only) | Their data-processing agreement, or a SaaS architecture document |

## 8. Lessons for our build

1. **Check one requirement per call, with bidder documents in a cached prefix.** This is their main quality lesson ("сильно нарастить качество", "greatly raised quality") and also their cost lever (Habr, phase 2). Budget for very long context per bidder, or add retrieval for large packages; they don't say how they handle that.
2. **Measure extraction and matching separately.** They score matching on the experts' requirements, not on the LLM-extracted ones, so the two kinds of error don't mix (Habr, "Как мы проверяем качество"). Use an LLM judge for matching extracted requirements against the gold set.
3. **Gate every prompt or model change on the gold benchmark** (Habr). Publish the test-set size; they don't, and their figures drift between sources.
4. **Big open-weight mixture-of-experts models won for them** (DeepSeek V3.1, Qwen3-235B). That means expensive on-prem hardware. Test whether a smaller model with code-based numeric checks gets close.
5. **A quote per verdict is what makes the result defensible in an appeal** (author comment about FAS complaints). They don't check that the quote really exists. We should, with fuzzy matching against the OCR text and page.
6. **Registry checks are their admitted gap** (author: EIS/RNP lookups are manual). For Uzbekistan, the bidder requirements in ZRU-684 Art. 36 (no tax debt, not on the unscrupulous-performers register; SUB) make a STIR-based lookup an obvious differentiator.
7. **Don't copy "discard the others immediately".** Rejection grounds are legally defined in both Russia and Uzbekistan. Rejection should need a failed mandatory item, a quote and human confirmation.
8. **Rank in code, not by the LLM.** Under ZRU-684 Art. 20 the commission sets the criteria (SUB). Weighted scoring in code is reproducible and can be audited; ranking by an LLM is neither.

## 9. Conflicts and how I resolved them

- **Accuracy figures:** 93/92 (Habr, Apr 2026) vs 95/93 (case pages, Sept 2025). The metrics are named differently ("accuracy" vs "completeness / accuracy of determining fulfilment") and come from different dates. I report both as VENDOR; neither gives a test-set size.
- **Speed claims:** 2x (Sept 2025 case and Dec 2025 Telegram post) → 5x (Habr and the site) → 10x (Telegram, July 2026). The only concrete figure is 5–7 minutes for 50 pages. I treat the multiples as marketing.
- **Deployment:** "offline, on-prem, no external APIs" vs SaaS on Cloud.ru plus API providers. I don't treat these as contradictory. The case client ran on-prem; the product supports several modes.
- **Who it's for:** the landing page has a contractor card, but everything else is buyer-side. Verdict: a buyer-side product with a secondary pitch to bidders.
- **Customer:** "Клиент: Doubletapp" vs "a large industrial company". Unresolved; most likely an unnamed customer.
- **Subagent errors I corrected:**
  - D's "privacy page is empty" (it isn't).
  - E's "Habr 870034 is about the tender system" (it isn't).
  - A's "year not shown" (it is 2026).

## 10. Unknowns and limits

- No access to the product UI, so screens are known only from landing-page text.
- Not found anywhere: the OCR engine, table handling, checklist editing, the scoring formula, quote verification, versioning, hardware, cost, pricing, test-set size, tenant isolation, retention, a policy on training with customer data, certifications, and any Uzbek or other non-Russian language support.
- crt.sh was unavailable, so no list of subdomains.
- The Russian law texts were read from secondary mirrors. The Uzbek procurement articles (ZRU-684) are SUB; I did not re-check them.

## 11. Next steps to close the biggest gaps

1. **Demo call (the user, not the agent).** Ask:
   - Can the checklist be edited or approved?
   - Are numbers compared in code?
   - Are quotes verified, and with page numbers?
   - What is the scoring formula?
   - How do re-evaluation and versioning work?
   - What are the OCR engine, hardware and price?
   - Is there an Uzbek-language pilot?
2. **Retry crt.sh later**, and check the Cloud.ru partner catalog by hand to see whether the listing exists.
3. **Run our own small benchmark:** about 20 Uzbek and Russian scans at 150 dpi, comparing 2–3 OCR engines, and Qwen3 against smaller models on our five statuses. This decides D4 for us no matter what Doubletapp uses.
4. **Re-verify ZRU-684 Articles 20, 36, 41 and 42** on lex.uz before encoding the rules.

## 12. Orchestration log

| Subagent (Haiku 5.5) | Scope | Result |
|---|---|---|
| A | Habr article and comments | Good. It worked from fetch summaries, so the orchestrator re-read the raw HTML. |
| B | Other Doubletapp content | Good. Found the workspace.ru and RUWARD cases. |
| C | Product site, passive REA method | Good. Static landing page; REA not executed. |
| D | Legal and procurement | Mixed. The privacy-page result was wrong; some Russian law IDs were guessed. |
| E | Hiring, GitHub, DNS, crt.sh | Mixed. crt.sh failed; one wrong attribution (Habr 870034). |
| F (follow-up) | Gaps: OCR, approval, numbers, hardware, customers, Cloud.ru, crt.sh | Found the Cloud.ru Telegram post; all other gaps are confirmed "not found". |

**Re-verified by the orchestrator from raw sources:**
- Habr article text and date, plus all 6 comments.
- Landing-page text and headers.
- workspace.ru and RUWARD case metrics, models, and the checklist "without human involvement" line.
- Habr 1006742 and Habr 870034 (the latter refuted).
- Telegram post 1312.
- The doubletapp.ai privacy policy.
- ZRU-547 Art. 27-1.
- DNS and hosting for the tender subdomain.

**Not re-verified (marked SUB):** ZRU-684 articles, Decree 2604 weights, the Telegram post of 2025-12-02, Doubletapp's GPT-4 meeting bot, the job boards, and GitHub.
