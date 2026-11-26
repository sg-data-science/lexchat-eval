"""
Unit tests for ``lex_eval.gather_responses.process_question``, the function
that runs a single question through ``audit_capture`` and returns a record dict.

Tests focus on error handling:
  - Comment 2: When actual_output is empty after retries, the specific
    error_message from audit_capture should be preserved (not replaced
    with a generic string).
  - Comment 4: When actual_output is non-empty but audit_capture returned
    is_error=True, the is_error/error_message should be passed through
    (not hardcoded to False/"").
"""

import copy
import json
import re
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

import httpx
import pytest

from lex_eval.gather_responses import process_question

# Mark every test in this module as a unit test (fast, offline, no LLM).
pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class _MockResponse:
    """Simulates an httpx streaming response."""

    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for line in self._lines:
            yield line


class _MockClient:
    """Simulates an httpx.Client with a .stream() context manager."""

    def __init__(self, lines, plan_response=None, plan_statuses=None):
        self._lines = lines
        self._plan_response = plan_response or {
            "needs_clarification": False,
            "plan": {},
        }
        # HTTP statuses the planner returns, one per call. A real planner
        # response always carries a status_code, and gather_responses now reads
        # it to decide whether a 5xx is worth retrying, so the mock has to
        # supply one. Pass a list to simulate a transient failure.
        self._plan_statuses = list(plan_statuses or [200])
        self.plan_calls = 0

    @contextmanager
    def stream(self, method, url, **kwargs):
        yield _MockResponse(self._lines)

    def close(self):
        pass

    def post(self, url, **kwargs):
        # Return a mock response for deep research plan requests
        status = self._plan_statuses[min(self.plan_calls, len(self._plan_statuses) - 1)]
        self.plan_calls += 1
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.json = MagicMock(return_value=self._plan_response)
        # A real response raises on 4xx/5xx, and gather_responses relies on
        # that to turn a dead planner into an error row.
        if status >= 400:
            mock_resp.raise_for_status = MagicMock(
                side_effect=httpx.HTTPStatusError(
                    f"Server error '{status}'",
                    request=httpx.Request("POST", url),
                    response=httpx.Response(status),
                )
            )
        else:
            mock_resp.raise_for_status = MagicMock()
        return mock_resp


def _sse(data_dict):
    """Build an SSE data: ... line from a dict."""
    return f"data: {json.dumps(data_dict)}"


def _sse_lines(*events):
    """Build a list of SSE lines from event dicts, ending with [DONE]."""
    lines = [_sse(evt) for evt in events]
    lines.append("data: [DONE]")
    return lines


# ---------------------------------------------------------------------------
# Audit event fixtures
# ---------------------------------------------------------------------------

AUDIT_SUCCESS = {
    "type": "audit",
    "schema_version": 1,
    "request_id": "ok123",
    "chat_mode": "research",
    "research_mode": "legislation_only",
    "provider": "openrouter",
    "model": "test-model",
    "answer": "This is a successful answer.",
    "delegations": [
        {
            "id": "d1",
            "kind": "delegation",
            "brief": "Research test",
            "report": "**Summary Answer (BLUF):**\nReport.",
            "reformatted": False,
            "error": None,
            "tools": [],
        }
    ],
    "timings": {"total_ms": 5000, "total_cost_usd": 0.01},
    "error": None,
}

# Audit event with an error but also a non-empty answer, tests Comment 4.
# The capture layer returns is_error=True with a non-empty actual_output.
AUDIT_ERROR_WITH_ANSWER = {
    "type": "audit",
    "schema_version": 1,
    "request_id": "err456",
    "chat_mode": "research",
    "research_mode": "legislation_only",
    "provider": "openrouter",
    "model": "test-model",
    "answer": "Partial answer despite error.",
    "delegations": [
        {
            "id": "d1",
            "kind": "delegation",
            "brief": "Research test",
            "report": "",
            "reformatted": False,
            "error": "Worker agent encountered an issue",
            "tools": [],
        }
    ],
    "timings": {"total_ms": 3000, "total_cost_usd": 0.005},
    "error": "Worker agent encountered an issue",
}

