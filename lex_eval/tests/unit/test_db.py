"""
Unit tests for ``lex_eval.utils.db``, DuckDB storage layer.

Tests focus on the ``init_db`` migration exception handling:
  - CatalogException (column already exists) → logged at DEBUG, skipped
  - Unexpected exceptions (syntax error, etc.) → logged at WARNING, not
    silently swallowed
"""

import json
import logging

import duckdb
import pytest

from lex_eval.utils.db import (
    clear_eval_results,
    covered_response_ids,
    init_db,
    init_eval_table,
    insert_eval_result,
    load_eval_results,
    _CREATE_TABLE,
    _eval_table_name,
)

# Mark every test in this module as a unit test (fast, offline, no LLM).
pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helper: wrapper connection for simulating failures
# ---------------------------------------------------------------------------


class _FailingConnection:
    """Wrapper around a DuckDB connection that raises a custom exception
    for the first ALTER TABLE statement matching a pattern.

    This is needed because DuckDB's C extension connection has read-only
    attributes, so ``unittest.mock.patch.object`` cannot be used directly.
    """

    def __init__(self, conn, fail_on_alter=True, exception=None):
        self._conn = conn
        self._fail_on_alter = fail_on_alter
        self._exception = exception
        self._already_failed = False

    def execute(self, stmt, *args, **kwargs):
        if (
            self._fail_on_alter
            and not self._already_failed
            and "ALTER TABLE" in str(stmt)
            and self._exception is not None
        ):
            self._already_failed = True
            raise self._exception
        return self._conn.execute(stmt, *args, **kwargs)

    def close(self):
        self._conn.close()


# ---------------------------------------------------------------------------
# Tests, init_db migration exception handling
# ---------------------------------------------------------------------------


class TestInitDbMigrationHandling:
    """Test that init_db distinguishes expected vs unexpected migration errors."""

    def test_catalog_exception_logged_at_debug(self, caplog):
        """CatalogException (column already exists) should be logged at DEBUG.

        When the full table already exists (created by _CREATE_TABLE), the
        migration statements will raise CatalogException because the columns
        already exist. These should be logged at DEBUG, and NO WARNING logs
        should appear (regression test for the NOT NULL constraint issue).
        """
        conn = duckdb.connect(":memory:")
        # Create the full table schema (same as init_db would on a fresh DB)
        conn.execute(_CREATE_TABLE)

        # Run init_db again, migrations will try to add columns that
        # already exist, raising CatalogException
        with caplog.at_level(logging.DEBUG, logger="lex_eval.utils.db"):
            init_db(conn)

        # Should have DEBUG logs about migration skipped (for CatalogException)
        debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
        skipped_msgs = [r for r in debug_msgs if "Migration skipped" in r.message]
        assert len(skipped_msgs) > 0, (
            "Expected at least one DEBUG 'Migration skipped' log for "
            "CatalogException (column already exists)"
        )

        # Should NOT have any WARNING logs, all migration statements should
        # either succeed or raise CatalogException (column already exists).
        # This is a regression test for the NOT NULL constraint issue where
        # ParserException was raised instead of CatalogException.
        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_msgs) == 0, (
            f"Expected no WARNING logs on existing database, but got: "
            f"{[r.message for r in warning_msgs]}"
        )

        conn.close()

    def test_unexpected_exception_logged_at_warning(self, caplog):
        """Non-CatalogException errors should be logged at WARNING, not DEBUG."""
        conn = duckdb.connect(":memory:")
        conn.execute(_CREATE_TABLE)

        # Wrap the connection so the first ALTER TABLE raises a
        # non-CatalogException (ParserException simulates a syntax error)
        failing_conn = _FailingConnection(
            conn,
            fail_on_alter=True,
            exception=duckdb.ParserException("Simulated syntax error"),
        )

        with caplog.at_level(logging.DEBUG, logger="lex_eval.utils.db"):
            init_db(failing_conn)

        # Should have a WARNING log about migration failure
        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        failed_msgs = [r for r in warning_msgs if "Migration failed" in r.message]
        assert len(failed_msgs) > 0, (
            "Expected at least one WARNING 'Migration failed' log for "
            "non-CatalogException"
        )

        # The warning should include exc_info for diagnostics
        assert failed_msgs[0].exc_info is not None

        conn.close()

    def test_migration_continues_after_unexpected_error(self, caplog):
        """init_db should continue applying remaining migrations after a failure."""
        conn = duckdb.connect(":memory:")
        conn.execute(_CREATE_TABLE)

        # Wrap so only the first ALTER fails; subsequent ones should pass
        # through to the real connection (which will raise CatalogException
        # since columns already exist, logged at DEBUG)
        failing_conn = _FailingConnection(
            conn,
            fail_on_alter=True,
            exception=duckdb.ParserException("Simulated syntax error"),
        )

        with caplog.at_level(logging.DEBUG, logger="lex_eval.utils.db"):
            init_db(failing_conn)

        # Should have both a WARNING (for the simulated failure) and
        # DEBUG logs (for subsequent CatalogException "already exists" cases)
        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]

        assert any("Migration failed" in r.message for r in warning_msgs)
        assert any(
            "Migration skipped" in r.message for r in debug_msgs
        ), "Subsequent migrations should have continued after the first failure"

        conn.close()


