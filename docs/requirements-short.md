# Tender-Sverka: short requirements summary

Short version of [spec-en.md](spec-en.md). Section numbers match the spec.

## 1. Goals and targets

1. Extract all requirements from the customer's documentation (goods, bidder) into a checklist.
2. Extract the actual facts from the bid package in the same structure.
3. Match both checklists and mark each item: Meets / Partial / Not confirmed / Does not meet / Needs human review.
4. Calculate the compliance % separately for goods and bidder.
5. Give a conclusion and a specific fix for each non-compliance.

MVP targets: recall 100%, classification accuracy 100%, package up to 100 pages in ≤ 10 min, manual checking time cut 5× or more.

## 2. Users and scenarios

1. Used by suppliers (self-check before submission) and customers / evaluation committee (checking received bids).
2. Scenarios: tender analysis, self-check, re-check after fixes, multi-bid check, handling clarifications and amendments.

## 3. Input and recognition

### 3.1 Formats and upload

1. Accept text, table, image, drawing and archive files (PDF, DOCX, XLSX, JPG, PNG, TIFF, ZIP, etc.). DWG/DXF in phase 2.
2. Two separate packages: "Customer documentation" and "Bidder's bid", with drag-and-drop.
3. MVP limits: 100 files, 500 MB and 1000 pages per package.
4. Auto-classify files by type (spec, contract, certificate, license, etc.). The user can correct the type.

### 3.2 OCR

1. Work with scans and photos from 150 dpi, including rotated or skewed ones.
2. Recognize all fonts, including drawing fonts and handwritten notes.
3. Rebuild tables, including merged cells and tables across pages.
4. Detect stamp, signature, QR code, barcode, document number, date and validity.
5. Give each fragment a confidence score. Low confidence → manual review, never silent acceptance.

### 3.3 Drawings

1. Read the title block: name, designation, material, mass, scale.
2. Read dimensions, tolerances, notes and technical requirements from labels only, never measured from the image.

### 3.4 Languages

1. Mandatory: Uzbek (Latin and Cyrillic), Russian, English.
2. Extended: Chinese, Turkish, Kazakh, German, Korean and others supported by the model.
3. Match across languages (e.g., a Russian requirement against an English passport).
4. Report language: RU / UZ / EN. Quotes stay in the original language with a translation.

## 4. Customer requirements

### 4.1 What is extracted

