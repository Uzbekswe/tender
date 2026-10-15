# Technical specification (ТЗ): AI tool for comparing tender requirements and bids

Oct 8, 2026 · Product owner · Draft v0.1

## 1. Purpose and goals

The tool automatically checks a bidder's bid against the requirements of the procurement documentation and produces a compliance percentage, a list of non-compliances, and a plan to fix them. Working name: "Tender-Sverka (Tender Check)" (to be confirmed).

Problem. Tender documentation runs to tens or hundreds of pages: scans, tables, drawings, and appendices in different languages. A specialist manually writes down the requirements, assembles the bid package, and checks it line by line. This takes from several hours to several days, and a missed requirement leads to the bid being rejected.

Product goals:

- Extract all requirements for the goods (works, services) and for the bidder from the customer's documentation and turn them into a structured checklist.
- Extract the actual characteristics of the goods and the information about the bidder from the bidder's bid package, in the same structure.
- Match the two checklists and mark each requirement: Meets, Does not meet, Partial, or Not confirmed by a document.
- Calculate the overall compliance percentage, with separate scores for the goods and for the bidder.
- Produce an analytical conclusion and specific recommendations for fixing each non-compliance.

Target metrics (preliminary, subject to agreement):


| Metric | MVP target |
|---|---|
| Completeness of requirement extraction (recall) | 100% |
| Accuracy of compliance classification | 100% |
| Processing time for a package of up to 100 pages | ≤ 10 min |
| Reduction in manual checking time | 5× or more |

## 2. Users and scenarios

The tool works on both sides of procurement: a supplier checks its own bid before submitting it; a customer or evaluation committee checks the bids it has received.


| Role | Task | Result |
|---|---|---|
| Supplier's tender specialist | Check own bid before submission | List of fixes to avoid rejection |
| Supplier's manager | Decide whether to take part in the tender | Requirements checklist + company readiness assessment |
| Customer's specialist / evaluation committee member | Check received bids | Report on each bidder, comparison of bidders |
| Administrator | Configure reference data, weights, templates | Adaptation to the platform's or client's rules |

Main scenarios:

- Tender analysis. Upload of the customer's documentation only → requirements checklist + list of documents to prepare.
- Self-check of a bid. Customer's documentation + own bid package → compliance report, %, and recommendations.
- Re-check. Upload of corrected documents → recalculation of the affected items only, version history (72% → 94%).
- Checking several bids (customer). One set of documentation + N bid packages → summary table of bidders for each requirement.
- Clarifications and amendments. Upload of clarifications / amendments to the documentation → checklist is updated, changed items are flagged.

## 3. Input data and recognition

The tool accepts documents in any common format and converts them into a single text representation that preserves the structure and a reference to the source (file, page, area on the page).

### 3.1. Formats and upload methods


| Group | Formats |
|---|---|
| Text | PDF (text and scanned), DOC, DOCX, RTF, ODT, TXT |
| Tabular | XLS, XLSX, ODS, CSV |
| Images | JPG, PNG, TIFF (incl. multi-page), HEIC, BMP, WEBP — phone photos and scans |
| Drawings and diagrams | PDF/image drawings; DWG/DXF — phase 2 |
| Archives | ZIP, RAR, 7Z — extraction with folder structure preserved |
| Other | Manual text entry; link to a lot (importing documentation from the platform) |

- Upload in two separate packages: "Customer documentation" and "Bidder's bid"; drag-and-drop, multiple file selection.
- MVP limits: up to 100 files and 500 MB per package, up to 1000 pages.
- Automatic classification of files by type: spec, specification, draft contract, qualification requirements, evaluation criteria, certificate, license, certificate (official statement), power of attorney, manufacturer's catalogue, drawing, etc. The user can correct the type.

### 3.2. Recognition (OCR and layout analysis)

