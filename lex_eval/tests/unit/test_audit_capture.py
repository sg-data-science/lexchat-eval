"""
Unit tests for ``audit_capture``, the SSE stream parser that derives the
result dict from the structured ``audit`` event emitted by LexChat
(commit da3070d+).

These tests use a mock client that simulates the SSE stream.  They do not
require a running LexChat server or a populated database.

Test cases (per implementation plan §4.2):
  - Standard legislation_only trace
  - Deep Research trace (multiple delegations)
  - Failed run (audit carries an error)
  - Missing ``audit`` event → RuntimeError
  - Unknown ``schema_version`` → RuntimeError
"""

import json
from contextlib import contextmanager

import copy

import pytest

from lex_eval.utils.audit_capture import audit_capture

# Mark every test in this module as a unit test (fast, offline, no LLM).
pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Mock SSE client
# ---------------------------------------------------------------------------


class _MockResponse:
    """Simulates an ``httpx`` streaming response."""

    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for line in self._lines:
            yield line


class _MockClient:
    """Simulates an ``httpx.Client`` with a ``.stream()`` context manager."""

    def __init__(self, lines):
        self._lines = lines

    @contextmanager
    def stream(self, method, url, **kwargs):
        yield _MockResponse(self._lines)

    def close(self):
        pass


def _sse(data_dict):
    """Build an SSE ``data: ...`` line from a dict."""
    return f"data: {json.dumps(data_dict)}"


def _sse_lines(*events):
    """Build a list of SSE lines from event dicts, ending with ``[DONE]``."""
    lines = [_sse(evt) for evt in events]
    lines.append("data: [DONE]")
    return lines


# ---------------------------------------------------------------------------
# Fixture audit dicts (per implementation plan §4.1)
# ---------------------------------------------------------------------------

AUDIT_LEGISLATION = {
    "type": "audit",
    "schema_version": 1,
    "request_id": "aabbccdd",
    "chat_mode": "research",
    "research_mode": "legislation_only",
    "provider": "openrouter",
    "model": "openai/gpt-4o",
    "answer": "The Housing Act 1985 requires landlords to...",
    "delegations": [
        {
            "id": "d1d1d1d1",
            "kind": "delegation",
            "step": None,
            "title": None,
            "brief": "Research the right to repair under the Housing Act 1985",
            "report": (
                "**Summary Answer (BLUF):**\n"
                "Landlords must repair.\n"
                "**Detailed Analysis:**\n"
                "Section 11 applies."
            ),
            "reformatted": False,
            "error": None,
            "started_at": 0.5,
            "duration_s": 12.0,
            "tools": [
                {
                    "id": "t1",
                    "name": "search_legislation",
                    "args": {"query": "Housing Act 1985 repair"},
                    "raw_result": "...",
                    "final_result": "...",
                    "summarised": False,
                    "local_cache_hit": False,
                    "memo_hit": False,
                    "api_calls": [
                        {
                            "url": "https://lex.../legislation/search",
                            "method": "POST",
                            "request": {},
                            "status": 200,
                            "response": {
                                "results": [{"title": "Housing Act 1985", "year": 1985}]
                            },
                        }
                    ],
                },
                {
                    "id": "t2",
                    "name": "search_legislation_sections",
                    "args": {
                        "legislation_id": "ukpga/1985/68",
                        "query": "repair",
                    },
                    "raw_result": "...",
                    "final_result": "Section 11: ...",
                    "summarised": False,
                    "local_cache_hit": False,
                    "memo_hit": False,
                    "api_calls": [
                        {
                            "url": "https://lex.../legislation/section/search",
                            "method": "POST",
                            "request": {},
                            "status": 200,
                            "response": [
                                {"title": "Section 11", "content": "The landlord..."}
                            ],
                        }
                    ],
                },
            ],
        }
    ],
    "timings": {"total_ms": 14200, "llm_calls": 3, "total_cost_usd": 0.05},
    "error": None,
}