# ---------------------------------------------------------------------------
# Tests, per-metric eval tables
# ---------------------------------------------------------------------------


def _sample_record(**overrides):
    record = {
        "response_id": 1,
        "llm_name": "test-llm",
        "question_id": 1,
        "question": "What does section 6 say?",
        "score": 0.8,
        "threshold": 0.6,
        "passed": True,
        "reason": "States 4 of 5 reference points.",
        "error": "",
        "tools_used": ["search_legislation"],
        "judge_llm": None,
        "judge_tokens": None,
    }
    record.update(overrides)
    return record


class TestEvalTableName:
    def test_rejects_invalid_metric_name(self):
        with pytest.raises(ValueError):
            _eval_table_name("tool_usage; DROP TABLE responses")

    def test_accepts_snake_case_metric_name(self):
        assert _eval_table_name("response_groundedness") == "eval_response_groundedness"


class TestEvalTableRoundTrip:
    """init_eval_table / insert_eval_result / clear_eval_results / covered_response_ids
    against an in-memory connection (these take a connection directly, unlike
    load_eval_results which opens its own file-based connection)."""

    def test_insert_and_covered_response_ids(self):
        conn = duckdb.connect(":memory:")
        init_eval_table(conn, "tool_usage")

        insert_eval_result(conn, "tool_usage", _sample_record(response_id=1))
        insert_eval_result(conn, "tool_usage", _sample_record(response_id=2))

        assert covered_response_ids(conn, "tool_usage") == {1, 2}
        conn.close()

    def test_judge_metadata_stored_for_judge_metrics(self):
        conn = duckdb.connect(":memory:")
        init_eval_table(conn, "response_groundedness")
        insert_eval_result(
            conn,
            "response_groundedness",
            _sample_record(judge_llm="openai/gpt-4o", judge_tokens=1234),
        )
        row = conn.execute(
            "SELECT judge_llm, judge_tokens, run_at FROM eval_response_groundedness"
        ).fetchone()
        assert row[0] == "openai/gpt-4o"
        assert row[1] == 1234
        assert row[2]  # run_at is populated
        conn.close()

    def test_clear_eval_results_only_affects_its_own_table(self):
        conn = duckdb.connect(":memory:")
        init_eval_table(conn, "tool_usage")
        init_eval_table(conn, "consistency")
        insert_eval_result(conn, "tool_usage", _sample_record())
        insert_eval_result(conn, "consistency", _sample_record())

        clear_eval_results(conn, "tool_usage")

        assert covered_response_ids(conn, "tool_usage") == set()
        assert covered_response_ids(conn, "consistency") == {1}
        conn.close()

    def test_clear_eval_results_with_llm_only_deletes_that_llm(self):
        conn = duckdb.connect(":memory:")
        init_eval_table(conn, "tool_usage")
        insert_eval_result(
            conn, "tool_usage", _sample_record(response_id=1, llm_name="model-a")
        )
        insert_eval_result(
            conn, "tool_usage", _sample_record(response_id=2, llm_name="model-b")
        )

        clear_eval_results(conn, "tool_usage", llm="model-a")

        assert covered_response_ids(conn, "tool_usage") == {2}
        conn.close()

    def test_clear_eval_results_with_llm_matches_overlapping_names(self):
        """run_evals.py --overwrite --llm selects re-run tests with pytest's
        substring ``-k``, so clearing must use the same substring match, or
        an overlapping name like "gpt-4" vs "gpt-4o" gets re-run without its
        old rows cleared, leaving duplicates."""
        conn = duckdb.connect(":memory:")
        init_eval_table(conn, "tool_usage")
        insert_eval_result(
            conn, "tool_usage", _sample_record(response_id=1, llm_name="gpt-4")
        )
        insert_eval_result(
            conn, "tool_usage", _sample_record(response_id=2, llm_name="gpt-4o")
        )

        clear_eval_results(conn, "tool_usage", llm="gpt-4")

        assert covered_response_ids(conn, "tool_usage") == set()
        conn.close()