# Audit event with empty answer and an error, tests Comment 2.
AUDIT_ERROR_EMPTY_ANSWER = {
    "type": "audit",
    "schema_version": 1,
    "request_id": "empty789",
    "chat_mode": "research",
    "research_mode": "legislation_only",
    "provider": "openrouter",
    "model": "test-model",
    "answer": "",
    "delegations": [
        {
            "id": "d1",
            "kind": "delegation",
            "brief": "Research test",
            "report": "",
            "reformatted": False,
            "error": "Connection reset by peer",
            "tools": [],
        }
    ],
    "timings": {"total_ms": 1000, "total_cost_usd": 0.001},
    "error": "Connection reset by peer",
}


# ---------------------------------------------------------------------------
# Tests, Comment 4: is_error/error_message pass-through on non-empty output
# ---------------------------------------------------------------------------


class TestIsErrorPassThrough:
    """Test that is_error/error_message from audit_capture are passed through."""

    def test_success_path_is_error_false(self):
        """When audit_capture returns is_error=False, record should have is_error=False."""
        lines = _sse_lines(AUDIT_SUCCESS)
        mock_client = _MockClient(lines)

        with patch(
            "lex_eval.gather_responses.get_authenticated_client",
            return_value=mock_client,
        ):
            result = process_question(
                question_id=1,
                question="test question",
                research_mode="legislation_only",
                model_name="test-model",
                summarisation_llm="test-model",
                max_retries=1,
            )

        assert result["is_error"] is False
        assert result["error_message"] == ""
        assert result["actual_output"] == "This is a successful answer."
        # No "error" key should be present for a successful capture
        assert "error" not in result

    def test_error_with_answer_passes_through_is_error(self):
        """When audit_capture returns is_error=True with non-empty output, pass it through."""
        lines = _sse_lines(AUDIT_ERROR_WITH_ANSWER)
        mock_client = _MockClient(lines)

        with patch(
            "lex_eval.gather_responses.get_authenticated_client",
            return_value=mock_client,
        ):
            result = process_question(
                question_id=1,
                question="test question",
                research_mode="legislation_only",
                model_name="test-model",
                summarisation_llm="test-model",
                max_retries=1,
            )

        # is_error and error_message should be passed through from capture_result
        assert result["is_error"] is True
        assert result["error_message"] == "Worker agent encountered an issue"
        # The "error" key should be present so insert_response treats it as an error row
        assert result.get("error") == "Worker agent encountered an issue"
        # actual_output should still be present (from the audit answer)
        assert result["actual_output"] == "Partial answer despite error."


# ---------------------------------------------------------------------------
# Tests, Comment 2: error_message preserved on empty output failure
# ---------------------------------------------------------------------------


class TestErrorMessagePreservedOnFailure:
    """Test that the specific error_message is preserved when actual_output is empty."""

    def test_specific_error_message_preserved(self):
        """When actual_output is empty, the capture's error_message should be used."""
        lines = _sse_lines(AUDIT_ERROR_EMPTY_ANSWER)
        mock_client = _MockClient(lines)

        with patch(
            "lex_eval.gather_responses.get_authenticated_client",
            return_value=mock_client,
        ):
            result = process_question(
                question_id=1,
                question="test question",
                research_mode="legislation_only",
                model_name="test-model",
                summarisation_llm="test-model",
                max_retries=1,
            )

        # The error key should contain the specific message, not the generic one
        assert result["error"] == "Connection reset by peer"
        assert result["error"] != "Empty actual_output after retries"

    def test_generic_fallback_when_no_error_message(self):
        """When capture returns no error_message, fall back to the generic string."""
        # Create an audit event with empty answer and no error
        audit_no_error = {
            "type": "audit",
            "schema_version": 1,
            "request_id": "noerr",
            "chat_mode": "research",
            "research_mode": "legislation_only",
            "provider": "openrouter",
            "model": "test-model",
            "answer": "",
            "delegations": [],
            "timings": {"total_ms": 1000, "total_cost_usd": 0.001},
            "error": None,
        }
        lines = _sse_lines(audit_no_error)
        mock_client = _MockClient(lines)

        with patch(
            "lex_eval.gather_responses.get_authenticated_client",
            return_value=mock_client,
        ):
            result = process_question(
                question_id=1,
                question="test question",
                research_mode="legislation_only",
                model_name="test-model",
                summarisation_llm="test-model",
                max_retries=1,
            )

        # Should fall back to the generic message
        assert result["error"] == "Empty actual_output after retries"


