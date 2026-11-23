# Running the evaluation on AILA's EC2 instance

*Decision record, 23 November 2026. Status: decisions D1 to D6 agreed 23 November 2026.
No code has been written. Checked against AILA (LexChat) `main` at v2026.11.9 plus its unreleased
changes, and against this repo at the start of the `aws-migration` branch.*

## What this proposes

Package the harness as a Docker image that runs beside AILA on the same EC2 instance and reaches AILA by
name over AILA's own network. An engineer starts a run with one command; a systemd timer can run the
same command nightly, but is installed switched off. AILA is not changed.

## Local use stays as it is

The container is an extra way to run the harness, not a replacement. `gather_responses.py`,
`run_evals.py` and the dashboard keep working on a laptop against a local LexChat using OpenRouter, with
`LEXCHAT_API` pointing at it. Each new AILA call falls back on an older build: `/api/version` to
`/api/bot-info`, `/api/system/models` to "unknown". Two things change locally because LexChat changed,
not because of this work:
- **Sign in with an eval account.** LexChat now sets up two-step sign-in for `admin` on first use, so the
  harness cannot sign in as `admin`. Create an account with role `eval` in the local Admin Portal, Users.
- **Spend caps apply locally too** when LexChat uses OpenRouter. As the local admin, raise them in Admin
  Portal, Cost, if a deep-research run is stopped.

## Decisions

| # | Decision | Agreed |
|---|---|---|
| D1 | When runs happen | On demand by default on day 1. A nightly timer is installed but not enabled; switching it on is one documented step. |
| D2 | Where results are viewed | On the instance. Day 1: the dashboard runs there and is opened through a tunnel from a laptop. Later: a dashboard reachable from outside, which needs AILA's cloud team (below). |
| D3 | Release names for this repo | AILA's form, `vYYYY.MM.N`, starting when this work is merged. The install guide checks out a tag. |
| D4 | Where the files live | Deployment files in `deploy/` at the repo root with a one-line-per-file `deploy/README.md`; the guide at `lex_eval/docs/deploy-ec2.md`, indexed in the README. |
| D5 | Judge metrics | Run as they are now. Changes to them come later, as their own work. |
| D6 | What a run covers | Chosen per run with options (question file, ids, chat mode, repeats, metrics, budget). With no options: `questions_new.json`, conversational questions only, two runs each, so a quick check stays cheap. |

### D2: the dashboard on AWS

**Day 1.** The dashboard runs in a container on the instance and is not open to the internet. To see it,
open a tunnel from a laptop with one command (SSM or SSH), then browse to `http://localhost:8501`.

**Later, a dashboard reachable from outside.** AILA's rule is that only its nginx is reachable, so this
needs AILA's cloud team: either a path on AILA's nginx (e.g. `/eval/`), which is a change to AILA, or
a separate load balancer. Streamlit has no login of its own, so either route also needs a sign-in in
front of it. This replaces the public streamlit.io deploy and is a separate piece of work.

### Spend caps: the caps bind well before the $50 eval cap

AILA checks every cap before every model call (`services/spend_caps.py`). An eval account is a user, so
three caps apply to it, not one:

| Cap | Default | What it means for a run |
|---|---|---|
| Per request | $5 | One deep-research answer stops when it reaches $5. |
| Per user per day | $20 | The eval account's whole day. |
| Evaluation per day | $50 | Never reached while the per-user cap is $20. |

The caps are single settings for the whole service, so raising one for the eval account raises it for
every user. Spend on eval traffic also counts toward the service's $100 daily budget shared with users.
The deep-research planner (`/api/research/plan`) is metered as ordinary user traffic, not eval traffic.

Measured on the 12 November Sonnet 5.5 sweep (OpenRouter prices; Bedrock will differ):

| Part of the sweep | AILA cost |
|---|---|
| 12 conversational questions, twice | about $4 |
| 12 deep-research questions, once | about $32, one answer alone $10.63 |

So the default conversational run fits inside the default caps, and a deep-research run of the whole set
does not. Running one needs one of these, and both belong to AILA's people, not to this repo:
- AILA's admin raises the per-request and per-user caps for everyone; or
- AILA's developer exempts eval traffic from the per-request and per-user caps, leaving the eval cap as
  its limit. That is a change to AILA, so it is a question for them, not part of this work.

