# Tender-Sverka backend

Tender-Sverka checks a supplier's bid against the requirements of a public tender.

## What it does

1. You upload the tender and a bid (PDF, DOCX, XLSX, images, ZIP or RAR).
2. The app builds a checklist of the tender's requirements. Each one has a quote from the tender page.
3. For each requirement it finds the bid evidence and gives a status: Meets, Partial, Not confirmed, Does not meet, or Needs human review.
4. It calculates the compliance %, a range, the tender's own points, and a verdict (Ready to submit, Needs revision, or Rejection risk).
5. The web UI shows the result side by side (tender on the left, bid evidence on the right). You can export XLSX and a clarification request.

## Trust rules

- **Every status has a verified quote.** The quote must be found word for word on the cited page. If it is not, it is dropped.
- **Numbers, dates and scores are computed by code.** The LLM reads and judges text. It never sets a status, a score or a date comparison.
- **Low confidence means "Needs human review".** The app does not guess.

Design details: [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

## Setup

Run these from the `backend/` folder.

```bash
cd backend
uv sync --python 3.13
bash scripts/set_openrouter_key.sh
```

The last command asks you to paste your OpenRouter key. The key is saved to `backend/.env`. The script does not show it on screen.

`.env` must contain one line:

```
OPENROUTER_API_KEY=...
```

Never commit `.env`. You can also copy `.env.example` to `.env` and fill it in by hand.

The default LLM mode (`handoff`, see below) does not call OpenRouter. You only need the key after you switch to `openrouter`.

Runtime data (the SQLite database, uploaded files, page images, LLM answers) is stored in `../data/`. That folder is git-ignored. The test documents in `../test-data/` are also git-ignored, and they contain personal data.

## Run

```bash
cd backend
uv run uvicorn app.main:app --port 8010
```

Open http://localhost:8010 in your browser. The JSON API is under `/api/v1` (for example `/api/v1/projects`). Interactive API docs are at http://localhost:8010/docs.

Tests:

```bash
cd backend
uv run pytest
```

## LLM providers

The LLM settings are in `config/models.toml`.

- `provider` selects the backend: `handoff` (development) or `openrouter`.
- `[tasks]` sets the model for each task, for example `match = "..."`. The tasks are `read_page`, `page_cards`, `extract_requirements`, `extract_scheme`, `confirm_contradictions`, `consolidate`, `match` and `word_recommendations`.
- `[limits]` sets parallel calls, requests per minute, retries and timeout.
- `[prompt_versions]` pins a prompt version. If it is empty, the newest `vN.md` in `prompts/<task>/` is used.

### The handoff provider (development only)

`handoff` is a stand-in for a real API. It does not call any model.

1. A stage needs an answer. The app writes the request to `data/llm_handoff/outbox/<id>/request.json`. Page images are saved as PNG files next to it. The stage does not fail. That unit stays pending.
2. Someone (in practice a Claude agent in the coding session) writes the answer to `data/llm_handoff/inbox/<id>.json`. The file holds the raw answer, in the same form a model would return it.
3. Run the stage again. The next run reads the inbox file, validates it against the schema, and caches it, the same way it caches an API answer.

`scripts/run_testset.py` prints how many requests are still waiting. The handoff is not for production. It has no latency promise and no data-processing contract.

### Switching to OpenRouter

1. Save the key with `bash scripts/set_openrouter_key.sh`.
2. In `config/models.toml`, set `provider = "openrouter"`.
3. Replace each task's model with a real OpenRouter model ID (the form is `vendor/model-name`). The current values are Claude names, which the handoff uses. They are not OpenRouter IDs. To list vision models, run `uv run python scripts/bakeoff.py --list`.
4. In your OpenRouter privacy settings, turn on the option that excludes providers which train on your prompts. Only use models that pass this setting. The bids contain personal data.
5. Set `requests_per_minute` (default 120) and `parallel_calls` (default 4, or `LLM_CONCURRENCY` in .env) to your account's real limits.

Free tier limits, as far as we know (re-check them, they change): about 20 requests per minute and about 50 per day. With at least $10 of credit, about 1,000 per day. One full test-set run needs about 100 calls, so on the free tier one run can take more than a day.

### Caching makes reruns free

Every answer is cached. The cache key is a SHA-256 hash of the provider, the model, the prompt version, the exact messages, the page image hashes, the temperature and the output schema. A rerun with the same files, models and prompts costs nothing and gives the same result. A new prompt version (a new `vN.md` file) or a new model causes new calls.

## Pipeline stages

Stages run in order. A run skips work that is already done, so a stopped run can be resumed.

| Stage | Run | What it does | Done by |
|---|---|---|---|
| `ingest` | reading | Unpacks archives, stores originals, hashes files, renders page images | code |
| `textlayer` | reading | Scores the PDF text of each page. Garbled text scores low. Detects the language of each page | code |
| `vision` | reading | Reads pages without usable text as images: full text, rotation, stamp, signature and QR flags | LLM (vision) |
| `cards` | reading | One short record per page: document type, title, issuer, dates, summary | LLM (text) |
| `extract` | tender | Pulls requirements and the evaluation scheme from the tender, in page windows, two passes | LLM |
| `verify` | tender | Drops every requirement whose quote is not on the cited page | code |
| `contradictions` | tender | Finds conflicting values and sums that do not match (for example 14 points vs a stated 15). The LLM confirms real conflicts | code + LLM |
| `consolidate` | tender | Suggests merges, exclusions and type changes. A person applies them. Advisory only | LLM, checked by code |
| `identify` | bid | Takes the bidder's name and tax ID (STIR) from the page cards | code |
| `retrieve` | bid | Picks candidate bid pages for each requirement, by document type and keyword search | code |
| `match` | bid | Asks the LLM for evidence and a judgement (never a status), in groups of requirements | LLM |
| `assess` | bid | Verifies every evidence quote, runs the checks (numbers, dates, identity, document type), sets the status | code |
| `score` | bid | Computes compliance %, range, tender points, verdict and recommendations | code |

## Test-set workflow

The test set is in `../test-data/gov-tender/` (tender, Bid 1, Bid 2). Run these from `backend/`.

```bash
# 1. Reading stages only, no LLM and no key needed
uv run python scripts/run_testset.py --stage read --no-llm

# 2. Reading stages with the LLM (vision and page cards)
uv run python scripts/run_testset.py --stage read

# 3. Tender run: extract, verify, contradictions, consolidate
uv run python scripts/run_testset.py --stage tender

# 4. Bid runs against the approved checklist
uv run python scripts/run_testset.py --stage bid --package all

# Test runs only: approve the latest draft checklist first, then run the bids
uv run python scripts/run_testset.py --stage bid --package all --auto-approve
```

`--auto-approve` is for test runs only. It approves the draft checklist if the checklist is complete and the tender run has no pending units. Do not use it on a real tender. Without it, approve the checklist on the Checklist screen before you run the bids.

If the handoff provider is active, `--stage tender` and `--stage bid` may stop with pending requests. Write the answers to `data/llm_handoff/inbox/`, then run the same command again.

## Folder map

- `app/main.py`: the FastAPI app. Starts the task runner and mounts the routes.
- `app/api/`: the JSON API under `/api/v1` (projects, checklist, results).
- `app/web/`: the HTML pages (`routes.py`, `templates/`, `static/`).
- `app/runner.py`: the in-process task runner and the stage registry.
- `app/models.py`, `schemas.py`, `db.py`, `settings.py`, `storage.py`: tables, API models, database, settings, file layout.
- `engine/`: the pipeline. No FastAPI imports. Main files: `pipeline.py` (reading), `tender_stages.py`, `bid_stages.py`, `score_stages.py`, `verify.py`, `status.py`, `scoring.py`, and `rules/`.
- `llm/`: the LLM client, the cache, and `providers/` (`handoff.py`, `openrouter.py`).
- `prompts/<task>/vN.md`: one folder per LLM task. A new prompt version is a new file. Do not edit old ones.
- `config/`: `models.toml` (models, limits) and `scoring.toml` (weights, review threshold).
- `scripts/`: `run_testset.py` (test-set runs), `bakeoff.py` (vision model test; sends page images to the provider, needs its confirmation flag), `set_openrouter_key.sh`.
- `tests/unit/`: unit tests. `tests/fixtures/`: small test inputs. `tests/gold/`: expected results (empty for now).
- `i18n/`: interface strings in Russian (`ru.json`) and English (`en.json`).