# ---------------------------------------------------------------------------
# Tests, deep_research plan capture (POST /api/research/plan)
# ---------------------------------------------------------------------------


class TestDeepResearchPlanCapture:
    """Test that the plan fetched from /api/research/plan is threaded into
    the result dict returned by process_question, rather than being
    discarded after it's sent back to the server."""

    def test_plan_included_on_success(self):
        """A successful deep_research run should carry the fetched plan."""
        plan = {
            "steps": [
                {"title": "Step 1: Identify the primary legislation"},
                {"title": "Step 2: Locate provisions on powers of direction"},
            ]
        }
        lines = _sse_lines(AUDIT_SUCCESS)
        mock_client = _MockClient(
            lines, plan_response={"needs_clarification": False, "plan": plan}
        )

        with patch(
            "lex_eval.gather_responses.get_authenticated_client",
            return_value=mock_client,
        ):
            result = process_question(
                question_id=1,
                question="test question",
                research_mode="legislation_only",
                model_name="test-model",
                summarisation_llm="test-model",
                max_retries=1,
                chat_mode="deep_research",
            )

        assert result["research_plan"] == plan

    def test_plan_absent_for_research_mode(self):
        """Non-deep_research runs never call /api/research/plan, so the
        result should carry no plan."""
        lines = _sse_lines(AUDIT_SUCCESS)
        mock_client = _MockClient(lines)

        with patch(
            "lex_eval.gather_responses.get_authenticated_client",
            return_value=mock_client,
        ):
            result = process_question(
                question_id=1,
                question="test question",
                research_mode="legislation_only",
                model_name="test-model",
                summarisation_llm="test-model",
                max_retries=1,
                chat_mode="research",
            )

        assert result["research_plan"] is None

    def test_plan_none_when_clarification_needed(self):
        """When the plan endpoint asks for clarification, no usable plan
        exists yet, so the early-return record should carry research_plan=None,
        and the outcome must be a distinct needs_clarification record, not an
        "error" (a model correctly asking a clarifying question is not a
        capture failure, and must not be silently excluded from scoring by
        load_records()'s default WHERE NOT is_error filter)."""
        lines = _sse_lines(AUDIT_SUCCESS)
        mock_client = _MockClient(
            lines,
            plan_response={
                "needs_clarification": True,
                "question": "Which jurisdiction?",
            },
        )

        with patch(
            "lex_eval.gather_responses.get_authenticated_client",
            return_value=mock_client,
        ):
            result = process_question(
                question_id=1,
                question="test question",
                research_mode="legislation_only",
                model_name="test-model",
                summarisation_llm="test-model",
                max_retries=1,
                chat_mode="deep_research",
            )

        assert result["research_plan"] is None
        assert result["needs_clarification"] is True
        assert result["clarification_question"] == "Which jurisdiction?"
        assert "error" not in result


class TestTurnCapHaltReachesTheRecord:
    """process_question builds its record by naming each key explicitly, so a
    field added to audit_capture's output is silently dropped unless it is
    named here too. This covers the whole capture -> record hand-off."""

    def test_halt_counts_reach_the_record(self):
        audit = copy.deepcopy(AUDIT_SUCCESS)
        audit["timings"] = {
            **audit["timings"],
            "max_turns_halted": 1,
            "react_turns_max": 20,
        }

        with patch(
            "lex_eval.gather_responses.get_authenticated_client",
            return_value=_MockClient(_sse_lines(audit)),
        ):
            result = process_question(
                question_id=1,
                question="test question",
                research_mode="legislation_only",
                model_name="test-model",
                summarisation_llm="test-model",
                max_retries=1,
            )

        assert result["max_turns_halted"] == 1
        assert result["react_turns_max"] == 20

    def test_zero_halts_reaches_the_record_as_zero(self):
        audit = copy.deepcopy(AUDIT_SUCCESS)
        audit["timings"] = {
            **audit["timings"],
            "max_turns_halted": 0,
            "react_turns_max": 7,
        }

        with patch(
            "lex_eval.gather_responses.get_authenticated_client",
            return_value=_MockClient(_sse_lines(audit)),
        ):
            result = process_question(
                question_id=1,
                question="test question",
                research_mode="legislation_only",
                model_name="test-model",
                summarisation_llm="test-model",
                max_retries=1,
            )

        assert result["max_turns_halted"] == 0
        assert result["react_turns_max"] == 7