class TestLoadEvalResults:
    def test_round_trip_via_file(self, tmp_path):
        from lex_eval.utils.db import get_connection

        db_path = tmp_path / "scratch.db"
        conn = get_connection(db_path)
        init_eval_table(conn, "citation_agreement")
        insert_eval_result(
            conn, "citation_agreement", _sample_record(response_id=1, score=0.5)
        )
        conn.commit()
        conn.close()

        results = load_eval_results(db_path, metric="citation_agreement")
        assert len(results) == 1
        assert results[0]["response_id"] == 1
        assert results[0]["score"] == 0.5
        assert results[0]["judge_llm"] is None

    def test_missing_table_returns_empty_list_read_only(self, tmp_path):
        from lex_eval.utils.db import get_connection

        db_path = tmp_path / "scratch.db"
        # Ensure the file exists but the metric's table doesn't.
        get_connection(db_path).close()

        assert load_eval_results(db_path, metric="genuine_gap", read_only=True) == []

    def test_requires_metric(self):
        with pytest.raises(ValueError):
            load_eval_results(metric=None)


class TestTurnCapHaltColumns:
    """max_turns_halted / react_turns_max round-trip, and 0 survives as 0."""

    def test_round_trip_keeps_zero_distinct_from_null(self, tmp_path):
        from lex_eval.utils.db import (
            get_connection,
            init_db,
            insert_response,
            load_records,
        )

        db = tmp_path / "r.db"
        conn = get_connection(db)
        init_db(conn)
        insert_response(
            conn,
            {
                "question_id": 1,
                "question": "q",
                "llm_name": "m",
                "timestamp": "t",
                "chat_mode": "deep_research",
                "max_turns_halted": 0,
                "react_turns_max": 12,
            },
        )
        insert_response(
            conn,
            {
                "question_id": 2,
                "question": "q",
                "llm_name": "m",
                "timestamp": "t",
                "chat_mode": "deep_research",
                "max_turns_halted": 1,
                "react_turns_max": 20,
            },
        )
        insert_response(
            conn,
            {
                "question_id": 3,
                "question": "q",
                "llm_name": "m",
                "timestamp": "t",
            },
        )
        conn.commit()
        conn.close()

        by_q = {r["question_id"]: r for r in load_records(path=db)}
        assert by_q[1]["max_turns_halted"] == 0
        assert by_q[2]["max_turns_halted"] == 1
        assert by_q[2]["react_turns_max"] == 20
        # Not reported by the server at all stays NULL, not 0.
        assert by_q[3]["max_turns_halted"] is None