- Text recognition from scans and photos: rotated, skewed, with perspective distortion, low quality (from 150 dpi). Automatic alignment, noise removal, contrast adjustment.
- Any fonts: printed, monospaced, drawing fonts (GOST 2.304), handwritten notes and dates.
- Tables: recovery of structure, including merged cells and tables that continue onto the next page.
- Document attributes: presence of a stamp, signature, QR code, barcode; document number and date; validity period.
- Each recognized fragment is assigned a confidence score (0–100%). Low confidence → the fragment is flagged for manual review rather than silently accepted.

### 3.3. Drawings

- Extraction of title block data: name, designation, material, mass, scale.
- Dimensions and tolerances, notes, technical requirements written on the drawing, specification and leader lines.
- Understanding of the graphics (product type, composition of assemblies) via a multimodal model; exact dimensions are taken only from recognized labels, never measured from the image.

### 3.4. Languages

- Mandatory: Uzbek (Latin and Cyrillic), Russian, English.
- Extended: Chinese, Turkish, Kazakh, German, Korean, and other languages supported by the model (manufacturer catalogues and certificates).
- Mixed languages within one document and across packages: a requirement in Russian is matched against a product passport in English or Chinese.
- The user chooses the report language (RU / UZ / EN); original quotes are shown in the source language with a translation.

## 4. Extraction of customer requirements

Two checklists are built from the customer's documentation: "Requirements for the goods" and "Requirements for the bidder". Each requirement is atomic (one condition = one line) and has a reference to its location in the source document.

### 4.1. What is extracted