AUDIT_DEEP_RESEARCH = {
    "type": "audit",
    "schema_version": 1,
    "request_id": "eeff0011",
    "chat_mode": "deep_research",
    "research_mode": "legislation_only",
    "provider": "openrouter",
    "model": "openai/gpt-4o",
    "answer": "Based on multi-step research...",
    "delegations": [
        {
            "id": "d1",
            "kind": "deep_research_step",
            "step": 1,
            "title": "Statutory framework",
            "brief": "Identify the key statutes",
            "report": "**Summary Answer (BLUF):**\nStep 1 report.",
            "reformatted": False,
            "error": None,
            "started_at": 0.5,
            "duration_s": 10.0,
            "tools": [
                {
                    "id": "t1",
                    "name": "search_legislation",
                    "args": {"query": "Housing Act"},
                    "raw_result": "...",
                    "final_result": "...",
                    "summarised": False,
                    "local_cache_hit": False,
                    "memo_hit": False,
                    "api_calls": [],
                },
            ],
        },
        {
            "id": "d2",
            "kind": "deep_research_step",
            "step": 2,
            "title": "Case law analysis",
            "brief": "Identify key cases",
            "report": "**Summary Answer (BLUF):**\nStep 2 report.",
            "reformatted": True,
            "error": None,
            "started_at": 11.0,
            "duration_s": 8.0,
            "tools": [
                {
                    "id": "t2",
                    "name": "search_legislation_sections",
                    "args": {
                        "legislation_id": "ukpga/1985/68",
                        "query": "repair",
                    },
                    "raw_result": "...",
                    "final_result": "Section 11 text",
                    "summarised": True,
                    "local_cache_hit": True,
                    "memo_hit": False,
                    "api_calls": [
                        {
                            "url": "https://lex.../legislation/section/search",
                            "method": "POST",
                            "request": {},
                            "status": 200,
                            "response": [
                                {"title": "Section 11", "content": "The landlord..."}
                            ],
                        }
                    ],
                },
            ],
        },
    ],
    "timings": {"total_ms": 20000, "llm_calls": 5, "total_cost_usd": 0.12},
    "error": None,
}

AUDIT_FAILED = {
    "type": "audit",
    "schema_version": 1,
    "request_id": "failed123",
    "chat_mode": "research",
    "research_mode": "legislation_only",
    "provider": "openrouter",
    "model": "openai/gpt-4o",
    "answer": "",
    "delegations": [
        {
            "id": "d1",
            "kind": "delegation",
            "step": None,
            "title": None,
            "brief": "Research something",
            "report": "",
            "reformatted": False,
            "error": "Worker agent timed out",
            "started_at": 0.5,
            "duration_s": 30.0,
            "tools": [],
        }
    ],
    "timings": {"total_ms": 30000, "llm_calls": 1, "total_cost_usd": 0.01},
    "error": "Worker agent timed out",
}

# Above MAX_AUDIT_SCHEMA_VERSION. Versions 2 to 6 are supported, so an
# unsupported fixture has to sit past the top of that range.
AUDIT_WRONG_SCHEMA = {
    "type": "audit",
    "schema_version": 7,
    "request_id": "wrong456",
    "chat_mode": "research",
    "research_mode": "legislation_only",
    "provider": "openrouter",
    "model": "openai/gpt-4o",
    "answer": "Some answer",
    "delegations": [],
    "timings": {"total_ms": 1000, "llm_calls": 1, "total_cost_usd": 0.01},
    "error": None,
}

# Audit event with a tool entry that has a missing/empty name field.
# The capture layer should use "Worker: unknown" as a placeholder instead
# of producing an ambiguous "Worker: " entry.
AUDIT_MISSING_TOOL_NAME = {
    "type": "audit",
    "schema_version": 1,
    "request_id": "noname789",
    "chat_mode": "research",
    "research_mode": "legislation_only",
    "provider": "openrouter",
    "model": "openai/gpt-4o",
    "answer": "An answer with a tool that has no name.",
    "delegations": [
        {
            "id": "d1",
            "kind": "delegation",
            "step": None,
            "title": None,
            "brief": "Research something",
            "report": "**Summary Answer (BLUF):**\nReport text.",
            "reformatted": False,
            "error": None,
            "started_at": 0.5,
            "duration_s": 5.0,
            "tools": [
                {
                    "id": "t1",
                    "name": "search_legislation",
                    "args": {"query": "test"},
                    "raw_result": "...",
                    "final_result": "...",
                    "summarised": False,
                    "local_cache_hit": False,
                    "memo_hit": False,
                    "api_calls": [],
                },
                {
                    "id": "t2",
                    # Missing "name" field entirely
                    "args": {"query": "no name"},
                    "raw_result": "...",
                    "final_result": "Result from unnamed tool",
                    "summarised": False,
                    "local_cache_hit": False,
                    "memo_hit": False,
                    "api_calls": [],
                },
                {
                    "id": "t3",
                    "name": "",  # Empty string name
                    "args": {"query": "empty name"},
                    "raw_result": "...",
                    "final_result": "Result from empty-named tool",
                    "summarised": False,
                    "local_cache_hit": False,
                    "memo_hit": False,
                    "api_calls": [],
                },
            ],
        }
    ],
    "timings": {"total_ms": 5000, "llm_calls": 2, "total_cost_usd": 0.02},
    "error": None,
}


