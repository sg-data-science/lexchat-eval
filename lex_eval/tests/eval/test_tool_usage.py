"""
Test that every captured response used all required legislation tools *and*
invoked them in the correct phase order.

Presence, 1/3 per required step: delegated research, found the legislation
(search_legislation, find_definitions or find_instruments_made_under) and read
it (search_legislation_sections, find_definitions or
find_instruments_made_under).

Order (legislation_only only), Worker tools must appear in the phase order
mandated by the Worker system prompt, and must not loop back to an earlier
phase once a later one has begun:

    1. Worker: search_legislation          (Phase 1, DISCOVER)
    2. Worker: search_legislation_sections (Phase 2, RETRIEVE PROVISIONS)
    3. Worker: get_legislation_text        (Phase 3, FALLBACK, optional)

Score:
    - 1.0  all three required steps present AND correct order
    - 0.5  all three required steps present BUT wrong order
    - <1.0 one or more required steps missing (proportional)

Passes only when score == 1.0.
"""

import pytest

from lex_eval.metrics.tool_usage import ToolUsageMetric
from lex_eval.utils.test_helpers import (
    halted_steps,
    load_records,
    record_to_test_case,
    record_id,
)
from lex_eval.utils.collector import attach_metric

records = load_records(read_only=True)


def _tools_list(test_case):
    """Extract tool names from a test case."""
    if test_case.tools_called:
        return [t.name for t in test_case.tools_called]
    return []


@pytest.mark.parametrize(
    "record",
    records,
    ids=[record_id(r) for r in records],
)
def test_tool_usage(request, record):
    """
    All three required steps must be present (delegated research, found the
    legislation, read it), with the Worker tools in the correct order:

        Worker: search_legislation (or find_definitions /
        find_instruments_made_under), Worker: search_legislation_sections,
        Worker: get_legislation_text (optional fallback)

    Score = 1.0 only when all three required steps are present AND the Worker
    tools appear in the expected phase order. Wrong order caps the score at
    0.5 (fail). Missing steps score proportionally (fail).
    """
    if record.get("needs_clarification"):
        # LexChat's deep research planner asked the user a question instead of
        # researching, a valid outcome the gather records. There is no tool
        # use to score, and the other metrics already leave it unmeasured.
        reason = "Not measured: LexChat asked for clarification instead of researching."
        attach_metric(
            request,
            record=record,
            test_name="tool_usage",
            metric_name="Tool Usage",
            score=0.0,
            threshold=1.0,
            passed=False,
            reason=reason,
        )
        pytest.skip(reason)

    test_case = record_to_test_case(record)
    research_mode = record.get("research_mode", "legislation_only")
    tool_sequence = record.get("tool_sequence") or []
    chat_mode = record.get("chat_mode", "research")
    metric = ToolUsageMetric(
        threshold=1.0,
        research_mode=research_mode,
        tool_sequence=tool_sequence,
        chat_mode=chat_mode,
        halted_steps=halted_steps(record),
    )
    metric.measure(test_case)

    attach_metric(
        request,
        record=record,
        test_name="tool_usage",
        metric_name=metric.__name__,
        score=metric.score,
        threshold=metric.threshold,
        passed=metric.is_successful(),
        reason=metric.reason,
        tools_used=_tools_list(test_case),
    )

    if metric.reason.startswith("Not measured:"):
        pytest.skip(metric.reason)
    assert metric.is_successful(), metric.reason
