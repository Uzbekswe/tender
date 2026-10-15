Use REA (Reverse Engineer Anything: https://github.com/morluto/rea) plus passive public-source research to investigate the BACKEND / AI side of Doubletapp Tender AI, with focus on the CUSTOMER-SIDE workflow: the buyer or evaluation committee that receives, checks and ranks bids.

Targets:
- Product site: https://tender.doubletapp.ai
- Engineering write-up: Doubletapp's article on Habr about building Tender AI. Find it on Doubletapp's Habr company blog; also check follow-up articles and the comments.

Why this research exists:
I am building a customer-side tender evaluation tool (MVP). Before we design our own pipeline, I need to understand how a product like Doubletapp Tender AI works for the buyer: how a tender becomes a checklist, how many bidders are compared, how missing or unclear evidence is handled, how the result is made auditable for a committee, and what it costs to run. The output decides what we build. It is not a code-copying exercise.

Claims about Doubletapp's product in this prompt (for example "can run on local models" or "quotes every verdict") come from marketing summaries and are my assumptions. Treat them as hypotheses to confirm or refute, not as facts.

Our target product, customer side (for comparison only):
1. Input: the customer's tender package (PDF, DOCX, XLS/XLSX, scans and phone photos, drawings, ZIP/RAR/7Z) plus N bidder packages.
2. Build two checklists from the customer documents: requirements for the goods and requirements for the bidder. Each item is atomic, with an ID (T-### goods, U-### bidder), a type (Mandatory / Scored / Desirable), a source page with a quote, and an AI confidence score. A human approves the checklist before comparison.
3. For each bidder and each item, assign one of five statuses: Meets / Partial / Not confirmed / Does not meet / Needs human review. Numbers are compared in code after unit normalization; text requirements are judged by an LLM. Every status needs a page reference and a quote. No quote, no status.
4. Per bidder: weighted compliance percentage (goods and bidder separately), a "rejection risk" flag if any mandatory item fails, and a verdict.
5. Multi-bidder comparison matrix (requirement x bidder) and ranking.
6. Clarification handling: draft clarification requests to bidders for missing or unclear documents, and to the customer for contradictions inside the tender documents.
7. Analytical summary for the evaluation committee; export to PDF, XLSX, DOCX; JSON/API.
8. Languages: Uzbek (Latin and Cyrillic), Russian, English. Scans from 150 dpi. Uzbek taxpayer ID (STIR).
9. Data: tenant isolation, configurable retention, no model training on customer documents without consent, personal-data localization in Uzbekistan.

Research questions, in priority order. Answer each one, or say "not found":
Q1. Pipeline: which stages, in what order, and where a human reviews or approves (checklist approval, status override, final decision).
Q2. Ingestion and OCR: which parsers and OCR engines; how archives, scans, merged-cell tables and multi-page TIFF are handled; how extraction quality is measured.
Q3. Requirement extraction: how a tender is split into atomic requirements; how references ("see Annex 3") are resolved; how duplicates and contradictions are found; how scored criteria and evaluation weights are read from the documents.
Q4. Bidder matching: how each status is decided, in particular "insufficient data" versus "does not meet"; how "or equivalent" is handled; whether numbers are compared in code or by an LLM.
Q5. Evidence: how quotes and page locations are linked to each verdict; whether the quote is checked against the source text.
Q6. Ranking and decision support: how scores are computed; how bidders are ranked; how sign / shortlist / reject is recommended; whether the committee keeps the final decision; how ties and disqualifications are handled.
Q7. Clarifications and audit: whether bidder or customer clarification requests are generated; committee protocols; audit log; version history; re-evaluation when a bidder uploads corrected documents.
Q8. Models and infrastructure: model names, sizes, local versus API; hosting; hardware; latency; cost per tender or per page.
Q9. Evaluation: accuracy numbers, test sets, failure cases, lessons learned, and what they say does not work yet.
Q10. Buyer context: which buyers (government, state companies, corporate, procurement platforms); pricing model; cloud versus on-premise; integrations with procurement platforms; supported procurement methods.
Q11. Legal and trust: data-processing statements, 152-FZ or local data residency, security certifications, and any claims about compliance with public-procurement rules.

Decisions this research must inform (answer each with evidence, or with the gap):
D1. Do they auto-recommend sign / shortlist / reject, or only assist the committee?
D2. Do they use an LLM for numeric comparison, or code?
D3. How many verdict statuses do they use, and how is "insufficient data" defined?
D4. Which OCR engine and which LLM(s) they use for scans and Uzbek text.
D5. Whether a human-in-the-loop checklist approval step exists, and how it looks.
D6. Whether they hold the data locally or use external AI APIs, and what they claim about it.

Ground truth rule:
Their servers and code are private. INFER the backend from public evidence. Separate confirmed facts from guesses, and never present an inference as fact. Claims Doubletapp makes in its own article count as CONFIRMED-BY-VENDOR (stated by them, not independently verified).

ORCHESTRATION: Opus 5.5 controls, Haiku 5.5 subagents execute.
You (main session, claude-opus-5-5) are the ORCHESTRATOR:
  1. Plan the investigation and split it into the workstreams below.
  2. Spawn one subagent per workstream with the Agent tool, model: "haiku" (claude-haiku-5-5). Run independent workstreams in parallel.
  3. Give each subagent a self-contained brief: scope, URLs, hard rules (copied in full), output format.
  4. Review all outputs. Re-open the source yourself for every high-impact claim (model names, model sizes, local versus cloud, OCR engine, accuracy numbers, buyer names) and downgrade anything you cannot verify.
  5. Send follow-ups (SendMessage) or spawn new Haiku subagents to fill gaps.
  6. Do ALL inference, cross-referencing, confidence scoring and final writing yourself.

Subagent output format (exactly):
  - finding: one factual sentence, no interpretation
  - source: URL / section heading / file / endpoint / header
  - evidence: quote under 15 words (original language + English translation), or paraphrase
  - date seen: publication or capture date, if known
  - notes: ambiguity; "not found" is also a result

Workstreams (one Haiku subagent each):
  A. Habr article deep read (primary source)
     Extract everything about: pipeline stages; document parsing and OCR; archive handling; chunking; requirement extraction; how the checklist is built and approved; bidder-versus-requirement matching; the meaning of each status and of "insufficient data"; citation and quote linking; scoring, ranking and recommendation logic; prompts or prompting strategy; which models were tried and which won (names, sizes, local versus API); hardware; latency; cost; accuracy and evaluation method; failure cases and lessons learned. Also read the comments for Q&A with the authors.
  B. Other Doubletapp content
     Other Habr posts; Doubletapp blog and site; case studies, especially with buyers (government, state companies, corporate); conference talks (YouTube, VC.ru, Telegram channels); press and interviews that describe how committees or procurement departments use Tender AI; any mention of pilots, customers or results.
  C. Product site and customer-facing UI (REA)
     Load tender.doubletapp.ai public pages with REA. Describe the flows a buyer sees: upload, checklist view and editing, approval step, bidder comparison matrix, ranking view, clarification features, export formats, roles, and any evaluation-committee or protocol screens. Capture network traffic, API hosts, subdomains, third-party services, JS bundle config, public source maps, response headers. List every claimed capability, supported file types, deployment options (cloud / on-premise), and pricing if shown.
  D. Legal, trust, procurement context and deployment
     Privacy policy, terms, data-processing statements, 152-FZ or other data-residency statements, on-premise or local-model claims, hosting providers, data retention, tenant isolation, audit logging statements. Also public-procurement rules that shape the product: evaluation methods (lowest price versus quality-cost), appeal rules, required documents, and any statement from Doubletapp about compliance with them. Use official government procurement pages for the rules; use Doubletapp's own pages for their claims.
  E. Team, hiring and infrastructure fingerprints
     Doubletapp job posts on hh.ru, Habr Career and their careers page, especially for ML, OCR, document-AI and backend roles (stack hints). Public GitHub repositories. DNS records and certificate transparency (crt.sh) for related subdomains. Conference or hackathon presence.

ANALYSIS (Opus 5.5 only):
  1. Inferred architecture, stage by stage. For each stage give: what it does, likely technology, evidence (workstream and source), confidence. Stages:
     - Ingestion (PDF, DOCX, Excel, scans, archives)
     - OCR and layout / table parsing
     - Requirement extraction and checklist building
     - Human checklist approval
     - Bidder document matching per requirement
     - Verdict assignment (number of statuses) and how "insufficient data" is decided
     - Citation linking (quote and source location)
     - Multi-bidder comparison and ranking
     - Recommendation (sign / shortlist / reject) and final human decision
     - Clarification request generation
     - Committee report, audit trail and versioning
     - Models: which ones, local versus API, and why
     - Evaluation and quality control
     - Hosting, privacy and deployment
     Deliver as a text diagram plus a table.
  2. Spec comparison table. For each of our customer-side features, say how Doubletapp does it, with evidence, and what we would need to add:
     - Checklist from customer documents with quote and page per item
     - Human approval of the checklist
     - Bidder check per item
     - Five statuses, including "Needs human review"
     - OCR for scans and phone photos; Uzbek text
     - Compliance percentage, separate goods and bidder scores, rejection-risk flag
     - Clarification requests to bidders and to the customer
     - Multi-bidder matrix and ranking
     - Committee report and export formats
     - Audit trail and reproducibility
     - Tenant data isolation and retention
  3. Decisions table. For each of D1 to D6, give the answer found, the evidence, and what would settle the open part.
  4. Lessons for our build: documented mistakes, model choices and trade-offs, each tied to a source.

Confidence levels (every claim):
  - CONFIRMED: observed directly in a primary source
  - CONFIRMED-BY-VENDOR: stated by Doubletapp, not independently verifiable
  - STRONG INFERENCE: multiple independent clues
  - SPECULATION: plausible, no direct evidence

Final report structure:
  - Product summary (customer-side view)
  - Evidence report by workstream
  - Inferred architecture: diagram + table
  - Models and vendors: known versus suspected
  - Spec comparison table
  - Decisions (D1 to D6): answer, evidence, gap
  - Lessons for our build
  - Conflicts and how you resolved them
  - Unknowns and limits
  - Next steps to close the biggest gaps
  - Orchestration log: subagents run, scope of each, claims you re-verified

HARD RULES (copy into every subagent brief):
  - Passive and public only. No port scanning, directory brute-forcing, fuzzing, vulnerability probing, or calling APIs beyond what public pages load themselves.
  - Do not log in, create accounts, bypass authentication, or submit forms. This includes demo requests, trial sign-ups, contact forms and price-quote requests, even with fake data.
  - Do not scrape private or personal data. Do not collect names or contact details of people beyond what is published as an official company or author attribution.
  - Do not invent details. If evidence is thin, say "not found".
  - Quotes under 15 words; paraphrase otherwise. Do not reproduce the Habr article wholesale.

After the report, ask me which customer-side stage (Q1 to Q11 or D1 to D6) to go deeper on next.

Start now: write your plan in 5 to 8 lines, then spawn the five Haiku 5.5 subagents in parallel.