# ---------------------------------------------------------------------------
# Tests, standard legislation_only
# ---------------------------------------------------------------------------


class TestStandardLegislation:
    """Test the standard legislation_only audit event."""

    def test_actual_output_from_audit_answer(self):
        """actual_output should prefer audit['answer'] over token stream."""
        lines = _sse_lines(
            {"type": "token", "content": "partial..."},
            AUDIT_LEGISLATION,
        )
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["actual_output"] == AUDIT_LEGISLATION["answer"]

    def test_research_output_from_delegation_report(self):
        """research_output should be the delegation's report."""
        lines = _sse_lines(AUDIT_LEGISLATION)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        expected = AUDIT_LEGISLATION["delegations"][0]["report"]
        assert result["research_output"] == expected

    def test_tool_sequence_includes_delegate_and_worker_tools(self):
        """tool_sequence should contain delegate_research and Worker: prefixed tools."""
        lines = _sse_lines(AUDIT_LEGISLATION)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert "delegate_research" in result["tool_sequence"]
        assert "Worker: search_legislation" in result["tool_sequence"]
        assert "Worker: search_legislation_sections" in result["tool_sequence"]

    def test_tools_called_structure(self):
        """tools_called should have delegate_research and Worker: entries."""
        lines = _sse_lines(AUDIT_LEGISLATION)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        names = [t["name"] for t in result["tools_called"]]
        assert "delegate_research" in names
        assert "Worker: search_legislation" in names
        assert "Worker: search_legislation_sections" in names

    def test_retrieval_context_from_sections(self):
        """retrieval_context should include section text from search_legislation_sections."""
        lines = _sse_lines(AUDIT_LEGISLATION)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert len(result["retrieval_context"]) > 0
        ctx_str = " ".join(result["retrieval_context"])
        assert "Section 11" in ctx_str

    def test_retrieval_context_from_legislation_search(self):
        """retrieval_context should include legislation search metadata."""
        lines = _sse_lines(AUDIT_LEGISLATION)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        ctx_str = " ".join(result["retrieval_context"])
        assert "Housing Act 1985" in ctx_str

    def test_fallback_used_false(self):
        """fallback_used should be False when get_legislation_text is not called."""
        lines = _sse_lines(AUDIT_LEGISLATION)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["fallback_used"] is False

    def test_new_fields_populated(self):
        """New fields from the audit event should be populated."""
        lines = _sse_lines(AUDIT_LEGISLATION)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["chat_mode"] == "research"
        assert result["provider"] == "openrouter"
        assert result["total_cost_usd"] == 0.05
        assert result["total_ms"] == 14200
        assert result["audit_schema_version"] == 1
        assert result["audit_json"] is not None

    def test_is_error_false(self):
        """is_error should be False for a successful run."""
        lines = _sse_lines(AUDIT_LEGISLATION)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["is_error"] is False
        assert result["error_message"] == ""


# ---------------------------------------------------------------------------
# Tests, deep research
# ---------------------------------------------------------------------------


