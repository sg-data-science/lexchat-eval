"""
Unit tests for ``lex_eval.eval_run``, the one-command evaluation run: which
questions it picks, the money checks, the lock, and the exit codes. LexChat,
the gather and the scoring are replaced, so nothing leaves the machine.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import duckdb
import pytest
from httpx2 import ConnectError, MockTransport, Response
from openai import DefaultHttpxClient, OpenAI

from lex_eval import eval_run
from lex_eval.eval_run import FAILED, LOCKED, MONEY, OK, Stop
from lex_eval.gather_responses import _money_stop
from lex_eval.utils import judge

pytestmark = pytest.mark.unit

_QUESTIONS = [
    {"id": 7, "question": "a", "chat_mode": "conversational"},
    {"id": 8, "question": "b", "chat_mode": "deep_research"},
    {"id": 9, "question": "c", "chat_mode": "conversational"},
]
_LEXCHAT = {
    "model": "sonnet",
    "provider": "openrouter",
    "summarisation_model": None,
    "build": "v2026.11.10",
}
_NOW = datetime(2026, 11, 24, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def questions_file(tmp_path):
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(_QUESTIONS))
    return path


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An empty responses table in a scratch database, for the money checks."""
    path = tmp_path / "responses.db"
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE responses (id INTEGER, llm_name TEXT, chat_mode TEXT, "
        "is_error BOOLEAN, total_cost_usd DOUBLE, timestamp TEXT)"
    )
    conn.close()
    monkeypatch.setattr(eval_run, "DEFAULT_DB", path)

    def add(model, mode, cost, when="2026-11-24T01:00:00+00:00", error=False):
        c = duckdb.connect(str(path))
        c.execute(
            "INSERT INTO responses VALUES (1, ?, ?, ?, ?, ?)",
            [model, mode, error, cost, when],
        )
        c.close()

    return add


