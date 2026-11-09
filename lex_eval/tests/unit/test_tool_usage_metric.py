"""
Unit tests for ``lex_eval.metrics.tool_usage``'s order check, in particular
the ``chat_mode == "deep_research"`` behaviour: per-step segmentation and
dropping the no-revisit ("loop-back") rule.

The loop-back rule is real and correct for single-shot ``research`` mode, but
was measured against six real deep-research responses to fail 13 of 25 real
step-segments, because a single deep-research step routinely covers more than
one Act, discovering and retrieving each in whatever order the Worker finds
useful. These tests fix that behaviour in place with synthetic tool
sequences, no DB or LexChat instance needed.
"""

import pytest

from lex_eval.metrics.tool_usage import ToolUsageMetric, _check_tool_order

pytestmark = pytest.mark.unit

_DELEGATE = "delegate_research"
_SEARCH = "Worker: search_legislation"
_SECTIONS = "Worker: search_legislation_sections"
_FALLBACK = "Worker: get_legislation_text"


def test_single_shot_loop_back_still_fails():
    """Unchanged behaviour for ordinary single-shot research: revisiting
    search_legislation after search_legislation_sections has started is a
    violation, chat_mode defaults to "research"."""
    sequence = [_DELEGATE, _SEARCH, _SECTIONS, _SEARCH, _SECTIONS]
    ok, detail = _check_tool_order(sequence, "legislation_only")
    assert not ok, detail


def test_deep_research_step_with_multi_act_loop_back_passes():
    """The exact real-world shape found in gathered deep-research data: one
    step searches Act A's sections, then discovers and retrieves Act B, all
    within a single plan step. This must not be flagged, chat_mode=deep_research
    checks first-occurrence order per step, not the no-revisit rule."""
    sequence = [
        _DELEGATE,
        _SEARCH,
        _SECTIONS,
        _SEARCH,  # a second Act, discovered after the first Act's sections
        _SECTIONS,
    ]
    ok, detail = _check_tool_order(
        sequence, "legislation_only", chat_mode="deep_research"
    )
    assert ok, detail


def test_deep_research_step_skipping_discovery_still_fails():
    """A step that jumps straight to retrieval before any discovery is still
    a real violation in deep-research mode, first-occurrence order is
    relaxed on revisits, not dropped entirely."""
    sequence = [_DELEGATE, _SECTIONS, _SEARCH]
    ok, detail = _check_tool_order(
        sequence, "legislation_only", chat_mode="deep_research"
    )
    assert not ok
    assert "step 1" in detail


def test_deep_research_step_with_no_discovery_at_all_still_fails():
    """A step whose Worker tools are only search_legislation_sections, with
    no search_legislation call anywhere in that step, has nothing to compare
    an index against and passed trivially before require_prerequisites was
    added. Global presence (search_legislation used somewhere in the whole
    run) doesn't cover this: it's checked per plan step here."""
    sequence = [_DELEGATE, _SECTIONS]
    ok, detail = _check_tool_order(
        sequence, "legislation_only", chat_mode="deep_research"
    )
    assert not ok
    assert "step 1" in detail


def test_deep_research_multiple_steps_each_checked_independently():
    """Step 1 is fine; step 2 falls back to full text before its own section
    search. The second step's violation must be caught even though the first
    step was clean."""
    sequence = [
        _DELEGATE,
        _SEARCH,
        _DELEGATE,
        _FALLBACK,  # step 2: full text before any section search
        _SECTIONS,
    ]
    ok, detail = _check_tool_order(
        sequence, "legislation_only", chat_mode="deep_research"
    )
    assert not ok
    assert "step 2" in detail


def test_deep_research_step_may_read_what_an_earlier_step_found():
    """Step 2 reads sections of an Act step 1 found, then searches for
    another. Its discovery was done by step 1, so this is not skipping it."""
    sequence = [_DELEGATE, _SEARCH, _SECTIONS, _DELEGATE, _SECTIONS, _SEARCH]
    ok, detail = _check_tool_order(
        sequence, "legislation_only", chat_mode="deep_research"
    )
    assert ok, detail


def test_tool_usage_metric_threads_chat_mode_through():
    """ToolUsageMetric itself (not just the module-level helper) must pass a
    multi-Act deep-research step that the plain "research" mode would fail."""
    sequence = [_DELEGATE, _SEARCH, _SECTIONS, _SEARCH, _SECTIONS, _FALLBACK]

    deep_research_metric = ToolUsageMetric(
        research_mode="legislation_only",
        tool_sequence=sequence,
        chat_mode="deep_research",
    )
    research_metric = ToolUsageMetric(
        research_mode="legislation_only",
        tool_sequence=sequence,
        chat_mode="research",
    )

    class _FakeToolCall:
        def __init__(self, name):
            self.name = name
            self.output = ""

    class _FakeTestCase:
        tools_called = [_FakeToolCall(t) for t in sequence]

    deep_research_metric.measure(_FakeTestCase())
    research_metric.measure(_FakeTestCase())

    assert deep_research_metric.is_successful(), deep_research_metric.reason
    assert not research_metric.is_successful()