class TestDeepResearch:
    """Test the deep research audit event with multiple delegations."""

    def test_research_output_concatenates_all_reports(self):
        """research_output should join all delegation reports."""
        lines = _sse_lines(AUDIT_DEEP_RESEARCH)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert "Step 1 report" in result["research_output"]
        assert "Step 2 report" in result["research_output"]
        assert "\n\n" in result["research_output"]

    def test_tool_sequence_has_delegate_per_delegation(self):
        """tool_sequence should contain delegate_research repeated per delegation."""
        lines = _sse_lines(AUDIT_DEEP_RESEARCH)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["tool_sequence"].count("delegate_research") == 2

    def test_summarisation_detected(self):
        """summarisation_used should be True when a tool has summarised=True."""
        lines = _sse_lines(AUDIT_DEEP_RESEARCH)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["summarisation_used"] is True
        assert result["summarisation_output"] is not None
        assert len(result["summarisation_output"]) > 0

    def test_local_cache_hits_counted(self):
        """local_cache_hits should count tools with local_cache_hit=True."""
        lines = _sse_lines(AUDIT_DEEP_RESEARCH)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["local_cache_hits"] == 1

    def test_reformatted_detected(self):
        """reformatted should be True when any delegation has reformatted=True."""
        lines = _sse_lines(AUDIT_DEEP_RESEARCH)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["reformatted"] is True

    def test_chat_mode_deep_research(self):
        """chat_mode should be deep_research."""
        lines = _sse_lines(AUDIT_DEEP_RESEARCH)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["chat_mode"] == "deep_research"


# ---------------------------------------------------------------------------
# Tests, failed run
# ---------------------------------------------------------------------------


class TestFailedRun:
    """Test a failed run where the audit event carries an error."""

    def test_is_error_true(self):
        """is_error should be True when audit['error'] is set."""
        lines = _sse_lines(AUDIT_FAILED)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["is_error"] is True

    def test_error_message_from_audit(self):
        """error_message should be populated from audit['error']."""
        lines = _sse_lines(AUDIT_FAILED)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["error_message"] == "Worker agent timed out"


# ---------------------------------------------------------------------------
# Tests, missing audit event
# ---------------------------------------------------------------------------


class TestMissingAuditEvent:
    """Test that a stream without an audit event raises RuntimeError."""

    def test_raises_runtime_error(self):
        """audit_capture should raise RuntimeError naming commit da3070d."""
        lines = _sse_lines(
            {"type": "token", "content": "some text"},
            {"type": "result", "message": {"content": "final answer"}},
        )
        with pytest.raises(RuntimeError, match="da3070d"):
            audit_capture(_MockClient(lines), "test question", "test-model")

    def test_stream_error_is_an_error_result_not_an_old_build(self):
        """A server error with no audit event is a failed request, not an old build."""
        lines = _sse_lines(
            {"type": "error", "error": "Service temporarily unavailable."},
        )
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["is_error"] is True
        assert "Service temporarily unavailable" in result["error_message"]
        assert result["actual_output"] == ""


# ---------------------------------------------------------------------------
# Tests, unknown schema version
# ---------------------------------------------------------------------------


class TestUnknownSchemaVersion:
    """Test which audit schema versions are accepted and which raise.

    LexChat bumped its schema to 5 through four additive changes, so the
    range is supported and only something past the top is refused. The
    refusal has to stay loud: a version we have not read may have moved a
    field rather than added one, and a silent miss would show up as a
    metric blaming the model for empty capture.
    """

    def test_raises_runtime_error_above_supported_range(self):
        """audit_capture should raise RuntimeError naming the version."""
        lines = _sse_lines(AUDIT_WRONG_SCHEMA)
        with pytest.raises(RuntimeError, match="schema_version"):
            audit_capture(_MockClient(lines), "test question", "test-model")

    @pytest.mark.parametrize("version", [0, 7, 99, "5", None, "abc"])
    def test_unsupported_or_malformed_versions_raise(self, version):
        """A non-integer version must raise the clear error, not a TypeError."""
        event = {**AUDIT_WRONG_SCHEMA, "schema_version": version}
        with pytest.raises(RuntimeError, match="schema_version"):
            audit_capture(_MockClient(_sse_lines(event)), "test question", "test-model")

    @pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6])
    def test_supported_versions_are_captured(self, version):
        """Every version in the supported range should capture normally."""
        event = {**AUDIT_WRONG_SCHEMA, "schema_version": version}
        result = audit_capture(
            _MockClient(_sse_lines(event)), "test question", "test-model"
        )
        assert result["audit_schema_version"] == version
        assert result["actual_output"] == "Some answer"


