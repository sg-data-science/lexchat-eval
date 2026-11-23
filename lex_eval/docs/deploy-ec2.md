# Installing lexchat-eval on AILA's EC2 instance

**For:** the cloud engineers who install and run AILA.

**Not yet followed from start to finish.** The image, its commands and the dashboard passed a smoke test against a stand-in for AILA. No step here has been run on a real EC2 instance or against a real AILA. Section 10 lists what is untested.

The design and its reasons are in [decisions/aws-deployment.md](decisions/aws-deployment.md). The files are listed in [`deploy/README.md`](../../deploy/README.md).

## 1. What this is

lexchat-eval asks AILA a fixed set of legal questions, scores the answers, and keeps the results in a database on the instance. It runs in its own container beside AILA, only when someone starts a run or the optional nightly timer does. It calls AILA over AILA's own Docker network, using an ordinary AILA account, and uses OpenRouter for the scores that need a language model. It changes no AILA file, container or setting. AILA records its sign-ins and spend as it does for any account.

## 2. Before you start

### 2.1 The instance

Use the AILA instance itself, with AILA's stack running.

| | Needed |
|---|---|
| AILA | `v2026.11.9` or later |
| Docker Compose | 2.24 or later: `sudo docker compose version` |
| Memory | Up to 2 GB during a run, and up to 2 GB more while the dashboard is open. These are limits each container is held to, not measurements. |
| Disk | **Not measured yet.** The trial install will give the image size and the growth per run. Estimated growth is 15 MB for each default run, and 10 MB for each deep-research answer. |

**Inbound:** nothing. No port is published. The dashboard is reached through a tunnel (section 7.5).

### 2.2 Outbound

| Host | Port | For | When | Already allowed for AILA |
|---|---|---|---|---|
| `github.com` | 22 | The code | Install and update | Yes |
| `registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com` | 443 | Base image | Build | Yes |
| `pypi.org`, `files.pythonhosted.org` | 443 | Python packages | Build | Yes |
| `openrouter.ai` | 443 | Scoring | Every run | Only if AILA uses OpenRouter |

**If AILA uses Bedrock, check that `openrouter.ai` is still allowed.** Scoring needs it whichever provider AILA uses.

`lex.lab.i.ai.gov.uk` is not needed. The reference answers are built elsewhere and come with each release.

### 2.3 What the AILA admin must do first

**Check the sign-in method.** If AILA signs users in through Okta, stop here and ask AILA's developer. AILA then refuses a password sign-in from every account except an admin's, and this cannot work.

**Create the account.** It needs the role `eval`, which the Admin Portal's role list does not offer, so it takes one command in the browser:

1. Sign in to AILA as an admin.
2. Open the browser's developer tools (F12), then the Console tab.
3. Paste this and press Enter. Some browsers ask you to type `allow pasting` first. It asks for a first password of at least 12 characters.

   ```js
   fetch('/api/users', {
     method: 'POST',
     headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({username: 'eval', role: 'eval', password: prompt('First password')})
   }).then(async r => console.log(r.status, await r.text()))
   ```

   It should print `201` and a line containing `"role":"eval"`.

Do not create it as a User and change the role later. Its first sign-in would set up two-step sign-in, which this tool cannot use.

**Set its real password.** AILA makes every new account choose its own password, and refuses everything else until it has. Make one with letters and digits only, because Docker Compose treats `$`, `#` and quotes in the settings file specially:

```sh
openssl rand -hex 24
```

Sign in to AILA once as `eval` in a private browser window, with the first password, and set this one when asked. Pass it to the installer securely.

### 2.4 What else you need

- An OpenRouter API key for the scoring, with a credit limit set on it at OpenRouter. This tool does not cap what scoring spends.
- The release tag to install.
- Confirmation that AILA's network exists:

  ```sh
  sudo docker network ls --filter name=aila_app
  ```

  It should list `aila_app`.

## 3. Get the code and configure it

### 3.1 The code

The repository is private. Read it with its own read-only deploy key, as for AILA:

