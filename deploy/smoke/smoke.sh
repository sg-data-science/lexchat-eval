#!/usr/bin/env bash
# Smoke test of the lexchat-eval container, against a stand-in for AILA
# (stub_aila.py). Needs Docker with Compose 2.24 or later; no AILA, no
# OpenRouter key, no cost. From the repo root:
#
#   deploy/smoke/smoke.sh                  # or: sudo deploy/smoke/smoke.sh
#   KEEP_IMAGE=1 deploy/smoke/smoke.sh     # keep the lexeval:smoke image afterwards
#
# Everything it creates is named lexeval-smoke* and removed at the end, so it
# leaves no trace of itself and touches nothing of AILA's or the real install's.
# Full output of every step: deploy/smoke/smoke.log.

set -u

REPO=$(cd "$(dirname "$0")/../.." && pwd)
PROJECT=lexeval-smoke
NET=lexeval-smoke-net
STUB=lexeval-smoke-aila
IMAGE=lexeval:smoke
LOG="$REPO/deploy/smoke/smoke.log"
WORK=$(mktemp -d)
passed=0
failed=0

: >"$LOG"

dc() {
  LEXEVAL_SETTINGS="$WORK/settings.env" AILA_NETWORK="$NET" docker compose \
    -p "$PROJECT" --env-file "$WORK/settings.env" -f "$REPO/deploy/compose.yml" "$@"
}
ev() { dc run --rm -T eval "$@"; }
stub() { docker exec "$STUB" python /stub/stub_aila.py "$@"; }

remove_test_objects() {
  {
    dc --profile dashboard down -v --remove-orphans
    docker rm -f "$STUB"
    docker network rm "$NET"
  } >>"$LOG" 2>&1
}

cleanup() {
  remove_test_objects
  [ -n "${KEEP_IMAGE:-}" ] || docker image rm "$IMAGE" >>"$LOG" 2>&1
  rm -rf "$WORK"
}

# expect NAME EXIT_CODE TEXT COMMAND...: run COMMAND, and pass when it exits
# with EXIT_CODE and its output contains TEXT (any output when TEXT is "").
expect() {
  local name=$1 want=$2 text=$3 out rc
  shift 3
  out=$("$@" 2>&1)
  rc=$?
  printf '\n=== %s (exit %s)\n%s\n' "$name" "$rc" "$out" >>"$LOG"
  if [ "$rc" = "$want" ] && { [ -z "$text" ] || grep -qF -- "$text" <<<"$out"; }; then
    printf 'ok    %s\n' "$name"
    passed=$((passed + 1))
  else
    printf 'FAIL  %s: exit %s, wanted %s%s\n' "$name" "$rc" "$want" \
      "${text:+ and output containing \"$text\"}"
    tail -n 5 <<<"$out" | sed 's/^/        /'
    failed=$((failed + 1))
  fi
}

# wait_for SECONDS COMMAND...: retry COMMAND once a second until it succeeds.
wait_for() {
  local limit=$1 i
  shift
  for ((i = 0; i < limit; i++)); do
    "$@" >>"$LOG" 2>&1 && return 0
    sleep 1
  done
  return 1
}

finish() {
  printf '\n%s passed, %s failed. Full output: %s\n' "$passed" "$failed" "$LOG"
  [ "$failed" = 0 ]
  exit $?
}

# ---------------------------------------------------------------------------
# Before starting
# ---------------------------------------------------------------------------

if ! version=$(docker compose version --short 2>/dev/null); then
  echo "Docker Compose not found (docker compose version failed)." >&2
  exit 1
fi
if ! printf '2.24.0\n%s\n' "${version#v}" | sort -V -C; then
  echo "Docker Compose $version is too old: the settings file needs 2.24 or later." >&2
  exit 1
fi
echo "Docker Compose $version. Full output goes to $LOG"

cat >"$WORK/settings.env" <<'EOF'
IMAGE_TAG=smoke
LEXCHAT_API=http://nginx:8080
USERNAME=eval
PASSWORD=smoke-password
OPENROUTER_API_KEY=
LEXCHAT_TIMEOUT=60
EVAL_QUESTIONS=lex_eval/data/questions_new.json
EVAL_CHAT_MODE=conversational
EVAL_REPEATS=2
EVAL_METRICS=all
EVAL_THREADS=2
EVAL_BUDGET_USD=10
EOF

remove_test_objects # anything left by an earlier run that was interrupted
trap cleanup EXIT
# Stands in for aila_app, AILA's network, which compose.yml expects to exist.
docker network create "$NET" >>"$LOG" 2>&1

# ---------------------------------------------------------------------------
# The image
# ---------------------------------------------------------------------------

expect "image builds" 0 "" dc build eval
if [ "$failed" != 0 ]; then
  echo "Nothing else can be tested without the image."
  finish
