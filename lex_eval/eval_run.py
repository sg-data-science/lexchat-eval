"""
One evaluation run against LexChat: check it, gather answers, score them, and
write a one-line summary.

    python -m lex_eval.eval_run                                  # the defaults
    python -m lex_eval.eval_run --chat-mode deep_research --question-id 8 9
    python -m lex_eval.eval_run --dry-run                        # what it would do
    python -m lex_eval.eval_run --check      # test LexChat and OpenRouter, at no cost
    python -m lex_eval.eval_run --backup     # copy the database, between runs

With no options it gathers the conversational questions in questions_new.json
twice each, into one new experiment, and scores every metric. Each option
overrides the matching EVAL_* setting in lex_eval/.env for this run only.

Money: EVAL_BUDGET_USD is the most the harness may spend on LexChat in one day
(London time), counted from the answers recorded in responses.db. A run that
would go over it is refused before it starts, and a gather stops starting new
questions once it is reached. LexChat's own spending caps still apply on top.

Exit codes: 0 the run completed (failed checks are results, not a failed run);
1 the run failed; 2 it was refused or stopped over money; 3 another run holds
the lock.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import duckdb
import httpx
from openai import AuthenticationError, OpenAIError

from lex_eval.run_evals import METRIC_FILES
from lex_eval.utils.db import DEFAULT_DB
from lex_eval.utils.get_llm import (
    get_active_model,
    get_lexchat_build,
    get_summarisation_model,
)
from lex_eval.utils.lexchat_client import ConfigError, SignInError

HERE = Path(__file__).parent
LOCK_FILE = DEFAULT_DB.parent / "eval_run.lock"
RUN_LOG = DEFAULT_DB.parent / "eval_runs.log"
BACKUP_DIR = Path(os.getenv("LEX_EVAL_BACKUP_DIR") or DEFAULT_DB.parent / "backups")
BACKUPS_KEPT = 7
LONDON = ZoneInfo("Europe/London")

OK, FAILED, MONEY, LOCKED = 0, 1, 2, 3
# run_evals.py's exit code when there was no answer to score or no test to run.
NOTHING_TO_SCORE = 5


class Stop(Exception):
    """Ends the run early with an exit code and a reason for the summary."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def parse_args(argv=None) -> argparse.Namespace:
    env = os.environ.get
    p = argparse.ArgumentParser(
        description="One evaluation run: check LexChat, gather, score, summarise.",
        epilog="Exit codes: 0 completed, 1 failed, 2 stopped over money, 3 already running.",
    )
    p.add_argument(
        "--questions",
        type=Path,
        default=Path(env("EVAL_QUESTIONS") or HERE / "data" / "questions_new.json"),
        help="Question file (EVAL_QUESTIONS; default questions_new.json)",
    )
    p.add_argument(
        "--question-id",
        type=int,
        nargs="+",
        default=[int(i) for i in (env("EVAL_QUESTION_IDS") or "").split()] or None,
        help="Only these questions (EVAL_QUESTION_IDS, space separated; default all)",
    )
    p.add_argument(
        "--chat-mode",
        choices=("conversational", "deep_research", "all"),
        default=env("EVAL_CHAT_MODE") or "conversational",
        help="Which questions by mode (EVAL_CHAT_MODE; default conversational)",
    )
    p.add_argument(
        "--repeats",
        type=int,
        default=int(env("EVAL_REPEATS") or 2),
        help="Answers per question (EVAL_REPEATS; default 2, so Consistency can score)",
    )
    p.add_argument(
        "--metrics",
        nargs="+",
        default=(env("EVAL_METRICS") or "all").split(),
        help="Metrics to score, or 'all' (EVAL_METRICS; default all)",
    )
    p.add_argument(
        "--budget",
        type=float,
        default=float(env("EVAL_BUDGET_USD") or 10),
        help="Most to spend on LexChat today, in USD (EVAL_BUDGET_USD; default 10)",
    )
    p.add_argument(
        "--threads",
        type=int,
        default=int(env("EVAL_THREADS") or 2),
        help="Questions in flight at once (EVAL_THREADS; default 2, LexChat runs 5 for everyone)",
    )
    p.add_argument(
        "--first-run",
        action="store_true",
        help="Run although there is no recorded cost to estimate from (the budget still stops it)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be gathered and its expected cost, then stop",
    )
    instead = p.add_mutually_exclusive_group()
    instead.add_argument(
        "--check",
        action="store_true",
        help="Instead of a run: sign in to LexChat and print its build and models",
    )
    instead.add_argument(
        "--backup",
        action="store_true",
        help=f"Instead of a run: copy the database to LEX_EVAL_BACKUP_DIR, keeping the newest {BACKUPS_KEPT}",
    )
    args = p.parse_args(argv)
    unknown = set(args.metrics) - set(METRIC_FILES) - {"all"}
    if unknown:
        p.error(f"unknown metric(s): {', '.join(sorted(unknown))}")
    if args.repeats < 1:
        p.error("--repeats must be at least 1")
    return args