```sh
sudo install -d -m 700 /root/.ssh
sudo ssh-keygen -t ed25519 -N '' -C lexeval-deploy -f /root/.ssh/lexeval_deploy
sudo cat /root/.ssh/lexeval_deploy.pub
```

Add that public key to the repository on GitHub (Settings, Deploy keys, without write access). Then:

```sh
SSHCMD='ssh -i /root/.ssh/lexeval_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'
sudo sh -c "umask 022; git -c core.sshCommand='$SSHCMD' clone git@github.com:tomwilsonsco/lexchat-eval.git /opt/lexchat-eval"
cd /opt/lexchat-eval
sudo git config core.sshCommand "$SSHCMD"
sudo sh -c 'umask 022; git checkout <release tag>'
```

Use a release tag, not a branch, and keep the path `/opt/lexchat-eval`, which the systemd service expects. The `umask` keeps the files readable by the container's user, which is not root.

### 3.2 The settings file

```sh
cd /opt/lexchat-eval
sudo install -d -m 700 /etc/lexeval
sudo install -m 600 deploy/settings.example /etc/lexeval/settings.env
sudo sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG=<release tag>/' /etc/lexeval/settings.env
sudoedit /etc/lexeval/settings.env
```

In the editor, fill in `PASSWORD` and `OPENROUTER_API_KEY`. The other values work as they are. Then check that both secrets are set, without showing them:

```sh
sudo grep -cE '^(PASSWORD|OPENROUTER_API_KEY)=.' /etc/lexeval/settings.env      # must print 2
```

| Name | Required | Example | What it does |
|---|---|---|---|
| `IMAGE_TAG` | Yes | `v2026.12.1` | The image to build and run. Set it to the release tag. |
| `LEXCHAT_API` | Yes | `http://nginx:8080` | AILA, by name on its own network. Never an IP address. |
| `USERNAME` | Yes | `eval` | The account from section 2.3. |
| `PASSWORD` | Yes, **secret** | | Its password. Letters and digits only. |
| `OPENROUTER_API_KEY` | Yes, **secret** | | The key for scoring. |
| `OPENROUTER_JUDGE_MODEL`, and the other `OPENROUTER_JUDGE_*` lines | Yes | `openai/gpt-5.6-luna` | The scoring model. Changing any of them makes new scores impossible to compare with old ones. |
| `EVAL_WORKERS` | No | `4` | Answers scored at once. |
| `LEXCHAT_TIMEOUT` | No | `600` | Seconds an answer may be silent before it is given up. AILA's nginx gives up at 600. |
| `EVAL_QUESTIONS` | No | `lex_eval/data/questions_new.json` | The question file, inside the image. |
| `EVAL_QUESTION_IDS` | No | `13 14` | Only these questions. |
| `EVAL_CHAT_MODE` | No | `conversational` | `conversational`, `deep_research` or `all`. Deep research costs far more (section 8). |
| `EVAL_REPEATS` | No | `2` | Answers per question. |
| `EVAL_METRICS` | No | `all` | Which scores to run. |
| `EVAL_THREADS` | No | `2` | Questions asked at once. AILA serves five model requests at a time for everyone, so keep this low. |
| `EVAL_BUDGET_USD` | No | `10` | The most to spend on AILA in one day, London time. |
| `EVAL_MEM_LIMIT` | No | `2g` | Memory limit for each container. |
| `HTTPS_PROXY` | Only behind a proxy | `http://proxy.example:3128` | The outbound proxy. |
| `NO_PROXY` | Only behind a proxy | `nginx,localhost` | Keeps calls to AILA off the proxy. |
| `SSL_CERT_FILE` | Only behind a TLS-inspecting proxy | `/etc/lexeval/ca/bundle.pem` | The trusted certificates (below). |

**Behind a TLS-inspecting proxy.** `SSL_CERT_FILE` replaces the trusted certificates, so the file must hold the public ones and the proxy's own. It must be readable by the container's user:

