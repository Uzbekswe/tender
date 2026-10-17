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

## Share a link (quick, from your Mac)

Use this to let a colleague try the app in a browser, without installing anything.

1. Install the tunnel tool once: `brew install cloudflared`.
2. In `backend/.env`, set a password and turn on the public demo:

   ```
   ACCESS_PASSWORD=choose-a-long-password
   DEMO_PUBLIC=1
   ```

   Visitors must enter the password to create projects. The demo projects stay open without it.
3. Start the app and keep this terminal open: `cd backend && uv run uvicorn app.main:app --port 8010`. Restart it after you change `.env`.
4. In a second terminal run `cd backend && bash scripts/share.sh`. It prints a link that starts with `https://` and ends in `trycloudflare.com`.
5. Send that link. It works while your Mac, the app and the script are running. Press Ctrl+C to stop. The link changes each time you run the script.

If you do not set `ACCESS_PASSWORD`, the script warns you: anyone with the link can create projects and use your AI quota.

## Deploy (always-on link)

To keep the link working when your Mac is off, run the app on [Render](https://render.com). The repo has a `Dockerfile` and a `render.yaml` for this. Nothing is deployed yet.

1. Push the repo to GitHub. Render reads the blueprint from there.
2. In the Render dashboard, choose **New > Blueprint** and select the repo. Render reads `render.yaml`.
3. Enter two values when Render asks: `ANTHROPIC_API_KEY` (your Anthropic API key) and `ACCESS_PASSWORD` (a long password you choose). They stay in Render. Never put them in the repo.
4. Wait for the first build, then open the address Render shows. The health check is `/health`.

Costs and privacy:

- Render charges for the web service and the disk. The disk needs a paid plan. Check the current prices at render.com.
- Real checks use your Anthropic API credit. Demo projects replay recorded answers, so they cost nothing.
- Uploaded files are stored on Render's disk, at `/data`. A real tender or bid would sit on Render's servers. Use the sample documents, or check that this is acceptable for your data first.

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