Until then, deep research can still be run on demand a few questions at a time.

## What a run does

One command, `run`, does these steps in order and stops at the first that fails. The nightly timer, once
enabled, runs the same command with the settings file's defaults:

1. **Take the lock.** If another run holds it, exit at once with code 3.
2. **Check AILA.** Sign in with the eval account, then read `/api/version` (build) and
   `/api/system/models` (model and summarisation model). Each failure has its own message: name not
   resolved, sign-in refused, two-step sign-in asked for (not an eval account), endpoint missing.
3. **Check the budget** (see below). Refuse to start if the run would not fit.
4. **Gather.** Start a new experiment labelled `<date> <model>`, gather the chosen questions, then
   gather them again into the same experiment for each further repeat, using the existing
   `--experiment-id` join. The build and models read in step 2 are stored as the experiment's deployment
   snapshot, marked as server-reported, so no one has to write a deployment file by hand.
5. **Score** the chosen metrics, all of them by default, judge metrics included, with
   `run_evals.py --experiment <id>`.
6. **Summarise.** Print one line and append it to `eval_runs.log`, e.g.
   `2026-11-24 02:00 ok | v2026.11.10 sonnet-5.5 | 24 answers, 0 errors, LexChat $4.10, judge 1,204,330 tokens, 310 of 340 checks passed | experiment 1a2b3c4d`.
   The judge's cost is given in tokens, the only figure the database records for it.

Options for an on-demand run, each overriding the matching setting for that run only:

| Option | Default |
|---|---|
| `--questions` | `questions_new.json` |
| `--question-id` | every question in the file |
| `--chat-mode` | `conversational`; `deep_research` or `all` to widen it |
| `--repeats` | 2 |
| `--metrics` | all |
| `--budget` | `EVAL_BUDGET_USD` |
| `--threads` | 2 |
| `--first-run` | Run although the model has no recorded cost to estimate from |
| `--dry-run` | Print what would be gathered and the expected cost, and stop |

Exit codes:

| Code | Meaning |
|---|---|
| 0 | The run completed. Failed checks are results, not a failed run. |
| 1 | The run failed: AILA unreachable, sign-in refused, an error in the harness. |
| 2 | Refused for money: the budget check, or AILA's spend cap stopped an answer. |
| 3 | Another run is in progress. |

`run_evals.py` returns pytest's code, which is non-zero whenever a metric fails an answer. The run
reads its counts from the database instead, so a model failing checks never reads as a broken run.

Gathers use 2 threads, not the default 10. AILA runs 5 model requests at a time for everyone, so 10 would
queue its users behind the evaluation.

## The budget check

The harness cannot read AILA's spend so far, because that is an admin endpoint. It uses its own records
instead:
`EVAL_BUDGET_USD` is the most the harness may spend on AILA in one day, London time, as AILA's own caps
are counted.
- **Before the run:** the run's expected cost is the mean recorded cost per answer for the same model and
  mode, times the number of answers. If that plus today's recorded spend exceeds the budget, refuse. With
  no history for the model, refuse unless the run is started by hand with `--first-run`.
- **During the run:** after each answer, add its recorded cost. Once the budget is reached, start no new
  question; the ones already in flight finish, so a run can overshoot by up to `--threads` answers.
- **If AILA refuses:** its message ("This request has been stopped because ... has been reached") is
  recognised, the answer is stored as a cap refusal with what it had already cost, it is not retried, no
  new question is started, and the run exits with code 2.
- **Either way,** what was gathered is still scored, and questions never started are not stored.

Default `EVAL_BUDGET_USD` is $10, half the per-user cap, leaving room for the planner and for anything
else the account does that day.

The budget covers AILA's spend only. The judge is paid through OpenRouter, so its use is reported in the
summary line, in tokens, but not capped by the harness. Set a credit limit on the OpenRouter key instead.

## Changes to the harness

The capture version changes when the client or the capture changes. That costs nothing: new gathers start
new experiments anyway. The scoring version changes when `metrics/` or `tests/eval/` changes, which forces
a paid rescore of every experiment being compared.