def test_conversational_loop_back_passes():
    """A conversational turn asking about several instruments goes back to
    search after reading the first Act's sections. That is the same multi-Act
    interleaving the deep-research exemption exists for, and it accounted for
    16 of the 22 order failures in the August 2026 question set."""
    sequence = [_DELEGATE, _SEARCH, _SECTIONS, _SEARCH, _SECTIONS]
    ok, detail = _check_tool_order(
        sequence, "legislation_only", chat_mode="conversational"
    )
    assert ok, detail


def test_conversational_still_requires_search_before_sections():
    """Loosening the revisit rule must not stop the metric noticing a run that
    retrieved provisions it never searched for."""
    sequence = [_DELEGATE, _SECTIONS, _SEARCH]
    ok, detail = _check_tool_order(
        sequence, "legislation_only", chat_mode="conversational"
    )
    assert not ok, detail


def test_conversational_still_requires_sections_before_fallback():
    sequence = [_DELEGATE, _SEARCH, _FALLBACK, _SECTIONS]
    ok, detail = _check_tool_order(
        sequence, "legislation_only", chat_mode="conversational"
    )
    assert not ok, detail


# ---------------------------------------------------------------------------
# A run stopped at LexChat's tool-call limit
# ---------------------------------------------------------------------------


class _Call:
    def __init__(self, name):
        self.name = name
        self.output = ""


def _case(sequence):
    class _Case:
        tools_called = [_Call(t) for t in sequence]

    return _Case()


class TestHaltedRunsAreNotScoredForMissingTools:
    """A tool the limit stopped the Worker reaching is not a model choice.

    The presence check is over the whole run, so a halt only matters when a
    required tool is genuinely absent. When the other steps called everything
    anyway, the halt says nothing about tool usage and the run is scored as
    normal.
    """

    def test_missing_tool_after_a_halt_is_not_measured(self):
        sequence = [_DELEGATE, _SEARCH]
        metric = ToolUsageMetric(
            research_mode="legislation_only",
            tool_sequence=sequence,
            chat_mode="deep_research",
            halted_steps={1},
        )
        metric.measure(_case(sequence))
        assert metric.reason.startswith("Not measured:")
        assert not metric.is_successful()

    def test_missing_tool_without_a_halt_still_fails(self):
        sequence = [_DELEGATE, _SEARCH]
        metric = ToolUsageMetric(
            research_mode="legislation_only",
            tool_sequence=sequence,
            chat_mode="deep_research",
        )
        metric.measure(_case(sequence))
        assert not metric.reason.startswith("Not measured:")
        assert not metric.is_successful()
        assert metric.score == pytest.approx(2 / 3)

    def test_halt_does_not_excuse_a_run_that_used_every_tool(self):
        """A complete chain is scored on its merits whether it halted or not."""
        sequence = [_DELEGATE, _SEARCH, _SECTIONS]
        metric = ToolUsageMetric(
            research_mode="legislation_only",
            tool_sequence=sequence,
            chat_mode="deep_research",
            halted_steps={1},
        )
        metric.measure(_case(sequence))
        assert metric.score == 1.0
        assert metric.is_successful()


_DEFINITIONS = "Worker: find_definitions"
_MADE_UNDER = "Worker: find_instruments_made_under"


def _score(sequence, chat_mode="research"):
    from lex_eval.testcase import LLMTestCase, ToolCall

    case = LLMTestCase(
        input="q",
        actual_output="a",
        tools_called=[
            ToolCall(name=t, input_parameters={}, output="x") for t in sequence
        ],
    )
    metric = ToolUsageMetric(tool_sequence=sequence, chat_mode=chat_mode)
    metric.measure(case)
    return metric


def test_made_under_alone_finds_and_reads():
    """A made-under question answered from the record alone, the shape of
    the v2026.11.1 conversational runs on Q13."""
    metric = _score([_DELEGATE, _MADE_UNDER], chat_mode="conversational")
    assert metric.score == 1.0, metric.reason
    assert "find_instruments_made_under" in metric.reason


def test_definitions_then_sections_in_a_deep_research_step_passes():
    """find_definitions is the step's discovery, so the section searches
    after it have their prerequisite."""
    sequence = [_DELEGATE, _DEFINITIONS, _SECTIONS, _SECTIONS, _DELEGATE, _DEFINITIONS]
    metric = _score(sequence, chat_mode="deep_research")
    assert metric.score == 1.0, metric.reason


def test_full_text_of_instruments_an_earlier_step_listed_passes():
    """The v2026.11.1 Q25 shape: the made-under record lists the instruments
    and a later step reads their full text. The record counts as finding and
    reading, so the later step has its prerequisites."""
    sequence = [
        _DELEGATE,
        _SEARCH,
        _MADE_UNDER,
        _DELEGATE,
        _MADE_UNDER,
        _DELEGATE,
        _FALLBACK,
    ]
    metric = _score(sequence, chat_mode="deep_research")
    assert metric.score == 1.0, metric.reason


def test_a_record_tool_after_sections_is_a_loop_back_in_research_mode():
    """The record tools are discovery for the order check, so the single-shot
    no-revisit rule treats them exactly as it treats search_legislation."""
    ok, detail = _check_tool_order(
        [_DELEGATE, _SEARCH, _SECTIONS, _DEFINITIONS], "legislation_only"
    )
    assert not ok
    assert "find_definitions called again" in detail


def test_missing_reading_step_is_still_missed():
    metric = _score([_DELEGATE, _SEARCH])
    assert metric.score < 1.0
    assert "read the legislation: ✗" in metric.reason