# ---------------------------------------------------------------------------
# Tests, tool with missing/empty name
# ---------------------------------------------------------------------------


class TestMissingToolName:
    """Test that tools with missing or empty name fields get a placeholder."""

    def test_missing_name_uses_unknown_placeholder(self):
        """Tools with no 'name' field should produce 'Worker: unknown'."""
        lines = _sse_lines(AUDIT_MISSING_TOOL_NAME)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        names = [t["name"] for t in result["tools_called"]]
        # The named tool should be preserved
        assert "Worker: search_legislation" in names
        # Missing-name and empty-name tools should both become "Worker: unknown"
        assert names.count("Worker: unknown") == 2
        # No ambiguous "Worker: " entries should exist
        assert "Worker: " not in names

    def test_missing_name_in_tool_sequence(self):
        """tool_sequence should also use 'Worker: unknown' for missing names."""
        lines = _sse_lines(AUDIT_MISSING_TOOL_NAME)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["tool_sequence"].count("Worker: unknown") == 2
        assert "Worker: " not in result["tool_sequence"]

    def test_missing_name_preserves_tool_data(self):
        """Tool args and output should still be captured for unnamed tools."""
        lines = _sse_lines(AUDIT_MISSING_TOOL_NAME)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        unnamed_tools = [
            t for t in result["tools_called"] if t["name"] == "Worker: unknown"
        ]
        assert len(unnamed_tools) == 2
        # Check that the output from the unnamed tools is preserved
        outputs = [t["output"] for t in unnamed_tools]
        assert "Result from unnamed tool" in outputs
        assert "Result from empty-named tool" in outputs


class TestTurnCapHalt:
    """A deep research step can be cut short at the server's ReAct turn cap.
    The server reports this in audit.timings; the eval surfaces it as a column
    so a step that returned no report is not mistaken for a model failure."""

    @staticmethod
    def _audit(**timings):
        audit = copy.deepcopy(AUDIT_LEGISLATION)
        audit["timings"] = {**audit["timings"], **timings}
        return audit

    def test_halt_counts_are_captured(self):
        lines = _sse_lines(self._audit(max_turns_halted=1, react_turns_max=20))
        result = audit_capture(_MockClient(lines), "q", "test-model")
        assert result["max_turns_halted"] == 1
        assert result["react_turns_max"] == 20

    def test_zero_halts_is_kept_not_nulled(self):
        """0 means "no step halted", which is different from "not reported"."""
        lines = _sse_lines(self._audit(max_turns_halted=0, react_turns_max=12))
        result = audit_capture(_MockClient(lines), "q", "test-model")
        assert result["max_turns_halted"] == 0
        assert result["react_turns_max"] == 12

    def test_absent_from_timings_is_none(self):
        """Older LexChat builds do not report these fields at all."""
        audit = copy.deepcopy(AUDIT_LEGISLATION)
        audit["timings"] = {"total_ms": 1000}
        result = audit_capture(_MockClient(_sse_lines(audit)), "q", "test-model")
        assert result["max_turns_halted"] is None
        assert result["react_turns_max"] is None


# ---------------------------------------------------------------------------
# Tests, case law tools
# ---------------------------------------------------------------------------