class TestCleanIncompleteResponsesSparesClarification:
    """A needs_clarification row has empty actual_output/retrieval_context by
    design, the same shape clean_incomplete_responses() otherwise treats as
    an incomplete capture, so it must be excluded from cleanup."""

    def test_clarification_row_is_not_deleted(self, tmp_path):
        from lex_eval.utils.db import (
            clean_incomplete_responses,
            get_connection,
            init_db,
            insert_response,
        )

        db = tmp_path / "r.db"
        conn = get_connection(db)
        init_db(conn)
        insert_response(
            conn,
            {
                "question_id": 1,
                "question": "q",
                "llm_name": "m",
                "timestamp": "t",
                "chat_mode": "deep_research",
                "needs_clarification": True,
                "clarification_question": "Which tax year?",
            },
        )
        insert_response(
            conn,
            {
                "question_id": 2,
                "question": "q",
                "llm_name": "m",
                "timestamp": "t",
            },
        )
        conn.commit()
        conn.close()

        deleted = clean_incomplete_responses(path=db)

        assert deleted == 1
        conn = get_connection(db, read_only=True)
        remaining = conn.execute("SELECT question_id FROM responses").fetchall()
        conn.close()
        assert remaining == [(1,)]


class TestCleanIncompleteResponsesCascades:
    """Deleting a response without clearing its eval_<metric> rows leaves rows
    behind pointing at an id that no longer resolves, which silently corrupts
    the eval tables of any database that has already been scored."""

    def test_eval_rows_go_with_the_response(self, tmp_path):
        from lex_eval.utils.db import (
            clean_incomplete_responses,
            get_connection,
            init_db,
            init_eval_table,
            insert_eval_result,
            insert_response,
        )

        db = tmp_path / "r.db"
        conn = get_connection(db)
        init_db(conn)
        init_eval_table(conn, "tool_usage")
        # Incomplete: no retrieval context, so cleanup takes it.
        insert_response(
            conn,
            {
                "question_id": 1,
                "question": "q",
                "llm_name": "m",
                "timestamp": "t",
                "actual_output": "Could you narrow this down?",
            },
        )
        # Complete, so it stays, and so must its eval row.
        insert_response(
            conn,
            {
                "question_id": 2,
                "question": "q",
                "llm_name": "m",
                "timestamp": "t",
                "actual_output": "an answer",
                "retrieval_context": ["s.1 text"],
            },
        )
        for response_id in (1, 2):
            insert_eval_result(
                conn,
                "tool_usage",
                {
                    "response_id": response_id,
                    "llm_name": "m",
                    "question_id": response_id,
                    "question": "q",
                    "score": 1.0,
                    "threshold": 1.0,
                    "passed": True,
                },
            )
        conn.commit()
        conn.close()

        assert clean_incomplete_responses(path=db) == 1

        conn = get_connection(db, read_only=True)
        scored = conn.execute(
            "SELECT response_id FROM eval_tool_usage ORDER BY response_id"
        ).fetchall()
        conn.close()
        assert scored == [(2,)]

    def test_dry_run_deletes_nothing(self, tmp_path):
        from lex_eval.utils.db import (
            clean_incomplete_responses,
            get_connection,
            init_db,
            init_eval_table,
            insert_eval_result,
            insert_response,
        )

        db = tmp_path / "r.db"
        conn = get_connection(db)
        init_db(conn)
        init_eval_table(conn, "tool_usage")
        insert_response(
            conn,
            {
                "question_id": 1,
                "question": "q",
                "llm_name": "m",
                "timestamp": "t",
                "actual_output": "Could you narrow this down?",
            },
        )
        insert_eval_result(
            conn,
            "tool_usage",
            {
                "response_id": 1,
                "llm_name": "m",
                "question_id": 1,
                "question": "q",
                "score": 1.0,
                "threshold": 1.0,
                "passed": True,
            },
        )
        conn.commit()
        conn.close()

        assert clean_incomplete_responses(path=db, dry_run=True) == 1

        conn = get_connection(db, read_only=True)
        assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM eval_tool_usage").fetchone()[0] == 1
        conn.close()