def is_deep(question: dict) -> bool:
    return question.get("chat_mode") == "deep_research"


def select_questions(
    path: Path, ids: Optional[list[int]], chat_mode: str
) -> list[dict]:
    """The questions in *path* this run gathers, by id and by mode."""
    questions = json.loads(path.read_text())
    if ids:
        missing = set(ids) - {q["id"] for q in questions}
        if missing:
            raise Stop(FAILED, f"no question with id(s) {sorted(missing)} in {path}")
        questions = [q for q in questions if q["id"] in ids]
    if chat_mode != "all":
        questions = [
            q for q in questions if is_deep(q) == (chat_mode == "deep_research")
        ]
    return questions


# ---------------------------------------------------------------------------
# Reading the database (opened and closed around each query, because a gather
# or scoring subprocess needs the write lock in between)
# ---------------------------------------------------------------------------


def _query(sql: str, params=()) -> list[tuple]:
    if not DEFAULT_DB.exists():
        return []
    conn = duckdb.connect(str(DEFAULT_DB), read_only=True)
    try:
        return conn.execute(sql, list(params)).fetchall()
    except duckdb.CatalogException:
        return []  # a table not created yet: nothing recorded
    finally:
        conn.close()


def london_midnight_utc(now: datetime) -> str:
    """Today's start in London, as the UTC ISO string responses are stamped with."""
    local = now.astimezone(LONDON).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).isoformat()


def spent_today(now: datetime) -> float:
    """LexChat cost of every answer recorded since London midnight."""
    rows = _query(
        "SELECT COALESCE(SUM(total_cost_usd), 0) FROM responses WHERE timestamp >= ?",
        [london_midnight_utc(now)],
    )
    return float(rows[0][0]) if rows else 0.0


def expected_cost(model: str, questions: list[dict], repeats: int) -> Optional[float]:
    """Mean recorded cost per answer for this model and mode, times the answers.

    None when the model has no recorded cost in a mode the run needs.
    """
    rows = _query(
        """SELECT chat_mode = 'deep_research', AVG(total_cost_usd) FROM responses
           WHERE llm_name = ? AND NOT is_error AND total_cost_usd IS NOT NULL
           GROUP BY 1""",
        [model],
    )
    mean = {bool(deep): cost for deep, cost in rows}
    total = 0.0
    for deep in {is_deep(q) for q in questions}:
        if deep not in mean:
            return None
        total += mean[deep] * sum(is_deep(q) == deep for q in questions) * repeats
    return total


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@contextmanager
def run_lock():
    """Held for the whole run, so two runs never overlap."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop(LOCKED, "another run is in progress") from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def check_lexchat() -> dict:
    """Sign in and read what this run will be evaluating."""
    try:
        model, provider = get_active_model()
        if not model:
            raise Stop(
                FAILED, "LexChat has no active model; set one in its Admin Portal"
            )
        summary_model, _ = get_summarisation_model()
        build = get_lexchat_build()
    except (SignInError, ConfigError) as e:
        raise Stop(FAILED, str(e)) from None
    except httpx.HTTPError as e:
        raise Stop(FAILED, f"LexChat answered with an error: {e}") from None
    return {
        "model": model,
        "provider": provider,
        "summarisation_model": summary_model,
        "build": build,
    }


def check_budget(args, lexchat: dict, questions: list[dict], now: datetime) -> None:
    spent = spent_today(now)
    expected = expected_cost(lexchat["model"], questions, args.repeats)
    shown = "unknown" if expected is None else f"${expected:.2f}"
    print(
        f"Budget: ${args.budget:.2f} a day; spent today ${spent:.2f}; this run {shown}"
    )
    if expected is None and not args.first_run:
        raise Stop(
            MONEY,
            f"no recorded cost for {lexchat['model']} in this mode to estimate from; "
            "start it with --first-run (the budget still stops it)",
        )
    if expected is not None and spent + expected > args.budget:
        raise Stop(
            MONEY,
            f"expected ${expected:.2f} on top of ${spent:.2f} spent today "
            f"would exceed the ${args.budget:.2f} budget",
        )


def gather(
    args, questions, label, deployment_file, experiment_id, now
) -> tuple[Optional[str], int]:
    """One pass over *questions*. Returns the experiment id and the gather's exit code."""
    remaining = args.budget - spent_today(now)
    if remaining <= 0:
        return experiment_id, MONEY
    cmd = [
        sys.executable,
        "-m",
        "lex_eval.gather_responses",
        "--questions",
        str(args.questions),
        "--question-id",
        *[str(q["id"]) for q in questions],
        "--threads",
        str(args.threads),
        "--label",
        label,
        "--deployment-config",
        str(deployment_file),
        "--budget-usd",
        f"{remaining:.2f}",
    ]
    if experiment_id:
        cmd += ["--experiment-id", experiment_id]
    rc = subprocess.run(cmd, cwd=HERE.parent).returncode
    if not experiment_id:
        rows = _query(
            "SELECT id FROM experiments WHERE label = ? ORDER BY created_at DESC LIMIT 1",
            [label],
        )
        experiment_id = rows[0][0] if rows else None
    return experiment_id, rc