For the goods (works, services): name and classifier code, quantity and units, technical characteristics (with operators: at least / at most / range / equal to), standards (GOST, O'z DSt, ISO, IEC), country of origin and restrictions, year of manufacture / novelty, certificates and declarations, packaging and marking, warranty, delivery term and place, installation and training, documentation included in the set, equivalence ("or equivalent").

For the bidder: legal capacity and registration, absence from the register of unreliable suppliers, absence of tax arrears, not in bankruptcy or liquidation, licenses and permits, status as manufacturer or dealer (manufacturer's authorization letter), experience of similar supplies (number, amount, period), financial stability (turnover, balance sheet), specialists and qualifications, material and technical base, service center, bid security and performance security, bid form and formatting (signature, stamp, language, offer validity period), and other possible requirements.

### 4.2. Understanding context

- Distinguish mandatory requirements ("must", "mandatory", "not permitted") from desirable and scored ones (which award points but do not lead to rejection).
- Distinguish a requirement from reference text (preamble, quotations of law, platform instructions).
- Resolve references: "per Annex 3", "per form No. 2", "see cl. 4.2.1" — a requirement may be assembled from several places.
- Identify duplicates and contradictions within the customer's documentation (e.g., "warranty 24 months" in the spec and "12 months" in the contract) and move them to a separate block "Questions for the customer".
- Flag requirements that may restrict competition (a trademark named without "or equivalent", unique parameters of a single manufacturer).
- Normalize units of measurement (kW/W, inches/mm, °F/°C) and parameter synonyms (Russian "мощность" = Uzbek "quvvat" = English "power").

### 4.3. Structure of a checklist item


| Field | Example |
|---|---|
| ID | T-014 (goods) / U-007 (bidder) |
| Category | Technical characteristics / Qualification / Documents |
| Lot item | Item 3 "Laptop" (for multi-item lots) |
| Parameter | RAM |
| Condition | ≥ |
| Value + unit | 16 GB |
| Type | Mandatory / Scored / Desirable |
| Method of confirmation | Technical passport / certificate / certificate (official statement) / declaration |
| Consequence of non-compliance | Rejection / score reduction |
| Source | Spec.pdf, p. 7, cl. 2.3 + quote |
| AI confidence | 96% |

The user can edit, merge, delete, and add items manually; the checklist is locked ("approved") before comparison.

## 5. Analysis of the bidder's bid

The bid package goes through the same recognition pipeline, but what is extracted is not the requirements but the declared facts and the documents that confirm them.

- Facts about the goods: model, manufacturer, country, actual values of characteristics, quantity, price, terms, warranty — from the technical proposal, passports, catalogues, certificates, and drawings.
- Facts about the bidder: name, TIN (taxpayer ID, STIR), registration date, head, licenses (number, type of activity, validity), experience (contracts, acts, amounts), financial indicators, certificates (official statements).
- Document control: validity on the submission date, presence of a signature and stamp, matching of the bidder's details across all documents, presence of a translation where one is required.
- Internal contradictions in the bid: one value in the technical proposal and a different one in the product passport; different models in different documents.
- Source priority: official document from the manufacturer / certificate > bidder's technical proposal > marketing materials. A statement without a supporting document is marked "Declared, not confirmed".
- Result: a bid checklist in the same field structure as the customer's checklist (section 4.3), so the two can be compared line by line.

External source check (phase 2): checking the TIN against the company register, the register of unreliable suppliers, and licenses — in conjunction with the TenderZone counterparty check module.

## 6. Matching and marking

Each customer requirement is paired with an item from the bid, and the item receives one of five statuses, with a rationale and two source references.


| Status | Color | When assigned |
|---|---|---|
| Meets | Green | The value fulfills the condition and is confirmed by a document |
| Partial | Yellow | Part of the condition is met (2 of 3 certificates), or a document had expired at submission |
| Not confirmed | Orange | Stated in the technical proposal, but no supporting document |
| Does not meet | Red | The value violates the condition, or the item is missing from the bid |
| Needs human review | Grey | AI confidence is below the threshold (default 80%), or expert judgment is required (equivalence) |

Comparison rules:

- Numerical parameters — deterministic, after unit conversion (code, not a neural network): conditions ≥, ≤, range, equal to, tolerance ±.
- Text and semantic requirements ("anti-corrosion coating", "experience supplying similar goods") — LLM with mandatory rationale and quotes.
- Dates and validity periods — as of the bid submission deadline (taken from the documentation or entered manually).
- "Or equivalent" — comparison by functional parameters; the status cannot be higher than "Needs human review" if some parameters are not found.
- Multi-item lots — each item is compared separately; bid items are matched to lot items by name and number.
- Excess in the bid (the bidder offers more than required) is not treated as non-compliance, but is flagged if it contradicts other requirements (for example, a price above the starting price).

The user can manually change a status with a comment; the change is logged and taken into account when the percentage is recalculated.

## 7. Scoring: compliance percentage

The result is a weighted percentage across all requirements, but any unmet mandatory requirement raises a "Rejection risk" flag regardless of the percentage. Otherwise, a bid with 95% and one missing mandatory document would look reliable.

S = \frac{\sum_{i} w_i \cdot k_i}{\sum_{i} w_i} \times 100\%

where w is the weight of the requirement and k is the status coefficient.


| Status | k | Requirement type | Default w |
|---|---|---|---|
| Meets | 1.0 | Mandatory | 3 |
| Partial | 0.5 | Scored | 2 (or points from the evaluation criteria) |
| Not confirmed | 0.3 | Desirable | 1 |
| Does not meet | 0 |  |  |
| Needs human review | excluded from the calculation; a range "from–to" is shown |  |  |

What the user sees:

- The overall percentage, plus "Goods" and "Bidder" separately, and by category (technical characteristics, documents, qualification, commercial terms).
- Counters: X of Y mandatory requirements met; the number of red / yellow / grey items.
- Final verdict: "Ready to submit" (all mandatory items green), "Needs revision", or "Rejection risk".
- Forecast: "if all recommendations are implemented — will reach N%".

Weights and coefficients are configured by the administrator. If the documentation contains official evaluation criteria with points, scored requirements are weighted by those points.

## 8. Analytical conclusion and recommendations

The conclusion is a coherent text of one page, and each non-compliance gets a specific action rather than general advice.

Contents of the conclusion:

- One-sentence summary: verdict + percentage + number of critical issues.
- Critical non-compliances (leading to rejection) first.
- Non-compliances that reduce the evaluation score.
- Risks: documents with expiring validity, contradictions within the bid, ambiguities in the customer's documentation.
- Strengths of the bid (exceeding requirements, additional points).
- Action plan, sorted by impact on the result.

Structure of a recommendation:


| Field | Example |
|---|---|
| Requirement | U-007: experience of supplies ≥ 3 contracts over 2 years |
| What is wrong | 2 contracts attached, one without a completion act |
| Action | Add a third contract and completion acts for all three |
| Action type | Add a document / Correct data / Replace the goods / Request clarification from the customer / Not fixable |
| Criticality | High |
| Impact on result | +6% |
| Sources | Documentation, p. 12, cl. 3.1; Bid, "Experience.pdf", p. 2 |

Additionally:

- Draft request for clarification of the documentation — based on the contradictions and ambiguities from section 4.2.
- A "what still needs to be collected" checklist for the employee, with completion marks.
- The AI does not suggest distorting facts: if the goods do not meet a characteristic, the recommendation is to replace the model or request clarification, not to "fix the number".

## 9. Output reports and interface

The main screen is a comparison table "requirement ↔ bid" with color marking. Clicking an item opens both sources side by side, with the relevant fragment highlighted.

[Preliminary mockup · "Comparison" screen]

At the top: the summary and verdict. On the left: items with their statuses. On the right: quotes from both packages for the selected item, the AI's rationale, and actions. Figures on the mockup are examples.

Screens:

- Projects (lots) — list of checks: lot, customer, submission deadline, compliance %, status.
- Upload — two zones (customer / bidder), processing progress by stage, warnings about unreadable pages.
- Requirements checklist — editable table with filters (goods / bidder, type, category, lot item) and an "Approve" button.
- Comparison — table with statuses + document viewer panel on the right (split view), "only issues" filter.
- Summary — percentage, verdict, distribution by category, analytical conclusion, action plan.
- Bidder comparison (for the customer) — "requirement × bidder" matrix.
- Version history — percentage dynamics across iterations and what changed.

Export: PDF report (summary + table + recommendations), XLSX (checklists and comparison), DOCX (clarification request), JSON/API for integration.

Interface: web (responsive); interface languages RU / UZ (Latin) / EN; built-in project chat ("why is item U-007 red?") that answers only from the uploaded documents.

## 10. Non-functional requirements

Principle: every output of the system is verifiable. Without a page reference and a quote, no status is assigned.


| Area | Requirement |
|---|---|
| OCR accuracy | ≤ 2% character error rate (CER) on scans ≥ 200 dpi; ≤ 5% on phone photos |
| Accuracy of numbers | 100% match of numbers in requirements with the original at confidence ≥ 90%; below that — manual review |
| Protection against hallucinations | A requirement without a source quote does not enter the checklist; automatic check that the quote exists in the text |
| Speed | Package up to 100 pages — ≤ 10 min; up to 500 pages — ≤ 30 min; intermediate results shown as they become ready |
| Load | ≥ 50 concurrent checks, with horizontal scaling |
| Confidentiality | Data isolation between companies; TLS 1.2+ in transit and AES-256 at rest; documents not used for model training without consent |
| Retention | Retention period configurable (default 12 months); deletion on request |
| Personal data | Compliance with the personal data legislation of the Republic of Uzbekistan, incl. localization of data storage (to be clarified with a lawyer) |
| Access | Roles (administrator, specialist, viewer), audit log, sign-in by phone / platform SSO |
| Availability | 99.5% per month |
| Explainability | Each status has a text rationale and two source references |
| Reproducibility | Re-running on the same files gives the same result (model version and prompts are fixed) |

## 11. Architecture and AI stack (preliminary)

The architecture is hybrid: the LLM extracts and interprets, while numbers, dates, and the final percentage are calculated by code, so the result is reproducible and verifiable.

[Processing pipeline · 5 stages]


| Component | Implementation options |
|---|---|
| Recognition and layout | Multimodal LLM (Claude, GPT, Gemini) + specialized OCR for dense tables (Azure Document Intelligence, Google Document AI, ABBYY, or open-source PaddleOCR / Surya) |
| Extraction and matching | LLM with long context, structured output (JSON schema), mandatory quotes |
| Document search | Vector index (pgvector) for finding confirmations in large packages |
| Rules and scoring | Code (Python): unit normalization, comparison of numbers and dates, percentage calculation |
| Storage | PostgreSQL + S3-compatible file storage |
| Task queue | Asynchronous processing by page and by file (Celery / RabbitMQ or equivalent) |
| Integrations | REST API; import of lots from TenderZone; embedding in TenderGPT as an evaluation committee module |

The specific model is chosen based on test results on a set of 20–30 real tenders (see section 12), taking into account data storage requirements.

## 12. Development and acceptance

Acceptance is carried out on a reference (benchmark) dataset: 20–30 real tenders with bids, for which an expert has manually prepared the correct checklists and statuses. The set includes goods, works, and services; text PDFs, scans, photos, and drawings; three languages.

Stages:

- Research and prototype. Collection of the reference dataset; comparison of 2–3 models on OCR and extraction. Result: stack selection, checklist data schema.
- MVP. Upload, recognition, customer and bid checklists, comparison, percentage, conclusion, PDF/XLSX export. Languages RU / UZ / EN.
- Pilot. 5–10 supplier companies on real tenders; collection of errors and refinement.
- Expansion. Customer mode (N bids), DWG/DXF, checks against external registers, integration with TenderZone / TenderGPT, API.

MVP acceptance criteria:

- ☐ Completeness of extraction of mandatory requirements — 100% on the reference dataset
- ☐ Accuracy of compliance statuses — 100% (excluding "Needs human review"); share of "Needs human review" ≤ 15%
- ☐ 0 cases of "Meets" for mandatory requirements that were actually not met (the most costly error)
- ☐ 100% of items have a working link to the source page
- ☐ A package of up to 100 pages is processed in ≤ 10 min
- ☐ The expert rates the recommendations as applicable in ≥ 80% of cases

## 13. Open questions

- ☐ Where does the product live: a standalone service, the AI component of TenderZone (phase 3 "document set check"), or the evaluation committee module of TenderGPT?
- ☐ Who is the primary MVP client: the supplier (self-check) or the customer (checking N bids)?
- ☐ Which types of procurement to target first: public procurement in Uzbekistan (tender, competition, selection of best offers), corporate, international (World Bank, ADB)?
- ☐ Industries for the reference dataset, and who will label it (tender expert, timing)?
- ☐ Cloud or on-premise; is it acceptable to send documents to external AI APIs, given personal data localization requirements?
- ☐ Monetization model: subscription, payment per check, or payment per page?
- ☐ Is a disclaimer needed stating that the report is advisory and does not replace the decision of the evaluation committee?
- ☐ Product name.

## Glossary

| Russian | English |
|---|---|
| Тендер-Сверка | Tender-Sverka (Tender Check) |
| ТЗ | technical specification (spec) |
| закупка / закупочная документация | procurement / procurement documentation |
| тендер | tender |
| лот | lot |
| заказчик | customer |
| участник | bidder |
| поставщик | supplier |
| заявка (участника) | bid |
| пакет заявки | bid package |
| комиссия | evaluation committee |
| требование | requirement |
| чек-лист | checklist |
| соответствует / частично / не подтверждено / не соответствует / требует проверки человеком | Meets / Partial / Not confirmed / Does not meet / Needs human review |
| обязательное / оценочное / желательное | Mandatory / Scored / Desirable |
| обеспечение заявки / исполнения | bid security / performance security |
| реестр недобросовестных исполнителей | register of unreliable suppliers |
| ИНН | TIN (taxpayer ID) |
| техпредложение | technical proposal |
| справка | certificate (official statement) |
| доверенность | power of attorney |
| авторизационное письмо | manufacturer's authorization letter |
| разъяснение | clarification |
| эталонный набор | reference (benchmark) dataset |
| приёмка | acceptance |
| вторая очередь | phase 2 |