1. Goods: name and code, quantity, technical characteristics, standards (GOST, O'z DSt, ISO, IEC), origin, certificates, packaging, warranty, delivery, installation, training, "or equivalent".
2. Bidder: registration, absence from the unreliable-suppliers register, no tax arrears, not bankrupt, licenses, manufacturer/dealer status, experience, financial stability, staff, equipment, service center, bid and performance security, bid form.

### 4.2 Understanding the context

1. Distinguish mandatory requirements (must, mandatory, not permitted) from scored and desirable ones (points only, no rejection).
2. Separate real requirements from reference text (preamble, law quotes, platform instructions).
3. Follow references ("per Annex 3", "see cl. 4.2.1") and assemble the full requirement from all places.
4. Flag duplicates and contradictions (e.g., warranty 24 vs 12 months) into "Questions for the customer".
5. Flag requirements that restrict competition (trademark without "or equivalent").
6. Normalize units (kW/W, inches/mm, °F/°C) and synonyms (power = quvvat).

### 4.3 Structure of a checklist item

1. **ID:** T-014 for goods, U-007 for bidder.
2. **Category:** Technical characteristics / Qualification / Documents.
3. **Lot item:** e.g., Item 3 "Laptop" (multi-item lots only).
4. **Parameter:** e.g., RAM, power, warranty period.
5. **Condition:** ≥, ≤, range, equal to.
6. **Value + unit:** e.g., 16 GB.
7. **Type:** Mandatory / Scored / Desirable.
8. **Confirmation method:** technical passport, certificate, certificate (official statement), declaration.
9. **Consequence:** Rejection / score reduction.
10. **Source:** file, page, clause + quote.
11. **AI confidence:** e.g., 96%.
12. User can edit, merge, delete and add items. The checklist must be approved before comparison.

## 5. Bid analysis

1. Extract declared facts about goods (model, manufacturer, values, price, terms) and bidder (name, TIN, licenses, experience, finances).
2. Check documents: validity on submission date, signature and stamp, matching details, translation if required.
3. Find internal contradictions (different values or models in different documents).
4. Source priority: manufacturer's official document > technical proposal > marketing. Claims without proof are "Declared, not confirmed".
5. Output uses the same field structure as section 4.3.
6. Phase 2: check TIN against company and unreliable-supplier registers.

## 6. Matching and status

| Status | Color | When |
|---|---|---|
| Meets | Green | Condition met and confirmed by a document |
| Partial | Yellow | Part met, or document expired at submission |
| Not confirmed | Orange | Claimed in proposal, no proof |
| Does not meet | Red | Condition violated, or item missing |
| Needs human review | Grey | AI confidence below 80%, or expert judgment needed |

1. Numbers: deterministic code after unit conversion.
2. Text and meaning: LLM, with mandatory rationale and quotes.
3. Dates: checked against the submission deadline.
4. "Or equivalent": status max "Needs human review" if some parameters are missing.
5. Multi-item lots: compare each item separately.
6. Excess over requirements is not a non-compliance, unless it conflicts with other requirements.
7. Users can change a status manually; the change is logged and recalculates the %.

## 7. Scoring

1. Score = weighted sum of status coefficients ÷ total weight × 100%.
2. Coefficients: Meets 1.0, Partial 0.5, Not confirmed 0.3, Does not meet 0, Needs human review excluded (shown as a range).
3. Default weights: Mandatory 3, Scored 2, Desirable 1 (scored items can use the official evaluation points).
4. Any missed mandatory requirement raises "Rejection risk", regardless of the %.
5. Show: overall %, Goods and Bidder separately, category breakdown, counts of mandatory met and red / yellow / grey items.
6. Verdict: Ready to submit / Needs revision / Rejection risk. Also show a forecast if all fixes are done.
7. Administrator sets weights and coefficients.

## 8. Conclusion and recommendations

1. One-page conclusion: verdict, %, number of critical issues.
2. Order: critical non-compliances, score-reducing issues, risks, strengths, action plan by impact.
3. Each recommendation has: requirement, what is wrong, action, action type (add document / correct data / replace goods / request clarification / not fixable), criticality, impact on %, sources.
4. Also: draft clarification request and a "still to collect" checklist.
5. Never suggest altering facts. If the goods don't meet a spec, recommend replacing the model or asking for clarification.

## 9. Outputs and interface

1. Main screen: "requirement ↔ bid" table, with both sources side by side on click.
2. Screens: Projects, Upload, Checklist (editable, approve button), Comparison (split view, "only issues" filter), Summary, Bidder comparison, Version history.
3. Exports: PDF report, XLSX (checklists, comparison), DOCX (clarification request), JSON/API.
4. Web, responsive, UI in RU / UZ / EN. Project chat answers only from uploaded documents.

## 10. Non-functional

1. Every status needs a page reference and a quote. No quote → no status.
2. OCR: ≤ 2% character errors on scans ≥ 200 dpi; ≤ 5% on phone photos.
3. Numbers: 100% match with source at confidence ≥ 90%, otherwise manual review.
4. Quotes must exist in the source text (automatic check).
5. Speed: 100 pages ≤ 10 min; 500 pages ≤ 30 min.
6. Load: 50+ concurrent checks, horizontal scaling.
7. Security: data isolation, TLS 1.2+, AES-256 at rest, no model training on documents without consent.
8. Retention: default 12 months, deletable on request.
9. Personal data: comply with Uzbekistan law, localize storage (legal review needed).
10. Access: roles, audit log, phone / SSO sign-in.
11. Availability 99.5% per month.
12. Same input → same output (model and prompt versions fixed).

## 11. Architecture

1. Hybrid: LLM extracts and interprets; code calculates numbers, dates and the final %.
2. Recognition: multimodal LLM + specialized OCR for tables.
3. Storage: PostgreSQL + S3-compatible; vector index for search.
4. Queue: asynchronous, per page and per file.
5. Integrations: REST API, TenderZone lot import, TenderGPT module.
6. Model choice based on a test on 20–30 real tenders.

## 12. Development and acceptance

1. Acceptance on a reference (benchmark) dataset of 20–30 real tenders with expert-made checklists and statuses.
2. Stages: research and prototype → MVP → pilot (5–10 suppliers) → expansion.
3. MVP acceptance:
   - Mandatory requirement extraction: 100% on the dataset.
   - Status accuracy: 100% (excluding Needs human review); Needs human review ≤ 15%.
   - Zero "Meets" for mandatory requirements that are not met.
   - 100% of items link to the source page.
   - 100-page package in ≤ 10 min.
   - Expert rates recommendations as applicable in ≥ 80% of cases.

## 13. Open questions

1. Where the product lives: standalone, TenderZone component, or TenderGPT module.
2. Primary MVP client: supplier or customer.
3. Procurement types to target first.
4. Who labels the reference dataset, and when.
5. Cloud or on-premise; allowed to use external AI APIs?
6. Monetization model.
7. Need a disclaimer that the report is advisory?
8. Final product name.