def score(experiment_id: str, metrics: list[str]) -> int:
    cmd = [sys.executable, str(HERE / "run_evals.py"), "--experiment", experiment_id]
    if "all" not in metrics:
        cmd += ["--metrics", *metrics]
    return subprocess.run(cmd, cwd=HERE.parent).returncode


def summary(experiment_id: Optional[str], started: str) -> dict:
    """Counts for the summary line: answers gathered and checks scored by this run."""
    out = {
        "answers": 0,
        "errors": 0,
        "cost": 0.0,
        "passed": 0,
        "checks": 0,
        "tokens": 0,
    }
    if not experiment_id:
        return out
    ids = (
        "SELECT response_id FROM response_runs rr JOIN responses r ON r.id = rr.response_id "
        "WHERE rr.experiment_id = ? AND r.timestamp >= ?"
    )
    rows = _query(
        f"""SELECT COUNT(*), COALESCE(SUM(is_error::INT), 0),
                   COALESCE(SUM(total_cost_usd), 0)
            FROM responses WHERE id IN ({ids})""",
        [experiment_id, started],
    )
    if rows:
        out["answers"], out["errors"], out["cost"] = rows[0]
    for metric in METRIC_FILES:
        rows = _query(
            f"""SELECT COUNT(*), COALESCE(SUM(passed::INT), 0),
                       COALESCE(SUM(judge_tokens), 0)
                FROM eval_{metric}
                WHERE measured AND run_at >= ? AND response_id IN ({ids})""",
            [started, experiment_id, started],
        )
        if rows:
            out["checks"] += rows[0][0]
            out["passed"] += rows[0][1]
            out["tokens"] += rows[0][2]
    return out


def summary_line(
    now: datetime, outcome: str, lexchat: dict, counts: dict, experiment_id
) -> str:
    """One line: when, how it ended, what was evaluated and what was found."""
    parts = [f"{now.astimezone(LONDON):%Y-%m-%d %H:%M} {outcome}"]
    if lexchat:
        parts.append(f"{lexchat['build']} {lexchat['model']}")
    if experiment_id:
        parts.append(
            f"{counts['answers']} answers, {counts['errors']} errors, "
            f"LexChat ${counts['cost']:.2f}, judge {counts['tokens']:,} tokens, "
            f"{counts['passed']} of {counts['checks']} checks passed"
        )
        parts.append(f"experiment {experiment_id[:8]}")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run(args) -> tuple[int, str]:
    """The whole run. Returns the exit code and the summary line."""
    now = datetime.now(timezone.utc)
    started = now.isoformat()
    lexchat: dict = {}
    experiment_id = None
    code, outcome = OK, "ok"
    try:
        with nullcontext() if args.dry_run else run_lock():
            questions = select_questions(
                args.questions, args.question_id, args.chat_mode
            )
            if not questions:
                raise Stop(
                    FAILED,
                    f"no {args.chat_mode} questions selected in {args.questions}",
                )
            lexchat = check_lexchat()
            print(
                f"LexChat {lexchat['build']}: model {lexchat['model']}, summarisation "
                f"model {lexchat['summarisation_model'] or 'unknown'}"
            )
            print(
                f"Questions: {len(questions)} from {args.questions.name}, "
                f"{args.repeats} answer(s) each, metrics {' '.join(args.metrics)}"
            )
            check_budget(args, lexchat, questions, now)
            if args.dry_run:
                return OK, "dry run: nothing gathered"

            experiment_id, code, outcome = gather_all(args, questions, lexchat, now)
            scored = score(experiment_id, args.metrics)
            # A money stop can leave nothing to score. Otherwise that is a failure:
            # every answer errored, or pytest found no tests.
            refused = code == MONEY and scored == NOTHING_TO_SCORE
            if scored not in (0, 1) and not refused:
                raise Stop(FAILED, "scoring did not complete")
    except Stop as stop:
        code = stop.code
        outcome = f"{'failed' if code == FAILED else 'stopped'}: {stop}"
        if code == LOCKED:
            return code, outcome
    counts = summary(experiment_id, started)
    return code, summary_line(now, outcome, lexchat, counts, experiment_id)