class TestCaptureKeysReachTheRecord:
    """Every field the capture layer derives must survive the gather seam.

    `gather_responses` rebuilds the stored record with an explicit key list
    rather than passing the capture result through, so a field added to
    `audit_capture` is silently dropped unless it is added here too. That
    happened to `delegation_halts` and `empty_completions`: both were derived
    correctly and stored as NULL for every response, and NULL is
    indistinguishable from "nothing halted", so nothing looked wrong until a
    run halted and the column still said nothing.
    """

    # Keys audit_capture returns that the record deliberately does not carry,
    # each for a stated reason.
    NOT_CARRIED = {
        # gather_responses stores the configured summarisation model instead,
        # since the capture cannot see which model was configured.
        "summarisation_llm",
        # A retry signal for gather_responses, not a property of the answer.
        "timed_out",
    }

    def _record_keys(self, source: str) -> set:
        """The keys assigned in gather_responses' success-path result dict."""
        import re

        start = source.index("if (actual_output and not failure_phrase) or refused:")
        end = source.index('"attempts": attempt,', start)
        return set(re.findall(r'"([a-z_]+)":', source[start:end]))

    def test_no_capture_field_is_dropped(self):
        import inspect

        from lex_eval import gather_responses
        from lex_eval.utils.audit_capture import audit_capture as capture_fn

        returned = set(
            re.findall(
                r'^\s+"([a-z_]+)":',
                inspect.getsource(capture_fn).split("return {")[-1],
                re.M,
            )
        )
        carried = self._record_keys(inspect.getsource(gather_responses))
        missing = returned - carried - self.NOT_CARRIED
        assert not missing, (
            f"audit_capture returns {sorted(missing)}, which gather_responses "
            "never writes to the record, so they are stored as NULL"
        )

    def test_the_two_halt_fields_are_carried(self):
        """Named explicitly, because this is the pair that was lost."""
        import inspect

        from lex_eval import gather_responses

        carried = self._record_keys(inspect.getsource(gather_responses))
        assert "delegation_halts" in carried
        assert "empty_completions" in carried


class TestPlannerIsRetried:
    """A transient planner 5xx must not discard the question.

    The planner is a single HTTP call taken before the chat stream, and it used
    to get one attempt regardless of --retries, so a one-off 502 lost the whole
    deep-research run. LexChat reports a provider rate limit here as a bare 502
    with no diagnosis, so from the outside a retryable condition is
    indistinguishable from a permanent one and retrying is the only way to tell.
    Measured 23 September 2026: 5 of 8 deep-research gather failures were a
    planner 502 that then succeeded on a later attempt.
    """

    def _run(self, mock_client, retries=3):
        with (
            patch(
                "lex_eval.gather_responses.get_authenticated_client",
                return_value=mock_client,
            ),
            patch("lex_eval.gather_responses.time.sleep"),
        ):
            return process_question(
                question_id=1,
                question="q",
                model_name="m",
                research_mode="legislation_only",
                summarisation_llm="m",
                max_retries=retries,
                chat_mode="deep_research",
            )

    def test_a_transient_502_is_retried_and_succeeds(self):
        lines = _sse_lines(AUDIT_SUCCESS)
        client = _MockClient(lines, plan_statuses=[502, 502, 200])
        result = self._run(client)
        assert client.plan_calls == 3
        assert not result.get("is_error"), result.get("error_message")

    def test_a_persistent_502_still_fails_after_the_retries(self):
        """It raises to the caller, which is what records the error row."""
        lines = _sse_lines(AUDIT_SUCCESS)
        client = _MockClient(lines, plan_statuses=[502])
        with pytest.raises(httpx.HTTPStatusError):
            self._run(client)
        assert client.plan_calls == 3

    def test_a_4xx_is_not_retried(self):
        """A client error is not transient; retrying only wastes time."""
        lines = _sse_lines(AUDIT_SUCCESS)
        client = _MockClient(lines, plan_statuses=[400])
        with pytest.raises(httpx.HTTPStatusError):
            self._run(client)
        assert client.plan_calls == 1

    def test_success_first_time_makes_one_call(self):
        lines = _sse_lines(AUDIT_SUCCESS)
        client = _MockClient(lines, plan_statuses=[200])
        self._run(client)
        assert client.plan_calls == 1