```sh
sudo sh -c 'cat /etc/ssl/certs/ca-certificates.crt /path/to/corporate-ca.pem > deploy/ca/bundle.pem'
sudo chmod 644 deploy/ca/bundle.pem
```

As for AILA, the proxy settings cover running containers only. Configure Docker's own image pulls and builds yourselves.

## 4. Build and check

Every command from here uses `$C`. Set it again in each new session:

```sh
cd /opt/lexchat-eval
C="sudo docker compose --env-file /etc/lexeval/settings.env -f deploy/compose.yml"
```

Build the image, which takes a minute or two. Include `eval`: without it, the build does not cover this image.

```sh
$C build eval
sudo docker image ls lexeval      # lists the release tag
```

**Check the connections.** This signs in to AILA and tests the OpenRouter key. It sends no question and costs nothing:

```sh
$C run --rm eval check
```

It should print AILA's build and models, and that OpenRouter accepted the key:

```
ok: LexChat v2026.11.10, model <model>, summarisation model <model>; OpenRouter accepted the key
```

"LexChat" is AILA's name in its code. "Summarisation model unknown" is expected on AILA `v2026.11.9`. "OpenRouter not checked" means the key is missing from the settings file. Any other answer says why it failed (section 11).

Do not start a run until this prints the line above. `run` itself does not test OpenRouter first, so with it blocked a run would pay AILA for answers it could not fully score.

## 5. First run

See what it will do. This asks AILA no questions:

```sh
$C run --rm eval run --dry-run --first-run
```

Start the run in the background, so that a dropped session does not stop it. `--first-run` is needed because there is no earlier cost to estimate from.

```sh
$C run -d --name lexeval-first eval run --first-run
sudo docker logs -f lexeval-first          # Ctrl+C stops watching, not the run
```

It asks 12 questions twice. Expect about 15 minutes and about $5 in total (section 8). Both are estimates from earlier runs on a developer's machine.

When it finishes, read its exit code and remove the container:

```sh
sudo docker wait lexeval-first
sudo docker rm lexeval-first
```

The last line of the log is the summary, e.g.:

```
2026-11-24 02:00 ok | v2026.11.10 sonnet-5.5 | 24 answers, 0 errors, LexChat $4.10, judge 678,000 tokens, 310 of 340 checks passed | experiment 1a2b3c4d
```

From left to right: when it started (London time), how it ended, AILA's build and model, what was gathered and spent, the checks passed, and the name of the result set. **Failed checks are findings about AILA's answers, not a fault in the run.**

| Exit code | Meaning |
|---|---|
| 0 | The run completed. |
| 1 | The run failed. The summary says why. |
| 2 | Refused or stopped over money: the daily budget, or one of AILA's caps. What was gathered is still scored. |
| 3 | Another run was already going. Nothing was done. |

## 6. The schedule

Nothing runs on its own until you switch the timer on. Install it switched off:

```sh
cd /opt/lexchat-eval
sudo cp deploy/lexeval-run.service deploy/lexeval-run.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl is-enabled lexeval-run.timer      # disabled
```

**Start one run now** through systemd, which keeps its log. Complete section 5 first:

```sh
sudo systemctl start --no-block lexeval-run.service
sudo journalctl -u lexeval-run.service -f
```

**Switch on the nightly run**, at 02:00 London time:

```sh
sudo systemctl enable --now lexeval-run.timer
systemctl list-timers lexeval-run.timer          # shows the next run
```

It costs $130 to $160 a month at the estimates in section 8. Agree that with the service owner first.

**Change the time or how often it runs:**

```sh
sudo systemctl edit lexeval-run.timer
```

Add these lines. The empty one clears the old time; without it, both apply. This example is Mondays only:

```
[Timer]
OnCalendar=
OnCalendar=Mon *-*-* 02:00:00 Europe/London
```

Keep it outside office hours, because the run shares AILA's model queue with its users.

## 7. Day to day

Set `$C` first (section 4).

### 7.1 Logs and results