def _args(questions_file, **overrides):
    defaults = dict(
        questions=questions_file,
        question_id=None,
        chat_mode="conversational",
        repeats=2,
        metrics=["all"],
        budget=10.0,
        threads=2,
        first_run=False,
        dry_run=False,
    )
    return SimpleNamespace(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# Which questions
# ---------------------------------------------------------------------------


class TestSelectQuestions:
    def test_conversational_by_default(self, questions_file):
        picked = eval_run.select_questions(questions_file, None, "conversational")
        assert [q["id"] for q in picked] == [7, 9]

    def test_deep_research_and_all(self, questions_file):
        deep = eval_run.select_questions(questions_file, None, "deep_research")
        assert [q["id"] for q in deep] == [8]
        assert len(eval_run.select_questions(questions_file, None, "all")) == 3

    def test_ids_narrow_within_the_mode(self, questions_file):
        picked = eval_run.select_questions(questions_file, [7, 8], "conversational")
        assert [q["id"] for q in picked] == [7]

    def test_an_unknown_id_fails_the_run(self, questions_file):
        with pytest.raises(Stop) as e:
            eval_run.select_questions(questions_file, [99], "all")
        assert e.value.code == FAILED

    def test_settings_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("EVAL_CHAT_MODE", "all")
        monkeypatch.setenv("EVAL_QUESTION_IDS", "7 9")
        monkeypatch.setenv("EVAL_REPEATS", "3")
        args = eval_run.parse_args([])
        assert (args.chat_mode, args.question_id, args.repeats) == ("all", [7, 9], 3)
        # An option overrides the setting for this run only.
        assert eval_run.parse_args(["--repeats", "1"]).repeats == 1


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


class TestMoney:
    def test_the_day_starts_at_london_midnight(self):
        winter = datetime(2026, 11, 24, 2, 0, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
        assert eval_run.london_midnight_utc(winter) == "2026-11-24T00:00:00+00:00"
        assert eval_run.london_midnight_utc(summer) == "2026-07-23T23:00:00+00:00"

    def test_spent_today_counts_only_today(self, db):
        db("sonnet", "conversational", 1.5)
        db("sonnet", "conversational", 4.0, when="2026-11-23T23:00:00+00:00")
        assert eval_run.spent_today(_NOW) == 1.5

    def test_expected_cost_is_the_mean_per_mode_times_the_answers(self, db):
        db("sonnet", "conversational", 0.1)
        db("sonnet", "conversational", 0.3)
        db("sonnet", "deep_research", 3.0)
        db("sonnet", "deep_research", 99.0, error=True)  # errors are left out
        two_conv_one_deep = [_QUESTIONS[0], _QUESTIONS[1], _QUESTIONS[2]]
        cost = eval_run.expected_cost("sonnet", two_conv_one_deep, repeats=2)
        assert cost == pytest.approx(0.2 * 2 * 2 + 3.0 * 1 * 2)

    def test_no_history_for_a_needed_mode_gives_no_estimate(self, db):
        db("sonnet", "conversational", 0.1)
        assert eval_run.expected_cost("sonnet", [_QUESTIONS[1]], 1) is None
        assert eval_run.expected_cost("other-model", [_QUESTIONS[0]], 1) is None

    def test_a_run_over_the_budget_is_refused(self, db, questions_file):
        db("sonnet", "conversational", 3.0)  # spent today, and the mean
        args = _args(questions_file, budget=10.0)
        with pytest.raises(Stop) as e:  # 3 spent + 2 questions x 2 x $3 = 15
            eval_run.check_budget(args, _LEXCHAT, [_QUESTIONS[0], _QUESTIONS[2]], _NOW)
        assert e.value.code == MONEY

    def test_no_estimate_needs_first_run(self, db, questions_file):
        with pytest.raises(Stop) as e:
            eval_run.check_budget(
                _args(questions_file), _LEXCHAT, [_QUESTIONS[0]], _NOW
            )
        assert e.value.code == MONEY and "--first-run" in str(e.value)
        eval_run.check_budget(
            _args(questions_file, first_run=True), _LEXCHAT, [_QUESTIONS[0]], _NOW
        )

    def test_the_gather_stops_on_a_cap_or_the_budget(self):
        cap = "LexChat spend cap reached: This request has been stopped because ..."
        assert _money_stop(cap, 0.0, None)
        assert _money_stop(None, 5.0, 5.0)
        assert not _money_stop(None, 4.99, 5.0)
        assert not _money_stop("the AI provider returned HTTP 429", 1.0, None)


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def test_a_second_run_cannot_take_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_run, "LOCK_FILE", tmp_path / "eval_run.lock")
    with eval_run.run_lock():
        with pytest.raises(Stop) as e:
            with eval_run.run_lock():
                pass
    assert e.value.code == LOCKED
    with eval_run.run_lock():  # released once the first run ends
        pass


# ---------------------------------------------------------------------------
# The whole run, with LexChat, the gather and the scoring replaced
# ---------------------------------------------------------------------------


class TestRun:
    @pytest.fixture(autouse=True)
    def _replace(self, tmp_path, monkeypatch, db):
        db("sonnet", "conversational", 0.1, when="2026-01-01T00:00:00+00:00")
        monkeypatch.setattr(eval_run, "LOCK_FILE", tmp_path / "eval_run.lock")
        monkeypatch.setattr(eval_run, "check_lexchat", lambda: dict(_LEXCHAT))
        self.gather_codes = [OK, OK]
        self.passes = 0
        self.scored = []

        def gather(args, questions, label, deployment_file, experiment_id, now):
            self.passes += 1
            snapshot = json.loads(open(deployment_file).read())
            assert snapshot["server_reported"]["build"] == "v2026.11.10"
            return "exp-1", self.gather_codes[self.passes - 1]

        monkeypatch.setattr(eval_run, "gather", gather)
        self.score_code = 1
        monkeypatch.setattr(
            eval_run,
            "score",
            lambda exp, metrics: self.scored.append(exp) or self.score_code,
        )

    def test_failed_checks_are_a_completed_run(self, questions_file):
        code, line = eval_run.run(_args(questions_file))
        assert code == OK
        assert self.passes == 2 and self.scored == ["exp-1"]
        assert " ok | v2026.11.10 sonnet | " in line and "experiment exp-1" in line

    def test_money_stops_the_passes_but_what_was_gathered_is_scored(
        self, questions_file
    ):
        self.gather_codes = [MONEY, OK]
        code, line = eval_run.run(_args(questions_file))
        assert code == MONEY
        assert self.passes == 1 and self.scored == ["exp-1"]
        assert "stopped: the budget or a LexChat cap was reached" in line

    def test_every_answer_refused_is_still_a_money_stop(self, questions_file):
        self.gather_codes = [MONEY]
        self.score_code = eval_run.NOTHING_TO_SCORE
        code, line = eval_run.run(_args(questions_file))
        assert code == MONEY
        assert "stopped: the budget or a LexChat cap was reached" in line

    def test_nothing_to_score_without_a_money_stop_is_a_failure(self, questions_file):
        self.score_code = eval_run.NOTHING_TO_SCORE
        code, line = eval_run.run(_args(questions_file))
        assert code == FAILED and "scoring did not complete" in line

    def test_a_broken_gather_fails_without_scoring(self, questions_file):
        self.gather_codes = [FAILED]
        code, line = eval_run.run(_args(questions_file))
        assert code == FAILED and self.scored == []
        assert "failed: the gather exited with code 1" in line

    def test_a_dry_run_gathers_nothing(self, questions_file):
        code, line = eval_run.run(_args(questions_file, dry_run=True))
        assert code == OK and self.passes == 0
        assert line == "dry run: nothing gathered"

    def test_sign_in_failure_is_a_failed_run(self, questions_file, monkeypatch):
        def refuse():
            raise Stop(FAILED, "LexChat refused the user name or password")

        monkeypatch.setattr(eval_run, "check_lexchat", refuse)
        code, line = eval_run.run(_args(questions_file))
        assert code == FAILED and "refused the user name" in line


def test_the_gather_stops_starting_questions_at_its_budget(tmp_path, monkeypatch):
    """Four questions at $3 each, one at a time, with a $2 budget: the first
    answer reaches it. The question already in flight still finishes and is
    stored, the rest are never started, and the gather exits 2."""
    import sys
    import time

    from lex_eval import gather_responses as gather
    from lex_eval.utils import db, versioning

    path = tmp_path / "responses.db"
    questions = tmp_path / "questions.json"
    questions.write_text(
        json.dumps(
            [dict(id=i, question="q", chat_mode="conversational") for i in range(4)]
        )
    )
    monkeypatch.setattr(gather, "get_connection", lambda _: db.get_connection(path))
    monkeypatch.setattr(gather, "get_active_model", lambda: ("m", "p"))
    monkeypatch.setattr(gather, "get_summarisation_model", lambda: (None, None))
    monkeypatch.setattr(gather, "get_lexchat_build", lambda: "build")
    monkeypatch.setattr(versioning, "revision", lambda: "revision")
    monkeypatch.setattr(versioning, "capture_version", lambda: "capture")
    started = []

    def answer(qid, question, mode, model, summary, retries, chat, *args):
        started.append(qid)
        time.sleep(0.3)  # long enough for the gather to see the first answer
        return dict(
            question_id=qid,
            question=question,
            research_mode=mode,
            llm_name=model,
            chat_mode=chat,
            actual_output="answer",
            timestamp="2026-11-24",
            total_cost_usd=3.0,
            attempts=1,
        )

    monkeypatch.setattr(gather, "process_question", answer)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gather",
            "--questions",
            str(questions),
            "--threads",
            "1",
            "--budget-usd",
            "2",
        ],
    )
    with pytest.raises(SystemExit) as e:
        gather.main()
    assert e.value.code == 2
    conn = db.get_connection(path, read_only=True)
    stored = conn.execute("SELECT count(*) FROM responses").fetchone()[0]
    assert conn.execute("SELECT status FROM gather_runs").fetchone()[0] == "finished"
    conn.close()
    assert 2 not in started and 3 not in started
    assert stored == len(started)


