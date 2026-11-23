"""Experiment identity and non-destructive rescoring on temporary databases."""

import pytest

from lex_eval.utils.db import (
    get_connection,
    init_db,
    insert_response,
    insert_eval_result,
    init_eval_table,
    load_records,
    consistency_group_key,
)
from lex_eval.utils.versioning import (
    start_gather,
    link_response,
    start_scoring,
    compatible_ids,
    metric_version,
    _code_only,
)
from lex_eval.reports.data import read_database

pytestmark = pytest.mark.unit


def test_repeats_join_only_the_same_condition(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    init_db(conn)
    config = {
        "model": "glm",
        "questions": [{"id": 1, "question": "q"}],
        "deployment": None,
    }
    exp, run1 = start_gather(conn, label="baseline", config=config, question_ids=[1])
    same, run2 = start_gather(
        conn, label="repeat", config=config, question_ids=[1], experiment_id=exp
    )
    assert exp == same and run1 != run2
    with pytest.raises(ValueError, match="configuration changed"):
        start_gather(
            conn,
            label="changed",
            config={**config, "model": "another"},
            question_ids=[1],
            experiment_id=exp,
        )
    conn.close()


def test_response_and_scoring_history_round_trip(tmp_path):
    path = tmp_path / "test.db"
    conn = get_connection(path)
    init_db(conn)
    exp, gather = start_gather(conn, label="baseline", config={}, question_ids=[1])
    rid = insert_response(
        conn,
        dict(
            question_id=1,
            question="q",
            llm_name="glm",
            timestamp="2026-09-07",
            actual_output="answer",
        ),
    )
    link_response(conn, rid, gather, exp, {"id": 1, "question": "q"})
    init_eval_table(conn, "tool_usage")
    config = {"source_version": "v1", "judge": {"model": "judge"}, "references": {}}
    run = start_scoring(conn, config)
    row = dict(
        response_id=rid,
        question_id=1,
        question="q",
        llm_name="glm",
        score=1.0,
        threshold=1.0,
        passed=True,
        scoring_run_id=run,
        metric_version=metric_version(config, "tool_usage"),
    )
    insert_eval_result(conn, "tool_usage", row)
    assert compatible_ids(conn, "tool_usage", row["metric_version"]) == {rid}
    assert compatible_ids(conn, "tool_usage", "different") == set()
    insert_eval_result(conn, "tool_usage", {**row, "score": 0.0, "passed": False})
    assert conn.execute("SELECT count(*) FROM eval_tool_usage").fetchone()[0] == 2
    conn.close()
    loaded = load_records(path, read_only=True)
    assert loaded[0]["experiment_id"] == exp
    responses, scores = read_database(path, [("tool_usage", "Tool Usage", "")])
    assert responses[0]["gather_run_id"] == gather
    assert scores[0]["scoring_run_id"] == run
    assert scores[0]["scoring_config"] == config


def test_consistency_respects_condition_and_wording():
    base = dict(
        question_id=1,
        question="q",
        llm_name="glm",
        chat_mode="research",
        research_mode="legislation_only",
        experiment_id="baseline",
    )
    original = consistency_group_key(base)
    for field, value in (
        ("experiment_id", "candidate"),
        ("question", "new"),
        ("research_mode", "case_law_only"),
        ("chat_mode", "deep_research"),
    ):
        assert consistency_group_key({**base, field: value}) != original


def test_gather_entrypoint_records_and_rejoins_experiment(tmp_path, monkeypatch):
    import json
    import sys
    from lex_eval import gather_responses as gather
    from lex_eval.utils import db, versioning

    path = tmp_path / "responses.db"
    questions = tmp_path / "questions.json"
    questions.write_text(
        json.dumps([dict(id=1, question="q", chat_mode="conversational")])
    )
    monkeypatch.setattr(gather, "get_connection", lambda _: db.get_connection(path))
    monkeypatch.setattr(gather, "get_active_model", lambda: ("glm", "provider"))
    monkeypatch.setattr(
        gather, "get_summarisation_model", lambda: ("summary", "provider")
    )
    monkeypatch.setattr(gather, "get_lexchat_build", lambda: "build")
    monkeypatch.setattr(versioning, "revision", lambda: "revision")
    monkeypatch.setattr(versioning, "capture_version", lambda: "capture")

    def answer(qid, question, mode, model, summary, retries, chat, *args):
        return dict(
            question_id=qid,
            question=question,
            research_mode=mode,
            llm_name=model,
            summarisation_llm=summary,
            chat_mode=chat,
            actual_output="answer",
            timestamp="2026-09-07",
            attempts=1,
        )

    monkeypatch.setattr(gather, "process_question", answer)
    argv = [
        "gather_responses.py",
        "--questions",
        str(questions),
        "--threads",
        "1",
        "--label",
        "baseline",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    gather.main()
    conn = db.get_connection(path, read_only=True)
    exp, config_json = conn.execute("SELECT id, config FROM experiments").fetchone()
    config = json.loads(config_json)
    assert config["provider"] == "provider"
    assert config["capture_version"] == "capture"
    conn.close()
    monkeypatch.setattr(sys, "argv", [*argv, "--experiment-id", exp])
    gather.main()
    conn = db.get_connection(path, read_only=True)
    assert conn.execute("SELECT count(*) FROM experiments").fetchone()[0] == 1
    assert (
        conn.execute(
            "SELECT count(*) FROM gather_runs WHERE status='finished'"
        ).fetchone()[0]
        == 2
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM response_runs WHERE experiment_id=?", [exp]
        ).fetchone()[0]
        == 2
    )
    conn.close()


def test_comment_and_docstring_edits_keep_the_scoring_version():
    code = 'def f(x):\n    """Old doc."""\n    return x + 1  # note\n'
    reworded = 'def f(x):\n    """New doc."""\n    # moved note\n    return x + 1\n'
    changed = 'def f(x):\n    """Old doc."""\n    return x + 2  # note\n'
    assert _code_only(code) == _code_only(reworded)
    assert _code_only(code) != _code_only(changed)


def test_consistency_version_does_not_depend_on_which_responses_were_scored():
    base = {"source_version": "v1", "judge": {}}
    assert metric_version(
        {**base, "response_ids": [1, 2]}, "consistency"
    ) == metric_version({**base, "response_ids": [3, 4]}, "consistency")
