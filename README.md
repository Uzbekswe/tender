# Tender-Sverka

Tender-Sverka checks bids against the requirements of a public tender. It has two sides, chosen on the start page:

- **Supplier: check my bid.** A company checks its own bid before it submits it.
- **Customer: compare bidders.** The buyer's evaluation committee checks several bids against one approved checklist and sees them side by side.

The interface is in Russian, Uzbek and English, with a light and a dark theme.

## What the tool does

1. You upload the customer's tender documents and a bid (PDF, DOCX, XLSX, images, ZIP or RAR). Files that look like they are in the wrong box are flagged before anything is sent to the AI.
2. The app builds a checklist of the tender's requirements. Each requirement carries a quote from the tender page. It suggests merging duplicates and removing non-requirements (for example the tender's own pass mark); you review and approve the checklist.
3. For each requirement it finds the evidence in the bid and gives a status: Meets, Partial, Not confirmed, Does not meet, or Needs human review.
4. It calculates the compliance percentage with a range, the tender's own points against the pass mark, and a verdict.
5. You see the tender and the bid evidence side by side, a one-page summary with an action for each problem, and exports: XLSX, a PDF report and a clarification request (Word).

Every status is backed by a quote that is found word for word on the cited page. Numbers, dates, document validity, proof versus claim, and scores are decided by code; the AI reads and judges text but never sets a status or a score on its own. When the evidence is unclear, the item is marked "Needs human review". The results are advisory: a person makes the decision.

## Install

You need:

- macOS or Linux, [git](https://git-scm.com/), and [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 for you).
- An AI provider. The default is **Claude Code**, logged in with your own Claude account (Pro or Max): install it with `npm install -g @anthropic-ai/claude-code`, run `claude` once and sign in with `/login`. No API key is needed. For an API key instead, see "AI layer" below.

```bash
git clone https://github.com/Uzbekswe/tender.git
cd tender/backend
uv sync
```

## Run

```bash
cd tender/backend
uv run uvicorn app.main:app --port 8010
```

Open http://localhost:8010. Keep the terminal open while you use the app; Ctrl+C stops it. Your projects and uploaded files are stored in the `data/` folder next to `backend/` and never leave your machine except for the AI calls.

## Use

**Supplier side**

1. Choose "Supplier: check my bid" and create a project. Set the submission deadline (used to check that documents are still valid).
2. Upload the customer's tender documents in the left box and press **Analyse tender** (about 3–5 minutes for 60–100 pages).
3. Open the checklist, look through it and press **Apply suggestions and approve**.
4. Upload the bid in the right box and press **Check bid** (about 4–5 minutes for 100 pages).
5. Open **Summary** for the verdict and what to fix, or **Comparison** for each requirement with the tender and bid quotes side by side. Downloads: XLSX, PDF (browser "Save as PDF"), clarification request (Word).

**Customer side**

1. Choose "Customer: compare bidders", create a project, upload the tender, analyse it and approve the checklist as above.
2. Add each bidder (name, optional tax ID) and upload its files.
3. Press **Check all bidders**, then open **Comparison**: a preliminary ranking and a requirement-by-bidder matrix with filters. Click any cell for the evidence. Export the matrix to XLSX.

## Tests

```bash
cd backend
uv run pytest
```

## AI layer

- **Providers.** `claude_cli` (default) uses the Claude Code account logged in on the machine. `anthropic_api` uses an API key: put `ANTHROPIC_API_KEY=...` and `LLM_PROVIDER=anthropic_api` in `backend/.env`. `handoff` is for development without a model.
- **Model tiers.** Easy tasks use Haiku, standard tasks Sonnet, hard ones Opus: reading scanned pages, and mandatory items that are still unclear after the standard model. Every escalation is logged with its reason.
- **Retries and cache.** Failed or empty answers are retried. Every answer is cached by provider, model, prompt version and inputs, so a re-check reuses earlier answers.
- **Prompts.** Each task's prompt is a versioned file in `backend/prompts/<task>/vN.md`.

## Architecture in short

The pipeline runs in stages in a background runner: ingest and text-layer check, reading scanned pages with vision, one card per page (document type, dates, organisations), requirement extraction in windows with two passes, quote verification, the contradictions check and consolidation suggestions, retrieval of candidate bid pages per requirement, matching, statuses decided in code, scoring and recommendations. Each stage is resumable. Developer details: [backend/README.md](backend/README.md).

## Status

- About 1,600 automated tests.
- On a confidential 220-page sample: 2.5–3 minutes per 100 pages; 2–7% of items marked "Needs human review".
- Status stability between two fresh runs of the same bid is still being improved (12–20% of statuses differed before the latest fixes).
- Accuracy against an expert-checked reference set has not been measured yet.

## Limitations

- A prototype: no user accounts or roles; it runs locally for one user.
- Engineering drawings (DWG/DXF) and registry checks (tax ID, unreliable suppliers) are not built.
- The sample documents are confidential and are not in the repo.
