Use REA (Reverse Engineer Anything: https://github.com/morluto/rea) plus public-source research and hands-on testing of the free tool to investigate the BACKEND / AI side of Lucius AI, with focus on the SUPPLIER side: the company that wants to bid on a tender and must check its own bid before submitting it.

Targets:
- Product site: https://ailucius.com
- Their free compliance checker (find it on the site)

Why this research exists:
I am building a tender bid-evaluation tool (MVP) with a supplier-side use. A bidder uploads the tender pack and its own bid package, and the tool checks the bid line by line before submission, shows what to fix, and says whether the bid is ready. Before we design our pipeline, I need to learn how Lucius does the supplier side and which parts of its UX are worth copying. The output decides what we build. It is not a code-copying exercise.

Known gap (my assumption, to verify, not to take as fact): Lucius mainly analyses the tender and does not fully check a finished bid line by line against it. If that holds, that check is our main difference. Confirm or refute it with evidence.

Claims about Lucius in this prompt come from marketing summaries and are my assumptions. Treat them as hypotheses to confirm or refute.

Our supplier-side product (for comparison only):
1. Input: the tender pack (PDF, DOCX, XLS/XLSX, scans and phone photos, drawings, archives) and the supplier's own bid package.
2. Build a requirements checklist from the tender pack. Each item is atomic, with an ID (T-### goods, U-### bidder), a type (Mandatory / Scored / Desirable), a source page with a quote, and an AI confidence score.
3. For each item, match the bid documents and assign one of five statuses: Meets / Partial / Not confirmed / Does not meet / Needs human review. "Not confirmed" means claimed in the technical proposal with no supporting document. Every status needs a page reference and a quote.
4. Check the bid's own documents: validity on the submission date, signatures and stamps, consistent company details (name, taxpayer ID) across documents, required translations, and internal contradictions (for example, different model numbers in the proposal and the product passport).
5. Give a readiness verdict: Ready to submit / Needs revision / Rejection risk. Any failed mandatory item raises "Rejection risk", regardless of score. Show a weighted compliance percentage, split into goods and bidder.
6. For each problem, a fix recommendation: what is wrong, the action, an action type (add document / correct data / replace goods / request clarification from the customer / not fixable), criticality, and expected score impact. Plus a forecast: the score if all fixes are done.
7. Re-check loop: the supplier uploads corrected documents; only affected items are recalculated; version history is kept (for example, 72% to 94%).
8. An ordered "what still needs to be collected" checklist with completion marks, and a draft clarification request to the customer where the tender itself is contradictory.
9. Languages: Uzbek (Latin and Cyrillic), Russian, English. Scans from 150 dpi. Uzbek taxpayer ID (STIR).
10. Data: company-level isolation, configurable retention, bid documents not used for model training without consent.

Hard product rule for us: the tool never suggests changing facts. If the goods do not meet a requirement, the recommendation is to replace the item or ask for clarification.

Research questions, in priority order. Answer each one, or say "not found":
Q1. Supplier journey: how a company starts (tender link, upload, onboarding), what it is asked to provide, and where it is told what to collect.
Q2. Bid-versus-tender check: is the finished bid checked line by line against the tender requirements, or is only the tender analysed? How is each bid document matched to each requirement (by section, by page, by semantic search)? Which bid documents count as evidence?
Q3. Requirement extraction: how the tender becomes a compliance matrix; how references ("see Annex 3") are resolved; how scored criteria are shown.
Q4. Statuses and verdict: which statuses are used; how "claimed but not proven" is treated; how bid / no-bid and the readiness verdict are computed; whether a fixed rule or an LLM decides.
Q5. Disqualifiers: how disqualifying clauses are detected and presented; whether they are shown separately from scored items.
Q6. Citations: how page-level references and quotes are produced; whether the quote is checked against the source text.
Q7. Fix guidance: whether fix recommendations are given per problem; whether they carry an action type and score impact; whether they ever suggest altering facts.
Q8. Document checks: validity on the submission date, signatures and stamps, consistency of company details, translations, contradictions across bid documents.
Q9. Models and infrastructure: model names, OCR engine, local versus API, hosting, latency, cost per tender or per page.
Q10. Counterparty checks: what is checked (the bidder's own registrations, sanctions, debarment lists), which data sources, and whether this applies to the supplier's own company as well as to the customer.
Q11. Market fit: which tender markets they target (for example UK, EU, Gulf, other); whether Uzbek or Russian documents are supported; which procurement types.
Q12. Privacy and retention for uploaded bid documents: processors, subprocessors, retention period, use for model training.
Q13. Evaluation and reception: accuracy numbers, test sets, known failure cases, and public user reviews (cited, never invented).

Decisions this research must inform (answer each with evidence, or with the gap):
D1. Is the readiness verdict rule-based or LLM-based?
D2. Does the tool check the finished bid against each requirement, or only the tender? This is our main differentiator.
D3. Does it show "claimed, not proven" as a separate state, and how does it count in the score?
D4. Does it give fix recommendations with action types and score impact, or only a list of problems?
D5. Which OCR engine and which LLM(s) they use for scans and mixed-language bid documents.
D6. Do they store and reuse bidder documents, and what do they claim about it?

Ground truth rule:
Their servers and code are private. INFER the backend from public evidence and from observing the free tool's behaviour. Separate confirmed from guessed, and never present an inference as fact.

ORCHESTRATION: Opus 5.5 controls, Haiku 5.5 subagents execute.
You (main session, claude-opus-5-5) are the ORCHESTRATOR:
  1. Plan the investigation and split it into the workstreams below.
  2. Spawn one subagent per workstream with the Agent tool, model: "haiku" (claude-haiku-5-5). Run workstreams A to E in parallel. Workstream F waits for my approval (see below).
  3. Give each subagent a self-contained brief: scope, URLs, hard rules (copied in full), output format.
  4. Review all outputs. Re-open the source yourself for every high-impact claim (AI vendors, hosting, how citations are produced, how the verdict is computed) and downgrade anything you cannot verify.
  5. Send follow-ups (SendMessage) or spawn new Haiku subagents to fill gaps.
  6. Do ALL inference, cross-referencing, confidence scoring and final writing yourself.

Subagent output format (exactly):
  - finding: one factual sentence, no interpretation
  - source: URL / file / endpoint / header / screenshot
  - evidence: quote under 15 words, or paraphrase
  - date seen: publication or capture date, if known
  - notes: ambiguity; "not found" is also a result

Workstreams (one Haiku subagent each):
  A. Product and capability mapping (supplier view)
     All product, feature, pricing and use-case pages, especially those aimed at bidders and suppliers. List every claim: supported formats, pack and bid size limits, languages, compliance matrix fields, disqualifier detection, bid / no-bid logic, readiness or score claims, fix guidance, counterparty checks, integrations, export formats. Include any how-to-bid guides and what they say causes bids to fail.
  B. Frontend-visible backend surface (REA)
     Public pages with REA, including the supplier-facing pages: sign-up or sign-in entry points, tender upload pages, bid-document upload slots, results pages. Capture network traffic, API hosts, subdomains, third-party services, JS bundle config, public source maps, response headers.
  C. Legal and trust documents
     Privacy policy, DPA, subprocessor list, terms, security and trust pages, GDPR / UK GDPR, certifications. Extract named AI providers, cloud providers, regions, and the retention and training-use statements for uploaded bid documents.
  D. Public content and team signals
     Blog, bidder guides, case studies, press, founder interviews, podcasts, LinkedIn posts, job ads, public GitHub, and the Companies House record (public company information only, no personal data beyond what is needed to identify the business). Public user reviews on review sites: cite them, do not invent them.
  E. Infrastructure fingerprints
     DNS records and certificate transparency (crt.sh) for subdomains; public status page, if any.
  F. Hands-on test of the free checker (ONLY after my approval)
     Before running anything, STOP and ask me to confirm:
       - Tender document: only a publicly published tender pack (for example from a public government procurement portal). Never a confidential or personal document.
       - Bid document: NO real company's bid is uploaded. If a bid is needed to test the bid check, we create a synthetic sample bid with clearly fake company data, and I approve its content before upload.
       - Sign-up: if the checker requires an account, I create it myself. You do not.
       - Terms: confirm that the tool's terms allow this use.
     Once approved, run it and capture with REA:
       - Network flow: upload method, job IDs, polling versus streaming (SSE / WebSockets), response payload structure, and whether the bid upload is a separate step from the tender upload.
       - Processing time and how progress is shown.
       - Output: compliance matrix columns, citation format (page, clause, quote), statuses used, how disqualifiers, the readiness verdict and fix recommendations are presented, and whether a score change is shown after a re-upload (only if the tool allows it).
       - Errors or limits hit: file size, page count, scans.
       - Screenshots of every key screen.
     Test with one text PDF and, if allowed, one scanned PDF to see whether OCR exists. Keep it to a few normal test runs.

ANALYSIS (Opus 5.5 only):
  1. Inferred architecture, stage by stage. For each stage give: what it does, likely technology, evidence (workstream and source), confidence. Stages:
     - Ingestion and parsing (formats, OCR or not)
     - Requirement extraction and compliance matrix
     - Bid document intake and per-requirement matching
     - Citation generation (how page-level references and quotes are likely produced)
     - Disqualifying-clause detection
     - Bid document checks (validity, signatures, consistency, contradictions)
     - Readiness and bid / no-bid scoring
     - Fix recommendation generation
     - Re-check and version history
     - Counterparty checks (which data sources)
     - Models and vendors
     - Async job handling (from the observed network flow)
     - Hosting, data retention, privacy
     Deliver as a text diagram plus a table.
  2. Spec comparison table. For each of our supplier-side features, say how Lucius does it, with evidence, and what we would need to add:
     - Requirements checklist from the tender
     - Quote and page per item
     - Bid-versus-requirement check (line by line)
     - Five statuses, including "Needs human review"
     - OCR for scans and phone photos
     - Compliance percentage and readiness verdict
     - Fix recommendations per problem, with action types
     - Re-check loop and version history
     - Document validity and consistency checks
     - Uzbek and Russian language support
     - Multi-bidder comparison: mark as out of scope for the supplier side, unless Lucius shows it
  3. Decisions table. For each of D1 to D6, give the answer found, the evidence, and what would settle the open part.
  4. UX patterns worth copying. For each pattern: a screenshot reference, what it does, and why it works. Cover the supplier journey, the fix list ordering, progress display, readiness meter, deadline countdown, and the per-item source viewer, where these exist.

Confidence levels (every claim):
  - CONFIRMED: observed directly (including in our own test run)
  - CONFIRMED-BY-VENDOR: stated by Lucius, not independently verifiable
  - STRONG INFERENCE: multiple independent clues
  - SPECULATION: plausible, no direct evidence

Final report structure:
  - Product summary (supplier view)
  - Evidence report by workstream
  - Test run results (if approved)
  - Inferred architecture: diagram + table
  - Models and vendors: known versus suspected
  - Spec comparison table
  - Decisions (D1 to D6): answer, evidence, gap
  - UX patterns worth copying
  - Conflicts and how you resolved them
  - Unknowns and limits
  - Next steps to close the biggest gaps
  - Orchestration log: subagents run, scope of each, claims you re-verified

HARD RULES (copy into every subagent brief):
  - Passive and public only. No port scanning, directory brute-forcing, fuzzing, vulnerability probing, or calling APIs outside normal use of the site and the free tool.
  - Do not create accounts, bypass authentication or paywalls, or automate the free tool beyond a few normal test runs.
  - Upload only publicly published tender documents. Never upload a real company's bid, or any confidential or personal document. A synthetic bid with fake data is allowed only after I approve it.
  - Do not submit any bid, tender, demo request, contact form or price-quote request.
  - Do not scrape private or personal data.
  - Do not invent details. If evidence is thin, say "not found".
  - Quotes under 15 words; paraphrase otherwise.

After the report, ask me which pipeline stage, UX pattern, Q or D to go deeper on next.

Start now: write your plan in 5 to 8 lines, spawn the Haiku 5.5 subagents for workstreams A to E in parallel, and ask me for approval on workstream F.