| Change | Version it moves |
|---|---|
| Sign-in: a clear error when AILA asks for a second step | capture |
| Summarisation model from `/api/system/models`; unknown on a 404, never the main model | capture |
| Build from `/api/version`, with the sign-in inside the error handling | capture |
| Recognise a spend-cap refusal | capture |
| `LEX_API_TLS_VERIFY` defaults to true | neither (reference builds only) |
| New `lex_eval/eval_run.py` (the `run` command above) | neither |
| Citation Domain allows `scotcourts.gov.uk` for case law | **scoring** |

The scotcourts change is in this work, not held back. Scottish judgments are on by default, so without
it every case-law answer citing one fails Citation Domain falsely from the first run. It changes the
scoring version, but only a comparison between an old experiment and a new one needs a rescore, and
those were gathered with Scottish judgments off, so they are a different condition anyway. Two old
experiments keep comparing with each other on their stored scores.

Proxy and certificates: both HTTP clients (httpx for AILA and LEX, the OpenAI client for the judge) read
`HTTPS_PROXY` and `SSL_CERT_FILE` from the environment. A test confirms that; nothing is added unless it
fails. `REQUESTS_CA_BUNDLE` is not needed, because nothing uses `requests`.

Also: drop `google-genai` from the dependencies (nothing imports it), and fix the gather unit test that
signs in to a live LexChat.

## The container

- **Image:** `python:3.12-slim`, the version the harness is developed and formatted on. A non-root user,
  dependencies installed from a lock file, no secrets, tagged `lexeval:${IMAGE_TAG}`.
- **Compose project** `lexeval` (`deploy/compose.yml`):
  - joins the external network `aila_app` and nothing else; publishes no ports;
  - reaches AILA at `LEXCHAT_API=http://nginx:8080`;
  - keeps the database, logs and backups on named volumes;
  - reads settings from an `env_file` outside the repo, mode 0600;
  - the same hardening AILA's services use: read-only root filesystem, `cap_drop: [ALL]`,
    `no-new-privileges`, a memory limit.
- **Question set and reference answers** are built into the image, so a release fixes them. The
  database and logs are the only things that change on the instance.
- **The lock** is a file on the data volume, held with `flock`. The systemd service is also a oneshot,
  so the timer cannot start a second run while one is active; the lock covers runs started by hand.

### Settings

| Name | Secret | Purpose |
|---|---|---|
| `LEXCHAT_API` | no | `http://nginx:8080` |
| `USERNAME`, `PASSWORD` | password | The eval account |
| `OPENROUTER_API_KEY` | yes | The judge |
| `OPENROUTER_JUDGE_MODEL` and the other `OPENROUTER_JUDGE_*` settings | no | Judge choice |
| `LEXCHAT_TIMEOUT` | no | Longest silence on a stream, seconds |
| `HTTPS_PROXY`, `SSL_CERT_FILE`, `NO_PROXY` | no | Proxy and its CA bundle, if any. `NO_PROXY=nginx` keeps AILA's own traffic off the proxy |
| `LEX_API_TLS_VERIFY` | no | Reference builds only; true by default |
| `IMAGE_TAG` | no | The image to run; the previous one is kept for rollback |
| `EVAL_QUESTIONS`, `EVAL_QUESTION_IDS`, `EVAL_CHAT_MODE` | no | What is gathered; default `questions_new.json`, every id, conversational |
| `EVAL_REPEATS` | no | Gathers per question, default 2 |
| `EVAL_METRICS` | no | Metrics to score, default all |
| `EVAL_BUDGET_USD` | no | Most the harness may spend on AILA in a day, default $10 |
| `EVAL_THREADS` | no | Questions in flight at once, default 2 |
| `LEX_EVAL_DB`, `LEX_EVAL_BACKUP_DIR` | no | Set by the image, not the settings file: the database and backups on their volumes |

The schedule itself (`OnCalendar=`, default 02:00 Europe/London) is in the systemd timer, not the
settings file. The install copies the timer into place but does not enable it (D1). The guide gives the
one command that switches it on, and changing the time is one `systemctl edit`.

## Storage and backup