```sh
$C run --rm --entrypoint tail eval -n 20 /var/lib/lexeval/eval_runs.log     # one line per run
sudo journalctl -u lexeval-run.service -n 200                              # full log of systemd runs
systemctl status lexeval-run.service                                        # how the last one ended
```

The database and run log are in the volume `lexeval_data`, on disk at `/var/lib/docker/volumes/lexeval_data/_data`.

### 7.2 A run with different options

Options replace the settings file for that run only. `$C run --rm eval run --help` lists them. For example, one deep-research question, once:

```sh
$C run --rm eval run --chat-mode deep_research --question-id 23 --repeats 1 --first-run
```

The first run in each mode, and the first after AILA's model changes, needs `--first-run`. **After a model change the nightly run is refused with exit code 2 until you do this.**

### 7.3 Updating and rolling back

Back up first. Wait for any run to finish (`sudo docker ps --filter name=lexeval`).

```sh
$C run --rm eval backup
sudo sh -c 'umask 022; git fetch --tags && git checkout <new tag>'
sudo sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG=<new tag>/' /etc/lexeval/settings.env
$C build eval
sudo docker image ls lexeval                     # both the old and new tags
sudo cp deploy/lexeval-run.service deploy/lexeval-run.timer /etc/systemd/system/
sudo systemctl daemon-reload
$C run --rm eval check
```

If the dashboard is running, recreate it: `$C --profile dashboard up -d dashboard`.

Changing `IMAGE_TAG` is what keeps the old image. Remove old ones with `sudo docker image rm lexeval:<old tag>`. `sudo docker image prune -a` removes every lexeval image not in use, including the current one; if that happens, build again.

**To roll back,** check out the old tag, set `IMAGE_TAG` back and copy the units again. There is nothing to build. If the older release cannot read the database, restore the backup.

### 7.4 Backing up and restoring

```sh
$C run --rm eval backup
$C run --rm --entrypoint ls eval -lh /var/backups/lexeval
```

It refuses while a run is going, and keeps the newest seven. Nothing takes a backup automatically. The copies are in the volume `lexeval_backups` (`/var/lib/docker/volumes/lexeval_backups/_data`), **on the same disk as the database**. The results should stay in the environment unless their owner agrees otherwise, so ask before copying them elsewhere.

**Restoring** replaces the current database. Stop everything first, and continue only when the last command lists nothing:

```sh
sudo systemctl stop lexeval-run.timer
$C --profile dashboard stop dashboard
sudo docker ps --filter name=lexeval
```

Then, with a file name from the list above:

```sh
$C run --rm --entrypoint sh eval -c 'set -e; cd /var/lib/lexeval; B=/var/backups/lexeval; test -f $B/$1
for f in responses.db responses.db.wal; do if [ -f $f ]; then mv $f before-restore-$f; fi; done
cp $B/$1 responses.db
if [ -f $B/$1.wal ]; then cp $B/$1.wal responses.db.wal; fi
ls -lh' sh responses-20261124-020000.db
```

The replaced database is kept beside it as `before-restore-responses.db`, until the next restore overwrites it. If the nightly run was on, start it again with `sudo systemctl start lexeval-run.timer`.

### 7.5 The dashboard

Start it after the first run:

```sh
$C --profile dashboard up -d dashboard
sudo docker inspect -f '{{(index .NetworkSettings.Networks "aila_app").IPAddress}}' lexeval-dashboard-1
```

The second command prints the dashboard's address. It can change whenever the container is recreated. From your own computer, forward a port to it, with Session Manager:

```sh
aws ssm start-session --target <instance id> \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters '{"host":["<address>"],"portNumber":["8501"],"localPortNumber":["8501"]}'
```

or with SSH:

```sh
ssh -N -L 8501:<address>:8501 <user>@<instance>
```

Leave that running and open `http://localhost:8501`.

**It has no sign-in.** Anyone with a session on the instance, and any container on `aila_app`, can open it. Stop it when you have finished, and before a run: opening a page at the wrong moment can make either the page or the run fail.

```sh
$C --profile dashboard stop dashboard
```

## 8. Costs and limits

