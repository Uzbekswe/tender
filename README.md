# Tender-Sverka

Tender-Sverka checks a bid against the requirements of a public tender. The first use is the **supplier self-check**: a company checks its own bid before it submits it. A **customer bid comparison** (for the buyer's committee) is planned, not built yet; see [docs/customer-side-decisions.md](docs/customer-side-decisions.md).

## What the tool does

1. You upload the tender and a bid (PDF, DOCX, XLSX, images, ZIP or RAR).
2. The app builds a checklist of the tender's requirements. Each requirement carries a quote from the tender page.
3. For each requirement it finds the bid evidence and gives a status: Meets, Partial, Not confirmed, Does not meet, or Needs human review.
4. It calculates the compliance percentage, a range, the tender's own points and a verdict.
5. The web UI shows the tender and the bid evidence side by side, and you can export XLSX and a clarification request.

Every status is backed by a quote that is found word for word on the cited page. Numbers, dates and scores are computed by code; the LLM reads and judges text but never sets a status or a score. When confidence is low, the item is marked "Needs human review".

## Install

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
cd backend
uv sync
```

## Run

```bash
cd backend
uv run uvicorn app.main:app --port 8010
```

Then open http://localhost:8010 (Russian by default; English is available).

## Tests

```bash
cd backend
uv run pytest
```

## How the AI layer works

- **Providers.** `claude_cli` is the default for the demo: it uses the Claude Code account that is logged in on this machine, with no API key. `anthropic_api` is for production and needs `ANTHROPIC_API_KEY`. `handoff` is for development only: it writes each request to a folder and reads the answer from another folder, without calling a model. `openrouter` is supported, but its model IDs are not set yet.
- **Model tiers.** Tasks use three tiers: Haiku (easy), Sonnet (standard) and Opus (hard). Vision on scanned pages and mandatory items with unclear evidence use the hard tier.
- **Escalation.** If an answer fails validation twice, the task is retried once on the next tier up.
- **Cache.** Every answer is cached by a hash of the provider, model, prompt version, messages, images and output schema, so a rerun with the same inputs makes no new calls.
- **Prompt versions.** Each task's prompt is a versioned file (`prompts/<task>/vN.md`); the newest version is used unless one is pinned in `config/models.toml`.

The settings are in [backend/config/models.toml](backend/config/models.toml).

## Architecture in short

The pipeline runs in stages: ingest and text-layer check, vision for pages that cannot be read as text, one card per page, extraction of requirements in windows with two passes, verification of quotes, the contradictions check, retrieval of candidate bid pages, matching, decision of statuses in code, scoring and recommendations. Each stage is resumable, and a run goes through the background runner.

Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The product spec: [docs/spec-en.md](docs/spec-en.md). Developer details: [backend/README.md](backend/README.md).

## Status

- 748 unit tests pass.
- First fully automatic runs on a confidential 220-page sample: about 24 minutes per run before card-stage parallelisation, 148 model calls (54 Haiku, 47 Sonnet, 47 Opus), 11–20% of items marked needs review, and 63–73% identical statuses between two fresh runs.
- Accuracy against a hand-checked gold set: not measured yet.

## Limitations

- The Uzbek interface is not built; the interface is in Russian and English.
- The customer side (bid comparison) is not built yet.
- Status stability between runs is still being improved.
- The target of 10 minutes per 100 pages is not confirmed yet.
- The sample documents are confidential and are not in the repo, so the sample runs cannot be reproduced from the repo alone.