# The National Archives returns Atom XML and LegalDocML, not JSON, so LexChat
# records only a 300-character preview of the raw response under api_calls and
# puts the parsed results in the tool's own raw_result. This fixture keeps that
# shape: the api_call carries nothing usable, the tool result carries everything.
AUDIT_CASE_LAW = {
    "type": "audit",
    "schema_version": 1,
    "request_id": "c0c0c0c0",
    "chat_mode": "research",
    "research_mode": "case_law_only",
    "provider": "openrouter",
    "model": "openai/gpt-4o",
    "answer": "The Court of Appeal held...",
    "delegations": [
        {
            "id": "d1",
            "kind": "delegation",
            "step": None,
            "title": None,
            "brief": "Find case law on unlawful detention",
            "report": "**Summary Answer (BLUF):**\nSee Burgin.",
            "reformatted": False,
            "error": None,
            "started_at": 0.5,
            "duration_s": 9.0,
            "tools": [
                {
                    "id": "t1",
                    "name": "search_case_law",
                    "args": {"query": "unlawful detention"},
                    "raw_result": json.dumps(
                        {
                            "results": [
                                {
                                    "title": "Burgin & Anor v Commission of Police",
                                    "ncn": "[2011] EWHC 1835 (Admin)",
                                    "court": "ewhc/admin",
                                    "date": "2011-07-13",
                                    "url": "https://caselaw.nationalarchives.gov.uk/ewhc/admin/2011/1835",
                                }
                            ],
                            "total": 1,
                            "query": "unlawful detention",
                        }
                    ),
                    "final_result": "...",
                    "summarised": False,
                    "local_cache_hit": False,
                    "memo_hit": False,
                    "api_calls": [
                        {
                            "url": "https://caselaw.nationalarchives.gov.uk/atom.xml",
                            "method": "GET",
                            "request": {},
                            "status": 200,
                            "response": {"preview": '<?xml version="1.0"?><feed'},
                        }
                    ],
                },
                {
                    "id": "t2",
                    "name": "search_case_law",
                    "args": {"query": "false imprisonment"},
                    "raw_result": json.dumps(
                        {
                            "results": [
                                {
                                    "title": "Burgin & Anor v Commission of Police",
                                    "ncn": "[2011] EWHC 1835 (Admin)",
                                    "court": "ewhc/admin",
                                    "date": "2011-07-13",
                                    "url": "https://caselaw.nationalarchives.gov.uk/ewhc/admin/2011/1835",
                                }
                            ],
                            "total": 1,
                            "query": "false imprisonment",
                        }
                    ),
                    "final_result": "...",
                    "summarised": False,
                    "local_cache_hit": False,
                    "memo_hit": False,
                    "api_calls": [],
                },
                {
                    "id": "t3",
                    "name": "get_case_law_text",
                    "args": {
                        "url": "https://caselaw.nationalarchives.gov.uk/ewhc/admin/2011/1835"
                    },
                    "raw_result": json.dumps(
                        {
                            "url": "https://caselaw.nationalarchives.gov.uk/ewhc/admin/2011/1835",
                            "title": "Burgin & Anor v Commission of Police",
                            "ncn": "[2011] EWHC 1835 (Admin)",
                            "text": "The claimants were detained for six hours.",
                        }
                    ),
                    "final_result": "...",
                    "summarised": False,
                    "local_cache_hit": False,
                    "memo_hit": False,
                    "api_calls": [
                        {
                            "url": "https://caselaw.nationalarchives.gov.uk/ewhc/admin/2011/1835/data.xml",
                            "method": "GET",
                            "request": {},
                            "status": 200,
                            "response": {"preview": "The claimants were detained"},
                        }
                    ],
                },
            ],
        }
    ],
    "timings": {"total_ms": 9000, "llm_calls": 2, "total_cost_usd": 0.03},
    "error": None,
}


class TestCaseLaw:
    """Case law tools populate case_law_context and retrieval_context."""

    def test_case_law_context_from_search(self):
        """search_case_law results become case_law_context entries."""
        lines = _sse_lines(AUDIT_CASE_LAW)
        result = audit_capture(
            _MockClient(lines), "test question", "test-model", "case_law_only"
        )
        assert result["case_law_context"] == [
            {
                "title": "Burgin & Anor v Commission of Police",
                "ncn": "[2011] EWHC 1835 (Admin)",
                "court": "ewhc/admin",
                "date": "2011-07-13",
                "url": "https://caselaw.nationalarchives.gov.uk/ewhc/admin/2011/1835",
            }
        ]

    def test_same_case_from_two_searches_appears_once(self):
        """A judgment returned by several searches is recorded once."""
        lines = _sse_lines(AUDIT_CASE_LAW)
        result = audit_capture(
            _MockClient(lines), "test question", "test-model", "case_law_only"
        )
        assert len(result["case_law_context"]) == 1

    def test_retrieval_context_has_case_summary_and_judgment_text(self):
        """Both the search hit and the fetched judgment text are retrievable."""
        lines = _sse_lines(AUDIT_CASE_LAW)
        result = audit_capture(
            _MockClient(lines), "test question", "test-model", "case_law_only"
        )
        ctx_str = " ".join(result["retrieval_context"])
        assert "[2011] EWHC 1835 (Admin)" in ctx_str
        assert "The claimants were detained for six hours." in ctx_str

    def test_case_law_does_not_set_fallback_used(self):
        """fallback_used tracks get_legislation_text only."""
        lines = _sse_lines(AUDIT_CASE_LAW)
        result = audit_capture(
            _MockClient(lines), "test question", "test-model", "case_law_only"
        )
        assert result["fallback_used"] is False

    def test_search_returning_no_results_adds_nothing(self):
        """A zero-result search leaves case_law_context empty."""
        audit = copy.deepcopy(AUDIT_CASE_LAW)
        tools = audit["delegations"][0]["tools"]
        for tool in tools:
            if tool["name"] == "search_case_law":
                tool["raw_result"] = json.dumps(
                    {"results": [], "total": 0, "query": "nothing"}
                )
        del tools[2]  # the get_case_law_text call that followed the hit
        lines = _sse_lines(audit)
        result = audit_capture(
            _MockClient(lines), "test question", "test-model", "case_law_only"
        )
        assert result["case_law_context"] == []
        assert result["retrieval_context"] == []