class TestBackup:
    @pytest.fixture(autouse=True)
    def _paths(self, tmp_path, monkeypatch):
        self.db = tmp_path / "responses.db"
        self.dir = tmp_path / "backups"
        monkeypatch.setattr(eval_run, "DEFAULT_DB", self.db)
        monkeypatch.setattr(eval_run, "BACKUP_DIR", self.dir)
        monkeypatch.setattr(eval_run, "LOCK_FILE", tmp_path / "eval_run.lock")

    def test_copies_the_database_and_keeps_the_newest_seven(self):
        self.db.write_bytes(b"db")
        for minute in range(9):
            code, line = eval_run.backup(
                datetime(2026, 11, 24, 3, minute, tzinfo=timezone.utc)
            )
            assert code == OK
        kept = sorted(p.name for p in self.dir.iterdir())
        assert len(kept) == 7
        assert kept[0] == "responses-20261124-030200.db"
        assert (self.dir / kept[-1]).read_bytes() == b"db"

    def test_waits_for_no_run_and_says_so(self):
        self.db.write_bytes(b"db")
        with eval_run.run_lock():
            code, line = eval_run.backup()
        assert code == LOCKED and "another run" in line

    def test_no_database_is_a_failure(self):
        code, line = eval_run.backup()
        assert code == FAILED and "no database" in line


