# Tender-Sverka: 10-minute demo

This script shows the app with the sample tender (services, 66 pages, Uzbek Latin and Cyrillic, plus Russian). The bid documents are not shown on screen.

Times are rough. The verdict numbers are not written here. Read them from the screen, because they change after the checklist v2 re-check.

## 0. Prerequisites (before the demo, about 1 minute to check)

- `uv` is installed and `backend/` has been synced (`uv sync --python 3.13`). See [backend/README.md](../backend/README.md).
- The test project already exists. It is created by `scripts/run_testset.py`, with the name `testset: gov-tender`. It has:
  - a finished tender run,
  - an approved checklist (Approved v1 or later),
  - finished runs for Bid 1 and Bid 2.
- The bid runs are complete: no match units are pending. From `backend/`, run
  `uv run python scripts/run_testset.py --set ../test-data/gov-tender --stage bid --package all`.
  For each bid run it prints `match units: done N/N, pending 0`. This command re-runs the bid stages.
  Answers that are already stored are reused, but a missing answer can still call the model.
  A run that is still matching shows partial numbers, so the top menu and History skip it.
- No handoff requests are waiting. `run_testset.py` prints "handoff requests: 0 waiting" for each run.
- Do not change the submission deadline during the demo. It affects the validity checks.

If the checklist is still a draft, approve it on the Checklist screen before you run the bids.

## 1. Start the app (about 30 seconds)

```bash
cd backend
uv run uvicorn app.main:app --port 8010
```

Or start the `tender-sverka` entry in `.claude/launch.json` (port 8010).

Open http://localhost:8010.

**Why it matters:** one Python app, one command. No separate frontend build.

## 2. Projects screen (about 1 minute)

**Click:** the **Projects** entry in the top menu, then **Open** next to `testset: gov-tender`. The project name also opens it.

**Point out:**
- The columns: Name, Customer, Submission deadline, Latest verdict.
- The deadline banner. For the sample tender, it says the submission deadline is not set and document validity cannot be checked. The tender only gives a number of working days, so the app does not invent a date.

**Why it matters:** the app does not guess dates. Until a person enters the deadline, validity checks go to human review. A tool that guesses would give a false "Meets".

## 3. Project page (about 1 minute)

**Point out:**
- The two drop zones: **Customer documentation** and **Bid**.
- The document list: each file has a detected document type, which you can correct (**Save type**). Warnings appear here, for example pages that could not be read.
- **Run a check**: **Analyse tender** and **Check bid**. **Check bid** is locked until the checklist is approved.
- **Progress**: each run's stages and status. Once the checklist is approved, the tender run shows **Checklist approved (vN)** with a link to the checklist, instead of "waiting for approval".

**Why it matters:** the app reads every page, including scanned pages. A bad page is shown as a warning, never dropped in silence.

## 4. Checklist screen (about 3 minutes)