# ---------------------------------------------------------------------------
# Tests, per-delegation halts and empty completions (audit schema v2-v4)
# ---------------------------------------------------------------------------


AUDIT_HALTED_STEPS = {
    "type": "audit",
    "schema_version": 5,
    "request_id": "halt789",
    "chat_mode": "deep_research",
    "research_mode": "legislation_only",
    "provider": "openrouter",
    "model": "openai/gpt-4o",
    "answer": "An incomplete answer",
    "delegations": [
        {"brief": "step 1", "report": "## Summary Answer", "tools": [], "halted": None},
        {
            "brief": "step 2",
            "report": "[Research Incomplete - step limit reached]",
            "tools": [],
            "halted": {
                "reason": "step_cap",
                "limit": 20,
                "steps": 20,
                "written_up": False,
            },
        },
        {
            "brief": "step 3",
            "report": "[Research Incomplete - step limit reached; PARTIAL findings below]",
            "tools": [],
            "halted": {
                "reason": "step_cap",
                "limit": 20,
                "steps": 20,
                "written_up": True,
            },
        },
    ],
    "empty_completions": [{"attempt": 1}, {"attempt": 2}],
    "timings": {"total_ms": 1000, "max_turns_halted": 2, "react_turns_max": 20},
    "error": None,
}


class TestDelegationHalts:
    """A halt has to be attributable to the step that halted.

    ``timings.max_turns_halted`` says how many steps stopped at the tool-call
    limit; it cannot say which. Without the position, a check that reads a
    step's report cannot tell a step that was cut short from one that threw
    its own research away, which is the distinction the whole halt-reporting
    fix turns on.
    """

    def test_halted_steps_carry_their_position(self):
        """Steps are numbered from 1, matching the delegation order."""
        lines = _sse_lines(AUDIT_HALTED_STEPS)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert [h["step"] for h in result["delegation_halts"]] == [2, 3]

    def test_halt_detail_is_preserved(self):
        """written_up distinguishes a bare halt from one with partial findings."""
        lines = _sse_lines(AUDIT_HALTED_STEPS)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        by_step = {h["step"]: h for h in result["delegation_halts"]}
        assert by_step[2]["written_up"] is False
        assert by_step[3]["written_up"] is True
        assert by_step[2]["reason"] == "step_cap"
        assert by_step[2]["limit"] == 20

    def test_empty_completions_counted(self):
        """The count is enough; the records stay in audit_json."""
        lines = _sse_lines(AUDIT_HALTED_STEPS)
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["empty_completions"] == 2

    def test_clean_run_reports_no_halts(self):
        """A run where nothing halted stores NULL, not an empty list."""
        event = {
            **AUDIT_HALTED_STEPS,
            "delegations": [AUDIT_HALTED_STEPS["delegations"][0]],
            "empty_completions": [],
            "timings": {"total_ms": 1000, "max_turns_halted": 0},
        }
        result = audit_capture(
            _MockClient(_sse_lines(event)), "test question", "test-model"
        )
        assert result["delegation_halts"] is None
        assert result["empty_completions"] == 0

    def test_older_schema_without_halt_key(self):
        """A v1 event has no halted key at all and must not error."""
        lines = _sse_lines(AUDIT_WRONG_SCHEMA | {"schema_version": 1})
        result = audit_capture(_MockClient(lines), "test question", "test-model")
        assert result["delegation_halts"] is None
        assert result["empty_completions"] == 0


