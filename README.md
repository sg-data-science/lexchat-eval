# lex-eval

Evaluations for [LexChat](https://github.com/delphium226/lexchat). Runs questions through
available LLMs (Ollama and OpenRouter), stores responses in DuckDB, scores them with both coded
and AI-as-judge metrics, and visualises results in a Streamlit dashboard.

## Prerequisites

Python 3.11+. Install dependencies:

```bash
pip install -e ".[dev]"
```

Create `lex_eval/.env` (see `lex_eval/.env.example`):

```bash
# LexChat API Configuration
LEXCHAT_API=http://host.docker.internal:80

# Authentication
USERNAME=admin
PASSWORD=admin

# OpenRouter judge (judge LLM for AI-as-judge metrics)
OPENROUTER_API_KEY=yourkeyhere
# Any OpenRouter model, openai/gpt-4o is the default, o4-mini for more thorough evals
OPENROUTER_JUDGE_MODEL=openai/gpt-4o

```

## Step 1 Check the active LLM

```bash
python -m lex_eval.utils.get_llm
```

Queries the LexChat API and prints the single active model (the one configured in the Admin Portal). There is always exactly one active model per provider.

## Step 2 Gather responses

```bash
# All questions (model is set in LexChat's admin portal):
python lex_eval/gather_responses.py

# Specific question:
python lex_eval/gather_responses.py --question-id 1

# Multiple specific questions:
python lex_eval/gather_responses.py --question-id 1 2 4

# A different questions file (default: lex_eval/data/questions.json):
python lex_eval/gather_responses.py --questions lex_eval/data/questions_new.json --question-id 7 8

# Overwrite existing results (start fresh):
python lex_eval/gather_responses.py --overwrite

# Debug: dump every raw SSE event for inspection:
python lex_eval/gather_responses.py --question-id 1 --debug-events
# → writes lex_eval/data/debug_events.jsonl

# Debug: write a per-question annotated audit log:
python lex_eval/gather_responses.py --question-id 1 --verbose-capture
# → writes lex_eval/data/verbose_logs/Q1_YYYYMMDD_HHMMSS.log

# Both flags can be combined:
python lex_eval/gather_responses.py --question-id 1 --debug-events --verbose-capture
```

### Diagnosing capture issues

Two flags are available when a response looks wrong: empty fields, missing tools, zero retrieval context, etc.

| Flag | Output | Use when… |
|---|---|---|
| `--debug-events` | `data/debug_events.jsonl`, one JSON line per raw SSE event, appended | You suspect the **server sent unexpected data**: field renamed, event type missing or added, payload structure changed |
| `--verbose-capture` | `data/verbose_logs/Q{id}_{YYYYMMDD}_{HHMMSS}.log`, one file per question | You got a **wrong capture result**: `research_output` empty, `tool_sequence` incomplete, zero `retrieval_context` items |

`--debug-events` shows what arrived **over the wire** before `audit_capture` processes it. `--verbose-capture` shows what `audit_capture` **decided to do** with each event, stack state before/after, action taken, and a final state summary.

Because `--debug-events` appends all questions into a single file, it is cleanest when combined with `--question-id`. `--verbose-capture` always writes one file per question so it is safe to use across all questions concurrently.

To diff two runs of the same question:

```bash
diff \
  lex_eval/data/verbose_logs/Q5_20260625_091600.log \
  lex_eval/data/verbose_logs/Q5_20260625_091740.log
```

Responses are stored in `lex_eval/data/responses.db` (DuckDB).
Each question is attempted up to 3 times; only complete responses (non-empty `actual_output`) are written to the database.

**The model used for responses is always set in LexChat's Admin Portal.** The eval does not select or override the model. `gather_responses.py` reads the active model from the LexChat API and records it in `responses.db`. To evaluate a different model, change it in the Admin Portal first, then re-run.

We need to gather at least two responses per question per llm to evaluate response consistency. So starting from the beginning this is the recommended process.

```bash
# 1. gather the first set of responses
python lex_eval/gather_responses.py

# 2. run the db script to get a report of complete responses
# per question and per llm
python -m lex_eval.utils.db

# gather another set of responses (appends by default)
python lex_eval/gather_responses.py
```

## Step 3 Run evaluations

```bash
# All metrics:
python lex_eval/run_evals.py

# Specific metric(s), each writes to its own eval_<metric> table:
python lex_eval/run_evals.py --metrics tool_usage
python lex_eval/run_evals.py --metrics response_groundedness    # needs OPENROUTER_API_KEY
python lex_eval/run_evals.py --metrics claim_support            # needs OPENROUTER_API_KEY
python lex_eval/run_evals.py --metrics consistency
python lex_eval/run_evals.py --metrics mandatory_structure citation_passthrough citation_grounding citation_domain genuine_gap
python lex_eval/run_evals.py --metrics citation_agreement
python lex_eval/run_evals.py --metrics reference_answer_agreement    # needs OPENROUTER_API_KEY

# Force re-run (clear and replace existing results for the selected metric(s)):
python lex_eval/run_evals.py --metrics response_groundedness --overwrite

# Re-run without clearing, so new rows accumulate alongside old ones
# (troubleshooting/testing a metric's determinism):
python lex_eval/run_evals.py --metrics claim_support --append

# Single LLM only:
python lex_eval/run_evals.py --llm "model-name"

# Verbose output:
python lex_eval/run_evals.py -v
```

Each metric writes to its own `eval_<metric>` table in `lex_eval/data/responses.db`.
By default, only scores with compatible code, judge configuration, and reference
versions are skipped. Normal rescoring preserves historical rows. Use `--dry-run`
to preview pending work without writes or judge calls, especially before the first
judge sweep over legacy results whose scoring version is unknown. `--append`
repeats compatible measurements too; `--overwrite` explicitly clears selected
results and cannot be combined with an experiment filter.

### Evaluation requirements

| Metric | Speed | Requires |
|---|---|---|
| `tool_usage` | Fast | Nothing extra |
| `mandatory_structure`, `citation_passthrough`, `citation_grounding`, `citation_read`, `citation_domain`, `genuine_gap`, `step_completion` | Fast | Nothing extra |
| `citation_agreement` | Fast | Authored reference answers |
| `consistency` | Fast | ≥2 responses per question/LLM/chat mode |
| `response_groundedness`, `claim_support` | Medium (1 LLM call/test) | `OPENROUTER_API_KEY` |
| `reference_answer_agreement` | Medium (2 LLM calls/test) | `OPENROUTER_API_KEY` + authored reference answers |
| `report_integration` | Slow (1 LLM call per plan step, each sending the whole final answer) | `OPENROUTER_API_KEY` |

## Step 4 Streamlit dashboard

```bash
streamlit run lex_eval/reports/streamlit_report.py
```

The dashboard reads `lex_eval/data/responses.db` without modifying it. Filter by
model, chat mode, research mode, and experiment, then inspect questions and their
failure stages. The question table leads with what needs attention, failed checks,
measurement gaps and execution warnings, and clicking a row opens that question.
Stored contradiction failures stay failures, mixed repeats remain mixed, and
clarification, errors, halts, and report repair are shown separately. A check with
no verdict says whether it does not apply, could not be measured, is not
comparable, or was never scored against that response. Every panel names a
response the same way, repeat answers can be read side by side with their own
check results, and the review evidence exports as JSON and as a readable
Markdown report carrying each score's provenance. Reviewer notes typed into the
export panel are kept for the browser session against that question.

Use `--label` and `--experiment-id` when gathering to identify repeat sweeps.
Scoring runs have their own identity, so rejudging an answer cannot count as an
extra response. The dashboard opens on an overview with one row per experiment,
showing each check's passes, run outcomes, cost and time side by side. The comparison view matches questions and compatible scoring
versions across two recorded experiments that have been scored. Experiment info
lists every experiment, scored or not, with the questions it gathered, which
checks have a result for each of them, and the `run_evals.py` command for the
checks still missing one. Existing results remain labelled as legacy, with
unknown experiment conditions.

See [Experiments and reviewing results](lex_eval/docs/experiments.md) for commands,
deployment metadata, scoring previews, and the review-evidence export.

## Step 5 Build the deployed dashboard

To update the dashboard hosted on streamlit.io, run:

```bash
python -m lex_eval.export_deploy --push
```

That rebuilds the data, rebuilds the deploy repo from this one, and force-pushes
it. streamlit.io redeploys from the new commit. Nothing else is needed.

The dashboard is served from a separate public repository,
[lexchat-eval-streamlit](https://github.com/tomwilsonsco/lexchat-eval-streamlit),
so that opening the app from streamlit.io does not expose this one. That repo is
a build output: nothing in it is edited by hand. Its checkout lives at
`lexchat-eval-streamlit/` inside this repo and is gitignored, which is the
default `--target`. Clone it there once:

```bash
git clone git@github.com:tomwilsonsco/lexchat-eval-streamlit.git lexchat-eval-streamlit
```

A sibling directory will not do, since only the repo directory itself is on
persistent storage. Pass `--target` if you keep the checkout somewhere else.

### What the one command does

1. **Builds the data**, as Parquet, one file per table, into
   `lex_eval/data/deploy/`. `retrieval_context` is trimmed to 2,000 characters
   per item, and the retrieved text that nothing displays is dropped from
   `audit_json` and from each scoring run's reference snapshot. Parquet rather
   than DuckDB because DuckDB stores large text uncompressed, which made the
   copy too large to commit. `reports/data.py` reads either, so the dashboard is
   the same either way.
2. **Builds the tree**: every module the dashboard imports, the Parquet, and the
   reference answers reduced to the fields anything reads, plus a generated
   `streamlit_app.py` and `requirements.txt`. The target is emptied first, so a
   file dropped from this repo does not linger in the deployed one.
3. **Proves it runs**, by importing the copied tree in a subprocess that cannot
   see this repo. A module the copy is missing fails the export rather than the
   deployed app.
4. **Pushes**, if `--push` was given. The branch is replaced with a single
   parentless commit, since Parquet cannot be delta-compressed and ordinary
   commits would grow that repo by a full copy every time.

### Flags

| Flag | Effect |
| --- | --- |
| `--push` | Do step 4. Without it the tree is written and left for inspection, and you re-run with `--push` once it looks right. |
| `--skip-data` | Skip step 1 and reuse the existing `lex_eval/data/deploy/*.parquet`. Use it on that second run. |
| `--target DIR` | Deploy repo checkout to rebuild. Defaults to `lexchat-eval-streamlit/`. |
| `--branch NAME` | Branch to replace. Defaults to `main`. |

`python -m lex_eval.utils.db --deploy-db [directory]` builds only the Parquet.
Step 1 above already calls it, so it is not a prerequisite of `export_deploy`;
run it on its own only when you want the Parquet to look at.

Neither `responses.db` nor `lex_eval/data/deploy/` is committed here.

## Reference ("gold") answers

A set of expected answers for the questions in `questions.json`, for tests to compare LexChat's
responses against. Each answer is researched against the live LEX API using the same legislation
tools LexChat's Worker agent uses, so it rests on exactly the material LexChat would have retrieved,
and is then written up from that retrieved text by the author recorded on the answer. For the
current set that author is an AI model (Claude Opus 5) rather than a person, which is exactly why a
lawyer's sign-off, not the drafting, is what makes an answer ground truth.

This does **not** need a running LexChat instance. It talks to the LEX API directly, so it works
when Steps 1-2 cannot run.

> Generated answers are **unverified drafts** until a lawyer returns a decision on the Markdown
> file. Evaluation uses drafts and signed-off answers alike, and labels a draft's scores
> `[DRAFT REFERENCE - unverified]`: agreement with a draft is agreement with its author, not legal
> correctness. Only a lawyer's approval makes it more than that.

### Building them

```bash
python -m lex_eval.reference.build --author "Your Name"
```

Run it repeatedly. It looks for questions with no answer in the manifest yet and advances each one
a stage, printing what it needs from you next:

| Stage | What the script does | What you do next |
| --- | --- | --- |
| **SCAFFOLD** | Creates `.authored/q{id}/` with template files | Fill in `searches.json` |
| **RETRIEVE** | Runs your searches, writes `retrieved.md` | Read it, then write `plan.json`, `answer.md` and `statements.json` |
| **BUILT** | Writes `q{id}.md` and updates the manifest | Send it for lawyer review |

`statements.json` holds the key statements a correct answer has to make, most important first,
between one and five of them. Five is a cap, not a quota: a narrow question may only turn on two
points, and padding the list lowers every score without telling answers apart.
They are what `Reference Answer Agreement` scores a response against, and they are written once and
stored with the answer so that the judge labels a fixed list instead of choosing the points again on
every run. Each one should be a single self-contained sentence about what the law says, since the
judge sees the statements and the response under test but never the reference answer itself.

Useful flags:

```bash
python -m lex_eval.reference.build --question-id 7    # one question only
python -m lex_eval.reference.build --refetch          # re-run searches after editing searches.json, and rebuild
python -m lex_eval.reference.build --overwrite        # rebuild a question that already has an answer
python -m lex_eval.reference.build --questions ...    # a different question file (see below)
python -m lex_eval.reference.build --render-only      # offline: re-read what you wrote, regenerate q{id}.md
```

### A different question set

`--questions` defaults to `lex_eval/data/questions.json`. Point it at another file to build answers
for that one instead:

```bash
python -m lex_eval.reference.build --questions lex_eval/data/questions_new.json --author "Your Name"
```

All question files build into the same answers directory, so their ids have to be unique across
files: an answer is matched to a response by question id.

### The files you write

In `lex_eval/data/reference_answers/.authored/q{id}/`. The build script scaffolds all of them; you
fill in the first four, and `review.json` holds what the lawyer decides later.

**`searches.json`**: the LEX tool calls to make. Follow the Worker's phases: `search_legislation`
to find the Acts, then `search_legislation_sections` to pull the provisions from each one
(`get_legislation_text` is available as a fallback for a whole Act).

```json
[
  {"tool": "search_legislation",
   "args": {"query": "Data Protection Act 2018", "year_from": 2018, "year_to": 2018}},
  {"tool": "search_legislation_sections",
   "args": {"legislation_id": "ukpga/2018/12", "query": "meaning of controller, definitions"}}
]
```

You will usually run the build twice here: once with the Phase 1 searches to find the
`legislation_id`s, then again after adding the Phase 2 section searches (`--refetch`). Once the
answer itself is written, `--refetch` replays the searches and rebuilds the answer against them in
the same run, so editing `searches.json` is one command and not two.

**`plan.json`**: how the question breaks down. Recorded so the reasoning behind the answer is
reviewable, not just the conclusion.

```json
{
  "scope_note": "What this answer covers and what it deliberately excludes.",
  "steps": [{"title": "Short imperative title", "detail": "What exactly to find, in domain terms."}]
}
```

**`answer.md`**: the answer itself, written from `retrieved.md`. Ground every statement in the
retrieved text and cite it, as a legislation.gov.uk link rather than a name in prose, or nothing can
check it. Use the four headings the Worker system prompt mandates: **Summary Answer (BLUF)**,
**Detailed Analysis**, **Jurisdiction & Status**, **References**. Write them as `###`, and anything
below them as `####`: the answer is shown inside a section of the generated review document, so a
heading above H3 would sit outside its own section in a reader's contents.

**`statements.json`**: the one to five points a correct answer must make, most important first, as
described above.

**`review.json`**: the lawyer's decision. Written by whoever transcribes the review, not by you when
authoring:

```json
{
  "verified": true,
  "verified_by": "Name",
  "verified_at": "2026-09-02",
  "verdict": "Approve",
  "citations_reviewed": true,
  "required_citations": ["https://www.legislation.gov.uk/asp/2009/12/section/35A"],
  "corrections": "",
  "notes": "",
  "signed_reference_sha256": null
}
```

Leave `signed_reference_sha256` as `null` when recording a fresh approval: `--render-only` stamps it
with the record's current version, and any later change to the answer, statements, required citations
or retrieved material makes the sign-off stale until they confirm the new version. Returned Word
review documents go in `lex_eval/data/reference_answers/reviews/` (gitignored). How to transcribe one
into `review.json` is set out in
[`lex_eval/docs/reference-answers.md`](lex_eval/docs/reference-answers.md#transcribing-a-returned-review).

### Output

```text
lex_eval/data/reference_answers/
├── q1.md                      # generated for review: answer, key statements, citations, decision, research appendix
├── reference_answers.json     # machine-readable manifest, all questions, the only thing metrics read
└── .authored/q1/              # what you write: searches.json, plan.json, answer.md, statements.json,
                               # review.json, plus the generated retrieved.md
```

The manifest is generated from `.authored/`, and `q{id}.md` is generated from the manifest. Each
field has one owner, so nothing is edited in two places:

| Material | Owned by | Copied into |
| --- | --- | --- |
| The answer | `.authored/q{id}/answer.md` | `final_answer`, `research_output`, section 2 of `q{id}.md` |
| The key statements | `.authored/q{id}/statements.json` | `statements`, section 3 of `q{id}.md` |
| The research plan and searches | `.authored/q{id}/plan.json`, `searches.json` | manifest audit fields, the Markdown appendix |
| The retrieval evidence | the manifest, captured when the searches ran | the Markdown appendix |
| The lawyer's decision | `.authored/q{id}/review.json` | `review`, section 5 of `q{id}.md` |

Read the manifest from a test with:

```python
from lex_eval.reference import load_reference_answers

answers = load_reference_answers()                     # signed off and drafts, the default
answers = load_reference_answers(verified_only=True)   # signed-off answers only
```

A lawyer's decision goes in `.authored/q{id}/review.json`, and a completed review survives a
rebuild. An answer counts as signed off only while the sign-off is complete (a reviewer, a date, and
a decision on which citations are mandatory) and still matches the record it was given against; if
the answer, the statements, the required citations or the retrieved material change afterwards, the
record reads `Stale` and drops back to being a draft until the reviewer confirms the new version.

Do not edit `q{id}.md` or `reference_answers.json` by hand. Markdown edits are ignored; manifest
edits affect scoring immediately but are overwritten by the next render. When a lawyer sends changes
back, edit `.authored/q{id}/answer.md`, `statements.json` or `review.json` and run:

```bash
python -m lex_eval.reference.build --render-only
```

That re-reads what you wrote into the manifest and regenerates every Markdown file offline, leaving
the retrieval audit showing the material the answer was actually written from.

### The retrieval audit

Each answer records two lists of sources, and the distinction matters when checking citations:

- **`sources_retrieved`**: provisions whose text was actually pulled. Citing one is grounded.
- **`sources_discovered`**: Acts and SIs that appeared in a search result but were never read. They
  exist and were found, but the answer never saw their text, and should say so.

Both appear in the Markdown so a reviewer can check every citation against them.

## Database utilities

```bash
# Show completeness report (responses per question/LLM pair):
python -m lex_eval.utils.db

# List every response (id, llm_name, timestamp):
python -m lex_eval.utils.db --list

# Remove incomplete / error rows:
python -m lex_eval.utils.db --clean

# Preview what --clean would remove without deleting:
python -m lex_eval.utils.db --dry-run

# Delete a single response by id, and its rows in every eval_<metric> table:
python -m lex_eval.utils.db --delete-response <ID>
```

## Repository structure

```text
lex_eval/
├── data/
│   ├── questions.json       # evaluation questions
│   ├── deploy/              # Parquet build output for the deploy repo (not committed)
│   ├── reference_answers/   # gold answers: q{id}.md + reference_answers.json
│   └── verbose_logs/        # per-question capture audit logs (gitignored)
├── docs/                    # permanent docs; docs/findings/ is local, dated notes
├── metrics/                 # custom metric classes
├── reference/               # reference ("gold") answers
│   ├── build.py             # the build script
│   ├── lex_client.py        # the LEX and Find Case Law tools, as LexChat calls them
│   └── store.py             # manifest + Markdown for review
├── reports/
│   ├── review_export.py     # review pack as a readable Markdown report
│   └── streamlit_report.py  # Streamlit dashboard
├── tests/                   # pytest evaluation suites
├── utils/                   # shared utilities (db, client, capture, judge)
├── gather_responses.py      # Step 2 entry point
├── open_db_ui.py            # opens responses.db in browser UI
└── run_evals.py             # Step 3 entry point
```

## Documentation

`lex_eval/docs/` holds the permanent documentation, the docs that answer "how does
this work" and are kept up to date with the code:

| Doc | What it covers |
| --- | --- |
| [Metrics](lex_eval/docs/metrics.md) | What each metric catches, how it scores, and why it is designed that way. |
| [Reference answers](lex_eval/docs/reference-answers.md) | The reference ("gold") answer system: what is authored, what is generated. |
| [Experiments](lex_eval/docs/experiments.md) | Running a gather as an experiment, scoring it, reading and comparing results. |
| [Question set provenance](lex_eval/docs/question-set-provenance.md) | The fields recording where each question came from. |
| [Decisions](lex_eval/docs/decisions/) | Decision records for choices made or still open. |

`lex_eval/docs/findings/` is the other shelf, for docs that answer "what did we find
on date X": a bug investigation, a review status, a report over one data export.
Each opens with the date it describes and is frozen rather than maintained, so
they are kept locally and are not committed. Nothing in the table above should
depend on a findings file for a fact.

## A note on LLM judge models
The judge LLM is accessed via OpenRouter, which provides access to hundreds of models from many providers. The default model is `openai/gpt-4o`, which offers a good balance of thoroughness and cost. More expensive or capable models may produce more critical judgments, leading to lower scores for reference agreement, response groundedness, and research groundedness.

For example, `openai/o4-mini` is a thinking model available through OpenRouter and will produce lower scores than `openai/gpt-4o-mini`. However, o4-mini does a better job picking up on subtleties that smaller models may ignore. You can change `OPENROUTER_JUDGE_MODEL` in your `.env` file to any model available on OpenRouter (e.g. `google/gemini-2.5-flash`, `openai/o4-mini`, `anthropic/claude-sonnet-4-6`).

Research showed that `google/gemini-2.5-flash-lite` was too weak for judge tasks, so it has not been used. Larger models like `google/gemini-2.5-pro` can be more thorough but may cost significantly more, as they run more analysis per evaluation.

## Current evaluations

| Metric | Description |
|--------|-------------|
| Tool Usage | Did the run delegate research, find the legislation and read it? The definitions and made-under tools count for both, since each returns the words the answer relies on. Legislation checks retain their mode-specific order rules. Case-law-only runs require delegation and either a case-law search or direct judgment lookup; mixed-mode scores cover legislation tools only. |
| Research Output Structure | Does the worker agent return the findings to the manager with the requested headers. Not measured in conversational mode, where the worker is told not to use those headers. |
| Reference Links | Are all reference links found by the researcher included in the final answer given to the user. |
| Citation Grounding | Does every Act cited in the researcher's report correspond to legislation the run's own tool calls actually retrieved, rather than one invented by the model. |
| Citation Read | Did the researcher actually read every Act it cites? An Act whose text was pulled, or that the definitions or made-under tool listed, counts as read; one that only appeared as a title in a search results list does not. Catches a report making claims about a real, correctly linked source it never opened. |
| Citation Domain | Does every citation link in the researcher's report point to a domain the Worker is permitted to cite. That is legislation.gov.uk for legislation only, caselaw.nationalarchives.gov.uk for case law only, and both for the hybrid mode, matching what each Worker prompt asks for. |
| Genuine Gap | When retrieval found no usable legislation text, does the researcher's report say so plainly instead of answering with unsupported confidence. |
| Step Completion | Deep research only. Did every step of the approved research plan carry its own retrieved legal text into its own report, rather than a step that retrieved text and then reported nothing (for example, hitting a tool-call budget limit mid-step). |
| Report Integration | Deep research only, AI as a judge metric: For every step that reported a real, cited finding of its own, does the final answer reflect it, rather than dropping it when the Manager condenses several step reports into one response. |
| Consistency (Cosine) | Compare the answers provided when the same question is asked multiple times using TF cosine similarity. Any legislation section cited in one answer but not the other is listed for information, but does not decide pass or fail: an agent searching a live corpus twice will touch different secondary provisions each run. |
| Citation Agreement | Of the legislation provisions the reference answer expects, how many does the response cite too. No AI judge, it compares lists of legislation.gov.uk links. Once a lawyer has signed a reference off, the expected list is the citations they marked required and the threshold is 0.5; for a draft it is every link in the reference answer and the threshold is 0.3. For each Act the reference answer relied on that the response does not cite, the reason says whether any search turned it up, so the reader can tell a search that missed the law from an answer that had the law and left it out. |
| Reference Answer Agreement | AI as a judge metric: How many of the question's key statements the response also makes, between one and five of them. The statements are written once alongside the reference answer and stored with it, so the judge labels a fixed list rather than picking the points afresh on every run. A second judge call looks for contradictions and nothing else, which is what catches a long answer that makes a point correctly in one section and then undoes it in another. A statement the response contradicts fails the metric outright, since a confidently wrong statement of law is worse than a missing one. |
| Plan Coverage | Deep research only, AI as a judge metric: Does the approved research plan set out to cover the question's key statements, before any research happens. Reuses the same fixed statement list as Reference Answer Agreement instead of a separately authored golden plan. |
| Claim Support | AI as a judge metric: What share of the report's verifiable legal claims are backed by text the researcher actually read? Claims whose truth depends on the absence of a provision are reported separately because absence generally cannot be established from retrieved excerpts or summaries. |
| Response Groundedness | Is the final answer to the user grounded in the research worker's summary. A near-unmodified copy is accepted automatically with no AI judge involved. Anything reworded enough to matter goes to the judge, which either passes it or fails it: it fails on any unsupported claim or meaningful misrepresentation, and passes only trivial wording differences. There is no partial credit, so the average for this metric is a pass rate. |