A run spends money twice. These are estimates for the default run (12 conversational questions, twice) on Sonnet 5.5, from two runs in October and November 2026 at OpenRouter prices. Bedrock prices differ.

| | Per run | Nightly, per month |
|---|---|---|
| AILA's model, through the `eval` account | $4 to $5 | $128 to $153 |
| Scoring, through OpenRouter | about $0.20 | about $6 |

**Deep research costs far more.** The 12 deep-research questions, once, cost $32 on AILA, and one answer alone cost $10.63. That does not fit inside AILA's default caps, so ask for a few questions at a time.

AILA's caps, at their defaults. An AILA admin sees them in Admin Portal, Cost:

| Cap | Default | Effect here |
|---|---|---|
| One request | $5 | Stops an expensive deep-research answer. |
| One account, per day | $20 | The limit a run meets first, not the evaluation cap. |
| One account, per month | $200 | A nightly run uses most of it. |
| Evaluation, per day | $50 | Not reached while the account cap is $20. |
| Whole service, per day | $100 | Shared with AILA's users. |
| Whole service, per month | $1,000 | Shared with AILA's users. |

The caps apply to the whole service, so raising one for this account raises it for everyone.

- **The daily budget** (`EVAL_BUDGET_USD`) is this tool's own limit, set below AILA's. A run that would exceed it is refused, and a run that reaches it starts no new questions. Answers already started finish, so it can be passed by up to `EVAL_THREADS` answers.
- **When AILA's cap is reached,** AILA refuses the answer, the run stops and scores what it has, and exits with code 2.
- **The budget relies on the costs AILA reports.** AILA reports no cost for a Bedrock model missing from its price table. The budget then cannot work, so check that the first run's summary shows a cost above zero.
- **Scoring is not capped here.** The credit limit on the OpenRouter key is the only limit.

## 9. Stopping and removing it

Set `$C` first (section 4). Nothing in this section affects AILA.

**Pause the schedule.** A run already going continues:

```sh
sudo systemctl disable --now lexeval-run.timer
```

**Stop a run.** Answers not yet finished are lost, though AILA has charged for them. Use the name the first command prints:

```sh
sudo docker ps --filter name=lexeval
sudo docker stop <name>
```

**Remove it all.** This deletes every result and backup for good:

```sh
sudo systemctl disable --now lexeval-run.timer
sudo rm /etc/systemd/system/lexeval-run.service /etc/systemd/system/lexeval-run.timer
sudo systemctl daemon-reload
$C --profile dashboard --profile commands down -v
sudo docker volume ls --filter name=lexeval        # lists nothing
sudo docker image ls lexeval
sudo docker image rm lexeval:<tag>                 # each tag listed
cd / && sudo rm -r /opt/lexchat-eval /etc/lexeval /root/.ssh/lexeval_deploy /root/.ssh/lexeval_deploy.pub
```

Then delete the `eval` account in AILA, the deploy key on GitHub, and the OpenRouter key.

**Taking AILA's own stack down.** The dashboard, or a run in progress, stays attached to `aila_app`, so AILA's `docker compose down` cannot remove that network while one is running. Stop both first. Once AILA is back, recreate the dashboard with `$C --profile dashboard up -d --force-recreate dashboard`.

## 10. What it does not do, and what is not tested

It does not:
- send alerts. A failed run shows in `systemctl status` and the logs only;
- use TLS to reach AILA. The traffic stays on the instance's Docker network;
- cap what scoring spends;
- back itself up, or copy anything off the instance;
- update the reference answers. They come with each release.

**Tested** by the smoke test, on WSL with a stand-in for AILA: the build, the non-root user and read-only filesystem, `check`, `run` and `backup`, the four exit codes, the dashboard, and that the timer's time is valid. The OpenRouter part of `check` and the steps of the restore command were tested outside a container.