def gather_all(args, questions, lexchat, now) -> tuple[str, int, str]:
    """Every pass into one new experiment. Returns its id, exit code and outcome."""
    label = f"{now.astimezone(LONDON):%Y-%m-%d %H:%M} {lexchat['model']}"
    snapshot = {
        "recorded_by": "eval_run, read from the server at gather time",
        "server_reported": {
            "build": lexchat["build"],
            "model": lexchat["model"],
            "summarisation_model": lexchat["summarisation_model"],
            "source": "GET /api/version and /api/system/models",
        },
    }
    experiment_id, code, outcome = None, OK, "ok"
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(snapshot, fh)
    try:
        for _ in range(args.repeats):
            experiment_id, rc = gather(
                args, questions, label, fh.name, experiment_id, now
            )
            if rc == MONEY:
                code, outcome = (
                    MONEY,
                    "stopped: the budget or a LexChat cap was reached",
                )
                break
            if rc != OK:
                raise Stop(FAILED, f"the gather exited with code {rc}")
    finally:
        os.unlink(fh.name)
    if not experiment_id:
        raise Stop(
            code or FAILED, outcome if code else "the gather recorded no experiment"
        )
    return experiment_id, code, outcome


def check_openrouter() -> str:
    """Ask OpenRouter about the judge's key, which costs nothing."""
    # Imported here: it builds the judge's client, which only --check needs.
    from lex_eval.utils.judge import _judge

    if _judge is None:
        return "OpenRouter not checked (no OPENROUTER_API_KEY)"
    try:
        client = _judge._client.with_options(max_retries=0, timeout=20)
        client.get("/key", cast_to=object)
    except AuthenticationError:
        raise Stop(
            FAILED, "OpenRouter refused the key (HTTP 401). Check OPENROUTER_API_KEY."
        ) from None
    except OpenAIError as e:
        raise Stop(
            FAILED,
            f"Cannot use OpenRouter ({e.__cause__ or e}). Check that openrouter.ai "
            "is reachable from here, and the proxy and certificate settings.",
        ) from None
    return "OpenRouter accepted the key"


def check() -> tuple[int, str]:
    """Say what LexChat is running and that the judge is reachable, or why not."""
    try:
        lexchat = check_lexchat()
        openrouter = check_openrouter()
    except Stop as stop:
        return stop.code, f"failed: {stop}"
    return OK, (
        f"ok: LexChat {lexchat['build']}, model {lexchat['model']}, summarisation "
        f"model {lexchat['summarisation_model'] or 'unknown'}; {openrouter}"
    )


def backup(now: Optional[datetime] = None) -> tuple[int, str]:
    """Copy the database while no run is writing to it; keep the newest copies."""
    now = now or datetime.now(timezone.utc)
    if not DEFAULT_DB.exists():
        return FAILED, f"failed: no database at {DEFAULT_DB}"
    try:
        with run_lock():
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            target = BACKUP_DIR / f"responses-{now.astimezone(LONDON):%Y%m%d-%H%M%S}.db"
            shutil.copy2(DEFAULT_DB, target)
            wal = DEFAULT_DB.with_name(DEFAULT_DB.name + ".wal")
            if wal.exists():
                shutil.copy2(wal, target.with_name(target.name + ".wal"))
    except Stop as stop:
        return stop.code, f"stopped: {stop}"
    for old in sorted(BACKUP_DIR.glob("responses-*.db"))[:-BACKUPS_KEPT]:
        old.unlink()
        old.with_name(old.name + ".wal").unlink(missing_ok=True)
    return OK, f"ok: backed up to {target}"


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.check or args.backup:
        code, line = check() if args.check else backup()
        print(line)
        return code
    code, line = run(args)
    print(line)
    if not args.dry_run and code != LOCKED:
        RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(RUN_LOG, "a") as fh:
            fh.write(line + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