class TestRecordTools:
    """find_definitions and find_instruments_made_under feed retrieval_context
    from their own result, never from the raw rows a definitions search read."""

    DEFINITIONS = {
        "name": "find_definitions",
        "args": {"term": "shop"},
        "raw_result": json.dumps(
            {
                "tool": "find_definitions",
                "term": "shop",
                "count": 1,
                "definitions": [
                    {
                        "legislation_id": "ukpga/1996/18",
                        "provision": "section 232",
                        "definition": '"shop" includes any premises where any retail trade is carried on',
                    }
                ],
                "longer_terms": [
                    {
                        "term": "shop worker",
                        "legislation_id": "ukpga/1996/18",
                        "provision": "section 232",
                        "definition": '"shop worker" means an employee who works in a shop',
                    }
                ],
            }
        ),
        "api_calls": [
            {"response": {"results": [{"text": "RAW ROW NEVER SHOWN TO THE MODEL"}]}}
        ],
    }

    MADE_UNDER = {
        "name": "find_instruments_made_under",
        "args": {"act": "asp/2018/9", "section": "95"},
        "raw_result": json.dumps(
            {
                "tool": "find_instruments_made_under",
                "status": "found",
                "act_id": "asp/2018/9",
                "matched_act": "Social Security (Scotland) Act 2018",
                "provision": "section/95",
                "count": 1,
                "instruments": [
                    {
                        "legislation_id": "ssi/2018/275",
                        "title": "The Social Security Appeals Regulations 2018",
                        "revocation": "no revocation recorded",
                    }
                ],
                "coverage": {"statement": "SSIs made from 1999 to 2026."},
            }
        ),
        "api_calls": [],
    }

    def _derive(self, *tools):
        from lex_eval.utils.audit_capture import derive_retrieval

        return derive_retrieval({"delegations": [{"tools": list(tools)}]})[0]

    def test_definitions_give_their_defining_words(self):
        ctx = self._derive(self.DEFINITIONS)
        assert any(
            "ukpga/1996/18 section 232" in c and "retail trade" in c for c in ctx
        )
        assert any('"shop worker" means' in c for c in ctx)

    def test_definitions_raw_rows_are_not_read(self):
        assert "RAW ROW" not in " ".join(self._derive(self.DEFINITIONS))

    def test_made_under_gives_the_count_coverage_and_each_instrument(self):
        ctx = " ".join(self._derive(self.MADE_UNDER))
        assert "1 instrument(s) whose own preamble names section/95" in ctx
        assert "SSIs made from 1999 to 2026." in ctx
        assert "(ssi/2018/275): made under section/95 of Social Security" in ctx
        assert "no revocation recorded" in ctx

    def test_made_under_not_found_gives_nothing(self):
        tool = copy.deepcopy(self.MADE_UNDER)
        tool["raw_result"] = json.dumps(
            {"tool": "find_instruments_made_under", "status": "act_not_in_record"}
        )
        assert self._derive(tool) == []

    def test_made_under_text_scan_is_kept_and_other_blocks_are_not(self):
        """A code-run text scan quotes the instruments it read, so it is
        retrieved text. The other blocks LexChat appends are guidance."""
        tool = copy.deepcopy(self.MADE_UNDER)
        tool["final_result"] = (
            tool["raw_result"]
            + "\n\n[ENABLING POWER — You MAY state that each was made under it.]"
            + "\n\n[SEARCH SCOPE — text scan by code: code read 1 of the 1 "
            'instruments.\nFound in 1:\n- ssi/2018/275: regulation 3: "£50"]'
        )
        ctx = self._derive(tool)
        assert ctx[-1].startswith("SEARCH SCOPE — text scan by code:")
        assert 'regulation 3: "£50"' in ctx[-1]
        assert "ENABLING POWER" not in " ".join(ctx)