fi
expect "help lists the commands" 0 "Commands:" ev help
expect "runs as user 10001, not root" 0 "10001" \
  dc run --rm -T --entrypoint id eval -u
expect "no .env or database copied into the image" 0 "none" \
  dc run --rm -T --entrypoint sh eval -c \
  'found=$(find /app \( -name .env -o -name "*.db" -o -name "*.db.wal" \)); echo "${found:-none}"'
expect "the container's root filesystem is read-only" 0 "root filesystem ro" \
  dc run --rm -T --entrypoint sh eval -c \
  'echo "root filesystem $(awk "\$2 == \"/\" { split(\$4, o, \",\"); print o[1] }" /proc/mounts)"'

# ---------------------------------------------------------------------------
# The stand-in AILA, as "nginx" on its own network, like AILA's
# ---------------------------------------------------------------------------

docker run -d --name "$STUB" --network "$NET" --network-alias nginx --read-only \
  -v "$REPO/deploy/smoke/stub_aila.py:/stub/stub_aila.py:ro" \
  --entrypoint python "$IMAGE" /stub/stub_aila.py >>"$LOG" 2>&1
if ! wait_for 30 stub --get; then
  echo "FAIL  the stand-in AILA did not start; see $LOG"
  failed=$((failed + 1))
  finish
fi

# ---------------------------------------------------------------------------
# Commands, against the stand-in
# ---------------------------------------------------------------------------

expect "check signs in and reads the build" 0 "v2026.12.0-smoke" ev check
expect "a wrong password is a clear failure" 1 "HTTP 401" \
  dc run --rm -T -e PASSWORD=wrong eval check
expect "the first run needs --first-run" 2 "--first-run" \
  ev run --question-id 13 14
expect "a dry run gathers nothing" 0 "dry run: nothing gathered" \
  ev run --dry-run --first-run --question-id 13 14
expect "a run gathers and scores (2 questions x 2)" 0 "4 answers, 0 errors" \
  ev run --first-run --question-id 13 14

# A second run while one is going: the stand-in holds each answer for 20s.
stub --set delay=20 >>"$LOG" 2>&1
ev run --question-id 13 --repeats 1 >"$WORK/first.out" 2>&1 &
first=$!
wait_for 120 sh -c "docker exec $STUB python /stub/stub_aila.py --get | grep -q '\"in_progress\": 1'"
expect "a second run waits its turn (exit 3)" 3 "another run is in progress" \
  ev run --question-id 13 --repeats 1
wait "$first"
first_rc=$?
expect "the first run still completes" 0 "ok |" \
  sh -c "cat '$WORK/first.out'; exit $first_rc"
stub --set delay=0 >>"$LOG" 2>&1

expect "a run over the budget is refused (exit 2)" 2 "budget" \
  dc run --rm -T -e EVAL_BUDGET_USD=0.01 eval run --question-id 13 --repeats 1

stub --set refuse=1 >>"$LOG" 2>&1
expect "AILA's spend cap stops the run (exit 2)" 2 "a LexChat cap was reached" \
  ev run --question-id 13 14 --repeats 1
stub --set refuse=0 >>"$LOG" 2>&1

expect "backup copies the database" 0 "ok: backed up to /var/backups/lexeval/" ev backup
expect "every run is in the run log" 0 "ok | v2026.12.0-smoke" \
  dc run --rm -T --entrypoint cat eval /var/lib/lexeval/eval_runs.log

# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------

expect "the dashboard page renders without an error" 0 "no errors" \
  dc run --rm -T --entrypoint python eval -c '
import sys
from streamlit.testing.v1 import AppTest
app = AppTest.from_file("lex_eval/reports/streamlit_report.py", default_timeout=180).run()
errors = [e.message for e in app.exception]
print("errors:", errors if errors else "no errors")
sys.exit(1 if errors else 0)'

dc --profile dashboard up -d dashboard >>"$LOG" 2>&1
expect "the dashboard service answers on its network" 0 "" \
  wait_for 90 docker exec "$STUB" python -c \
  'import urllib.request; print(urllib.request.urlopen("http://dashboard:8501/_stcore/health", timeout=5).read().decode())'
dc --profile dashboard stop dashboard >>"$LOG" 2>&1

# ---------------------------------------------------------------------------
# The nightly timer (only where systemd is installed)
# ---------------------------------------------------------------------------

if command -v systemd-analyze >/dev/null; then
  expect "the timer's time is valid" 0 "Next elapse" \
    systemd-analyze calendar "*-*-* 02:00:00 Europe/London"
  if systemd-analyze verify "$REPO/deploy/lexeval-run.service" \
    "$REPO/deploy/lexeval-run.timer" >>"$LOG" 2>&1; then
    echo "ok    the unit files verify"
    passed=$((passed + 1))
  else
    echo "note  systemd-analyze verify had remarks (expected off the server); see $LOG"
  fi
else
  echo "skip  the nightly timer: no systemd-analyze here"
fi

finish