**Not tested:**
- a real EC2 instance, and a real AILA behind its own nginx;
- scoring with a language model from inside the container;
- the browser command in section 2.3, which was written from AILA's code;
- the deploy key, clone and file-permission steps in section 3.1;
- the background run in section 5, and the systemd service and timer running;
- updating, rolling back, restoring inside the container, stopping a run, and removing it all;
- both dashboard tunnels;
- a forward proxy or a TLS-inspecting proxy, and the real allow-list;
- disk and memory use.

Section 8's costs and section 5's time come from runs on a developer's machine.

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `network aila_app declared as external, but could not be found` | AILA's stack is not running. | Start AILA's stack. |
| `Cannot reach LexChat ... name resolution` or `Name or service not known` | `LEXCHAT_API` is wrong, AILA's nginx is down, or the container is not on `aila_app`. | Set it to `http://nginx:8080`, and check AILA's nginx is running. |
| `Cannot reach LexChat ... Connection refused` | The wrong port, or nginx is still starting. | Use port 8080. Try again in a minute. |
| `refused the user name or password ... (HTTP 401)` | The wrong name or password, or the account is locked. Five wrong passwords lock it for 15 minutes, with this same message. | Correct it, wait 15 minutes, and check again. |
| `asked 'eval' for a two-step sign-in code` | The account's role is not `eval`, or it set up two-step sign-in before it was. | An admin deletes it and creates it again (section 2.3). |
| `must set its own password before it can be used` | Nobody has signed in as the account in a browser, or an admin has reset its password. | Section 2.3, "Set its real password". |
| `signs in through Okta` (HTTP 403) | AILA uses Okta, which refuses this account. | AILA's developer has to decide. |
| `check` says `ok`, but every answer in the run fails with 403 | The account's role is not `eval`. `check` does not notice this where AILA has two-step sign-in switched off. | An admin deletes it and creates it again (section 2.3). |
| Summarisation model `unknown`, or a 404 on `/api/system/models` in the log | AILA is `v2026.11.9` or older. | Nothing. It is recorded as unknown, and the run works. |
| `LexChat has no active model` | No model is set in AILA. | An admin sets one in Admin Portal, Developer. |
| `OpenRouter refused the key (HTTP 401)` | The OpenRouter key is wrong. | Correct `OPENROUTER_API_KEY`. |
| `Cannot use OpenRouter (...)` with a refused connection, a name that does not resolve, or a timeout | `openrouter.ai` is not allowed, or the proxy is not set. | Section 2.2, or `HTTPS_PROXY`. |
| `CERTIFICATE_VERIFY_FAILED` | A TLS-inspecting proxy whose certificate is not trusted, or a bundle without the public certificates. | Section 3.2, "Behind a TLS-inspecting proxy". |
| A Python traceback ending `FileNotFoundError` or `PermissionError`, from `load_verify_locations` | `SSL_CERT_FILE` points at a missing file, or the file is not readable. | It must be in `deploy/ca/`, mode 644. |
| Exit 2, `no recorded cost ... start it with --first-run` | The first run, or the first since AILA's model changed. | Section 7.2. |
| Exit 2, `would exceed the ... budget` | Today's runs have used the daily budget. | Wait until tomorrow, or raise `EVAL_BUDGET_USD`, keeping it below AILA's $20. |
| Exit 2, `the budget or a LexChat cap was reached` | The run reached its budget, or AILA's cap refused an answer. | The log's `Stopping:` line says which. For a cap, ask an AILA admin. |
| Exit 3, `another run is in progress` | Two runs overlapped. The second did nothing. | Wait for the first. A leftover lock file does no harm. |
| Exit 1, `scoring did not complete` | Every answer failed, or the scoring did. | Read the log above the summary. Check AILA answers a question in a browser. |
| `Permission denied` on a file under `/app` | The code was checked out with a restrictive umask. | `sudo chmod -R a+rX lex_eval deploy pyproject.toml`, then build again. |
| `No such image: lexeval:<tag>` | `IMAGE_TAG` does not match a built image. | `$C build eval`. |
| The dashboard says `No results at this path` | No run has finished yet. | Run section 5 first. |
| The tunnel connects, but the page does not load | The dashboard's address changed. | Look it up again (section 7.5). |