class TestMakeDeployDbPreservesIds:
    """Eval rows are copied with their original response_id, so a deploy
    copy must keep source response ids intact, even across a gap left by a
    deleted response, or later eval rows point at the wrong response."""

    def test_ids_survive_a_gap(self, tmp_path):
        from lex_eval.reports.data import _connect
        from lex_eval.utils.db import (
            get_connection,
            init_db,
            insert_response,
            make_deploy_db,
        )

        src = tmp_path / "responses.db"
        conn = get_connection(src)
        init_db(conn)
        for i in range(1, 4):
            insert_response(
                conn,
                {
                    "question_id": i,
                    "question": "q",
                    "llm_name": "m",
                    "timestamp": "t",
                },
            )
        conn.commit()
        conn.execute("DELETE FROM responses WHERE id = 2")
        conn.commit()
        conn.close()

        deploy = tmp_path / "deploy"
        make_deploy_db(source_path=src, output_path=deploy)

        with _connect(deploy) as conn:
            ids = {r[0] for r in conn.execute("SELECT id FROM responses").fetchall()}
        assert ids == {1, 3}


class TestMeasuredColumn:
    """A row a metric could not score must never reach a mean.

    Such a row is still written, so the gap stays visible, and carries score
    0.0 because the column is NOT NULL. `measured` is what marks it as a
    placeholder rather than a verdict.
    """

    @staticmethod
    def _row(**over):
        row = {
            "response_id": 1,
            "llm_name": "test-llm",
            "question_id": 1,
            "question": "q?",
            "score": 0.0,
            "threshold": 1.0,
            "passed": False,
            "reason": "",
            "error": "",
            "tools_used": None,
            "judge_llm": None,
            "judge_tokens": None,
        }
        row.update(over)
        return row

    def test_measured_defaults_true_and_round_trips_false(self, tmp_path):
        from lex_eval.utils.db import (
            get_connection,
            init_eval_table,
            insert_eval_result,
            load_eval_results,
        )

        db_path = tmp_path / "scratch.db"
        conn = get_connection(db_path)
        init_eval_table(conn, "tool_usage")
        insert_eval_result(conn, "tool_usage", self._row(score=0.8))
        insert_eval_result(
            conn,
            "tool_usage",
            self._row(
                response_id=2,
                reason="Not deep_research; Step Completion not measured",
                measured=False,
            ),
        )
        conn.commit()
        conn.close()

        rows = load_eval_results(db_path, metric="tool_usage", read_only=True)
        assert [r["measured"] for r in rows] == [True, False]
        scored = [r["score"] for r in rows if r["measured"]]
        assert scored == [0.8], "the placeholder 0.0 must not reach the mean"

    def test_attach_metric_derives_measured_from_reason(self):
        """A metric cannot forget: the reason wording decides, at one place."""
        from lex_eval.utils.collector import attach_metric

        class _Node:
            pass

        class _Req:
            node = _Node()

        record = {"response_id": 1, "llm_name": "l", "question_id": 1, "question": "q"}

        req = _Req()
        attach_metric(
            request=req,
            record=record,
            test_name="step_completion",
            metric_name="Step Completion",
            score=0.0,
            threshold=1.0,
            passed=False,
            reason="Not deep_research; Step Completion not measured",
        )
        assert req.node._metric_data["measured"] is False

        req = _Req()
        attach_metric(
            request=req,
            record=record,
            test_name="step_completion",
            metric_name="Step Completion",
            score=0.0,
            threshold=1.0,
            passed=False,
            reason="Step 2 retrieved text but carried no citation into its report",
        )
        assert (
            req.node._metric_data["measured"] is True
        ), "a real zero is a verdict and must stay in the mean"

    def test_backfill_marks_pre_existing_rows(self, tmp_path):
        from lex_eval.utils.db import (
            backfill_measured_column,
            get_connection,
            init_eval_table,
            insert_eval_result,
            load_eval_results,
        )

        db_path = tmp_path / "scratch.db"
        conn = get_connection(db_path)
        init_eval_table(conn, "plan_coverage")
        # Written the old way: no `measured` argument, so it lands as True.
        insert_eval_result(
            conn,
            "plan_coverage",
            self._row(
                reason="No research plan for this record; Plan Coverage not measured"
            ),
        )
        insert_eval_result(conn, "plan_coverage", self._row(response_id=2, score=0.5))
        conn.commit()
        conn.close()

        assert backfill_measured_column(db_path) == 1
        rows = load_eval_results(db_path, metric="plan_coverage", read_only=True)
        assert [r["measured"] for r in rows] == [False, True]
        assert backfill_measured_column(db_path) == 0, "must be safe to re-run"