class TestTimedOutStreamIsRetried:
    """A stream that goes silent past the read timeout is retried.

    It keeps the tokens it had sent, so before this its partial answer was
    stored at once as an error row and never retried (Q16, 12 October 2026).
    """

    class _TimeoutThenOk(_MockClient):
        def __init__(self, lines, timeouts):
            super().__init__(lines)
            self.timeouts = timeouts
            self.stream_calls = 0

        @contextmanager
        def stream(self, method, url, **kwargs):
            self.stream_calls += 1
            if self.stream_calls <= self.timeouts:
                yield _MockResponse(self._partial())
            else:
                yield _MockResponse(self._lines)

        @staticmethod
        def _partial():
            yield _sse({"type": "token", "content": "partial answer"})
            raise httpx.ReadTimeout("timed out")

    def _run(self, client, retries=3):
        with patch(
            "lex_eval.gather_responses.get_authenticated_client",
            return_value=client,
        ):
            return process_question(
                question_id=1,
                question="q",
                model_name="m",
                research_mode="legislation_only",
                summarisation_llm="m",
                max_retries=retries,
            )

    def test_a_timeout_is_retried_and_succeeds(self):
        client = self._TimeoutThenOk(_sse_lines(AUDIT_SUCCESS), timeouts=1)
        result = self._run(client)
        assert client.stream_calls == 2
        assert not result.get("is_error"), result.get("error_message")
        assert result["attempts"] == 2

    def test_a_persistent_timeout_fails_after_the_retries(self):
        client = self._TimeoutThenOk(_sse_lines(AUDIT_SUCCESS), timeouts=99)
        result = self._run(client, retries=2)
        assert client.stream_calls == 2
        assert "stream timed out" in result["error"]


class TestSpendCapRefusalIsNotRetried:
    """LexChat refuses a request over a spending cap before the model call, so a
    retry gets the same refusal. It is stored at once, named as a cap refusal,
    with whatever the request had spent before the cap was reached."""

    _CAP = (
        "This request has been stopped because your daily allowance for the AI "
        "service has been reached. Please try again later, or ask your "
        "administrator if you need more."
    )

    class _CountingClient(_MockClient):
        stream_calls = 0

        def stream(self, method, url, **kwargs):
            self.stream_calls += 1
            return super().stream(method, url, **kwargs)

    def _run(self, client, chat_mode="research"):
        with (
            patch(
                "lex_eval.gather_responses.get_authenticated_client",
                return_value=client,
            ),
            patch("lex_eval.gather_responses.time.sleep"),
        ):
            return process_question(
                question_id=1,
                question="q",
                model_name="m",
                research_mode="legislation_only",
                summarisation_llm="m",
                max_retries=3,
                chat_mode=chat_mode,
            )

    def test_a_refused_answer_is_stored_once_with_its_cost(self):
        refused = {
            **AUDIT_ERROR_EMPTY_ANSWER,
            "error": self._CAP,
            "timings": {"total_ms": 900, "total_cost_usd": 0.42},
        }
        client = self._CountingClient(_sse_lines(refused))
        result = self._run(client)
        assert client.stream_calls == 1
        assert result["error"] == "LexChat spend cap reached: " + self._CAP
        assert result["total_cost_usd"] == 0.42

    def test_a_refused_plan_is_not_retried(self):
        client = _MockClient(
            _sse_lines(AUDIT_SUCCESS),
            plan_response={"detail": self._CAP},
            plan_statuses=[503],
        )
        with pytest.raises(RuntimeError, match="^LexChat spend cap reached: "):
            self._run(client, chat_mode="deep_research")
        assert client.plan_calls == 1