**Click:** **Open checklist** (on the project page, the tender run's **Checklist approved (vN)** link opens it too).

**Point out, in this order:**

1. **The table.** Each row has a code (T-### for the subject of procurement, U-### for the bidder), a type (Mandatory, Scored, Desirable), a condition and a confirmation method.
2. **The source column.** Each row has the tender quote and a ✓ with the tooltip "Quote verified on the page". Click the quote and show the page. Say: "The app checked this quote against the page text. If the quote was not on the page, the requirement would not appear here." (The rejected ones would be listed below in **Rejected by the verifier**. In the demo run that list is empty, so say that the verifier rejected nothing.)
3. **Evaluation scheme.** Five criteria with their maximum points and levels. The stated maximum is 15, but the criteria add up to 14. The app shows the mismatch warning. Say: "This is a real problem in the tender, and the app found it by adding up the numbers, not by asking the LLM."
4. **Questions for the customer.** Among them is the validity contradiction in the tender: one clause asks for N days, another for a shorter M days. Say: "The app puts the two quotes side by side and drafts the question. This is useful before the bid is sent." On the English page the question texts stay in Russian, because they are written from the tender text.
5. **Consolidation suggestions.** Rows with a suggestion show badges such as "Suggested: merge into ..." or "Suggested: exclude"; **Apply** on a row, or **Apply all suggestions**, applies them. Say: "The LLM suggests, a person decides. Suggestions never change the checklist on their own." On an approved version they are read-only.
   The approved version in the demo has no suggestions left, because they were applied earlier. So show it like this, and describe what is on screen:
   - Click **Only with suggestions**. The list is empty. Say: "Nothing is pending: the suggestions were already applied."
   - Click **New version**. Draft v(N+1) appears with the same rows. The filter is still empty, and **Apply all suggestions (0)** is disabled.
   - Click **Show excluded**. The rows that applied suggestions excluded or merged are listed and marked "excluded". Say: "Every applied suggestion is written to the audit log." The audit log has no page in the app, so do not open it.
   - Only if a draft with pending suggestions exists (a newer tender run that produced new ones), show the badges and **Apply** on one row.
6. **Approve.** If the checklist is still a draft, press **Approve checklist**. Say: "Approval freezes the version. Every bid check uses this exact version, so the bid is always checked against the same requirements." Do not approve the draft from step 5 during the demo: the bid checks were run against the approved version, and a new approval would change the version the numbers refer to. Each press of **New version** creates another draft, so press it once.

**Why it matters (what we do better than Lucius):**
- Verbatim quotes, verified by code, shown on the page.
- A scheme check that catches the 14 vs 15 mismatch before anyone relies on the score.
- Contradictions become questions for the customer, not hidden inconsistencies.
- A human approves the checklist before any bid is scored.

## 5. Comparison screen (about 3 minutes)

**Click:** **Comparison** in the top menu. It opens the latest finished bid run of the project. To open the other bid run, use its **Comparison** link on the project page. Start with the top bar.

**Top bar, point out:**
- The verdict badge (Ready to submit, Needs revision or Rejection risk), followed by "because of:" and the deciding requirement codes. The verdict is a fixed rule, and it always names the items that decided it.
- **Compliance**: the percentage, with the range in brackets. The range shows how much the result could move if the items under review were resolved.
- **Goods** and **Bidder** percentages.
- **Mandatory met: X of Y**. Say: "This counter sits next to the percentage. A high % cannot hide a missing mandatory item."
- **Tender points**: the estimate against the pass mark (11 points). If the tender had no scheme, it would say "not estimated". The scheme mismatch warning shows here too.
- **Statuses**: the filter chips with counts.
- **Check on the platform** (for example "Проверить на площадке (1 из 2 подтверждено)"): items that the e-procurement platform checks, not the bid files. Click **Confirmed on the platform** after checking on the platform, and **Undo** to take it back. The top bar counts them.

**Left list, point out:**
- Requirements with coloured status chips.
- **Only issues** filter. Click it, so the list shows only the items that need attention.

**Right panel (split view), click one "Does not meet" or "Needs review" item and point out:**
- **Tender requirement**: the condition, the confirmation method, the page image and text of the tender page.
- **Bid**: each evidence quote, with the file, the page, and a ✓ "verified" or "not verified". The matched text is highlighted in the page text.
- **Why this status**: reasons in plain words (for example "Claimed in the bid, but the confirming document is not attached").
- **Pages searched**: the number of bid pages checked. Say: "'Not found' always means 'not found in these pages'. It is not a bare 'absent'."
- **The model's rationale** and **What the confidence is made of**. The factors have names such as "Quote accuracy", "Page read quality", "Assessment method" and "Unverified quotes discarded". Say: "The confidence is calculated by code from the quote match, the page quality and the method. The model does not give its own confidence." The model's rationale text may be in Russian on the English page.
- **Recommendation**: problem, action, action type, criticality and the impact on the result.

**Override (do this last, it changes the saved result):**
- Scroll to **Override the status**. Choose a new status, write a comment (required), and press **Save status**.
- Point out the badge "Overridden by hand" and the line "Status changed by hand: old → new, by user, at time, comment".
- The top bar updates at once. Say: "Every manual change is logged with a comment. The score is recalculated from the stored facts."

**Why it matters:** the reviewer sees the tender's words, the bid's words, the reason and the next action on one screen. Nothing is paraphrased.

## 6. Summary screen (about 1.5 minutes)

**Click:** **Summary** in the top menu (the same bid run as Comparison).

**Point out, top to bottom:**
- The headline: verdict, compliance %, points against the pass mark, and the number of critical issues.
- **Critical non-compliances**: mandatory items that are not met or not confirmed. Say: "These lead to rejection, so they come first."
- **Check on the platform before submission**, when the run has such items: the same items as the Comparison card, with **Confirmed on the platform** and **Undo**.
- **Reduce the score**, **Risks** (needs a human check, contradictions in the bid, questions for the customer), **Strengths**.
- **Action plan by impact**: sorted by how much each fix changes the result.
- **Still to collect**: a checklist of documents. Tick items as you collect them. The ticks are kept in this browser only.
- **Downloads**: **Download XLSX** (the full checklist and comparison) and **Download the clarification request** (a text file with the customer questions and their quotes).

**Why it matters:** a one-page conclusion for the manager, with the work list for the team.

## 7. History screen (about 1 minute)

**Click:** **History** in the run's links (the page is titled **Check history**, or "История проверок" in Russian).

**Point out:**
- One row per check: Check, Date, Compliance, Verdict.
- **Changes since the previous check**, for example "U-004: Not confirmed → Meets".
- The trend line, with a small graph under it: the compliance of each completed check, oldest first.
- Only complete checks are listed. A run that is still matching is left out, so the trend has no 0% points from partial data.

**Say:** "After the supplier fixes a document, we start a new check. History shows exactly which items changed, and why."

**Why it matters:** the supplier sees progress item by item, not only a new percentage.

## 8. Wrap-up (about 30 seconds)

Summarise in one sentence: verified quotes, five statuses with reasons, a mandatory counter next to the percentage, document checks, and tender points against the pass mark.

## Known limitations

Say these out loud. Do not hide them.

- **The handoff provider is for development only.** Answers come from an agent in the coding session, not from a production model. It has no latency promise and no data-processing contract. Switching to OpenRouter needs the steps in [backend/README.md](../backend/README.md).
- **The sample tender has no calendar deadline.** Validity checks go to Needs human review until a person enters the submission deadline.
- **Consolidation suggestions need human approval.** They are read-only on an approved version. Create a new version to apply them.
- **Scanned pages are read by a vision model.** The reading may be less exact than the text layer. Such pages lower the confidence, and a requirement that depends on them goes to human review.
- **Export formats are limited.** The clarification request is a plain text file (.txt), not DOCX. There is no PDF export yet. XLSX is available.
- **Summary ticks are stored in the browser only.** They are not shared between users.
- **The audit log has no screen.** Applied suggestions and overrides are recorded there, but the app shows only their effect (excluded rows, the status change).