The stored text per answer averages about 10 MB for deep research and 0.7 MB for conversational, so the
nightly conversational run adds roughly 15 MB and a deep-research run roughly 120 MB. That is an
estimate from text length; the real figure on disk is measured during the trial install. A `backup`
command takes the lock, copies the database to a dated file on the backups volume and keeps the newest
seven. The existing `--compact` reclaims space after a deletion.

## The dashboard

A compose service that starts only when asked. Streamlit listens on the container's address on
`aila_app`, port 8501, and publishes nothing. The guide gives the tunnel command, which looks the
container's address up and forwards `localhost:8501` to it (SSM's port-forward to a remote host, or
`ssh -L`). `export_deploy.py` is not run on the instance: it pushes to GitHub, which the instance cannot
reach.

## Outbound hosts

| Host | Port | For | When |
|---|---|---|---|
| `github.com` | 22 | Cloning this repo | Install and update |
| `registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com` | 443 | `python:3.12-slim` | Build |
| `pypi.org`, `files.pythonhosted.org` | 443 | Python packages | Build |
| `deb.debian.org` | 80 | Only if the image installs system packages | Build |
| `openrouter.ai` | 443 | The judge | Every run |
| `lex.lab.i.ai.gov.uk` | 443 | Reference answer builds | Only when building references |

Every row on port 443 is HTTPS. AILA's own install guide (`DOCKER_EC2_INSTALL.md`, section 1.3) already
allows every row: the build-time rows for its own clone and image build, `lex.lab.i.ai.gov.uk` always,
and `openrouter.ai` when AILA uses OpenRouter. If AILA moves to Bedrock and drops `openrouter.ai`, the
judge needs it added back.

## Tests

- **Unit tests,** offline: eval-account sign-in, a two-step reply, `/api/system/models` and its 404,
  `/api/version`, a spend-cap refusal, the budget check, the lock, exit codes, and the proxy and CA
  settings reaching both clients.
- **Smoke test:** `deploy/smoke/smoke.sh` builds the image and runs it with `docker compose` against
  a stub AILA (`deploy/smoke/stub_aila.py`: sign-in, the read endpoints, a canned v6 `audit` stream)
  on a test network. It checks the image (non-root, no `.env` or database inside, read-only root),
  every command, each exit code including the lock and a spend-cap refusal, and that the dashboard
  renders. No AILA, no judge key, no cost; it removes everything it creates.
- **Trial install:** follow the guide from a clean checkout on Linux, against AILA's own compose stack
  from the `LexChat/` clone, and fix each step that does not work as written.

## Order of work

Each step is its own reviewable change:
1. Client changes and their tests.
2. `eval_run.py`: the command, its options, the budget check, the lock, the summary.
3. Packaging: lock file, Dockerfile, compose file, systemd units, `deploy/README.md`.
4. The stub and the smoke test.
5. The install guide.
6. The trial install, with disk, memory, time and cost measured for the guide.

## Two sign-in facts for the install guide

Found in AILA's code while building the client, and not in the original brief:
- **A new account must choose its own password first.** AILA marks every account an admin creates as
  "must change password", and refuses every other request until it does. Someone signs in once as the
  eval account in a browser and sets the password that then goes into the settings file. The harness
  says so if this step was missed.
- **Okta blocks eval accounts.** When AILA's sign-in method is Okta, it refuses a password sign-in for
  every account except admins, eval accounts included. The harness cannot sign in on such an instance
  until AILA allows it, which is a change to AILA and a question for its developer.

## Not covered by this proposal

- **Changes to AILA.** The cap exemption under D1 is the only one this work would want. Ask before
  anything that needs one.
- **The judge stability changes.** Later, as their own work (D5). Until then, read a single run's
  Groundedness and Claim Support verdicts with care: on 12 November about one in five flipped when the
  same answers were scored again.
- **Alerting.** A failed run shows in `systemctl` and the logs only.
- **What will probably remain untested:** a TLS-inspecting proxy, SSM port-forwarding, and the real
  allow-list.
- **A dependency on AILA's next release.** `/api/system/models` is in AILA's unreleased changes, after
  v2026.11.9. Against v2026.11.9 itself the summarisation model is recorded as unknown, which is correct
  but makes those experiments less informative.