def test_check_reports_lexchat_or_why_not(monkeypatch):
    monkeypatch.setattr(eval_run, "check_lexchat", lambda: dict(_LEXCHAT))
    monkeypatch.setattr(eval_run, "check_openrouter", lambda: "OpenRouter ok")
    code, line = eval_run.check()
    assert (
        code == OK and "v2026.11.10" in line and "summarisation model unknown" in line
    )

    def refuse():
        raise Stop(FAILED, "LexChat refused the user name or password")

    monkeypatch.setattr(eval_run, "check_lexchat", refuse)
    code, line = eval_run.check()
    assert code == FAILED and "refused" in line


class TestCheckOpenRouter:
    def _check(self, monkeypatch, handler):
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key="k",
            http_client=DefaultHttpxClient(transport=MockTransport(handler)),
        )
        monkeypatch.setattr(judge, "_judge", SimpleNamespace(_client=client))
        monkeypatch.setattr(eval_run, "check_lexchat", lambda: dict(_LEXCHAT))
        return eval_run.check()

    def test_an_accepted_key(self, monkeypatch):
        seen = []

        def handler(request):
            seen.append((request.method, request.url.path))
            return Response(200, json={"data": {}})

        code, line = self._check(monkeypatch, handler)
        assert code == OK and line.endswith("; OpenRouter accepted the key")
        assert seen == [("GET", "/api/v1/key")]

    def test_a_refused_key(self, monkeypatch):
        code, line = self._check(monkeypatch, lambda r: Response(401, json={}))
        assert code == FAILED and "Check OPENROUTER_API_KEY" in line

    def test_a_blocked_host_is_tried_once(self, monkeypatch):
        calls = []

        def handler(request):
            calls.append(1)
            raise ConnectError("Connection refused")

        code, line = self._check(monkeypatch, handler)
        assert code == FAILED and len(calls) == 1
        assert "Connection refused" in line and "openrouter.ai" in line

    def test_no_key_is_not_a_failure(self, monkeypatch):
        monkeypatch.setattr(judge, "_judge", None)
        monkeypatch.setattr(eval_run, "check_lexchat", lambda: dict(_LEXCHAT))
        code, line = eval_run.check()
        assert code == OK and "OpenRouter not checked" in line