class TestBackfillCaseLawContext:
    """Repairing rows gathered before case law results were read correctly."""

    def _audit(self):
        return {
            "type": "audit",
            "schema_version": 1,
            "research_mode": "case_law_only",
            "answer": "a",
            "delegations": [
                {
                    "id": "d1",
                    "brief": "b",
                    "report": "r",
                    "tools": [
                        {
                            "name": "search_case_law",
                            "args": {"query": "detention"},
                            "raw_result": json.dumps(
                                {
                                    "results": [
                                        {
                                            "title": "Burgin v Commission of Police",
                                            "ncn": "[2011] EWHC 1835 (Admin)",
                                            "court": "ewhc/admin",
                                            "date": "2011-07-13",
                                            "url": "https://caselaw.nationalarchives.gov.uk/ewhc/admin/2011/1835",
                                        }
                                    ],
                                    "total": 1,
                                }
                            ),
                            "final_result": "...",
                            "api_calls": [
                                {"url": "u", "response": {"preview": "<feed"}}
                            ],
                        }
                    ],
                }
            ],
        }

    def test_empty_case_law_context_is_repaired(self, tmp_path):
        from lex_eval.utils.db import (
            backfill_retrieval_context,
            get_connection,
            init_db,
            insert_response,
            load_records,
        )

        db = tmp_path / "r.db"
        conn = get_connection(db)
        init_db(conn)
        insert_response(
            conn,
            {
                "question_id": 1,
                "question": "q",
                "llm_name": "m",
                "timestamp": "t",
                "research_mode": "case_law_only",
                "retrieval_context": [],
                "case_law_context": [],
                "audit_json": self._audit(),
            },
        )
        conn.commit()
        conn.close()

        assert backfill_retrieval_context(path=db) == 1

        rec = load_records(path=db)[0]
        assert [c["ncn"] for c in rec["case_law_context"]] == [
            "[2011] EWHC 1835 (Admin)"
        ]
        assert "[2011] EWHC 1835 (Admin)" in " ".join(rec["retrieval_context"])

    def test_running_twice_changes_nothing_the_second_time(self, tmp_path):
        from lex_eval.utils.db import (
            backfill_retrieval_context,
            get_connection,
            init_db,
            insert_response,
        )

        db = tmp_path / "r.db"
        conn = get_connection(db)
        init_db(conn)
        insert_response(
            conn,
            {
                "question_id": 1,
                "question": "q",
                "llm_name": "m",
                "timestamp": "t",
                "research_mode": "case_law_only",
                "audit_json": self._audit(),
            },
        )
        conn.commit()
        conn.close()

        backfill_retrieval_context(path=db)
        assert backfill_retrieval_context(path=db) == 0
