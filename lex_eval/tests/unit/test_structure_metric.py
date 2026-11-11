"""
Unit tests for ``lex_eval.metrics.structure.MandatoryStructureMetric``'s
heading matching, synthetic Worker output, no DB or LexChat instance needed.
"""

import json

import pytest
from lex_eval.testcase import LLMTestCase, ToolCall

from lex_eval.metrics.structure import (
    CitationGroundingMetric,
    CitationReadMetric,
    GenuineGapMetric,
    MandatoryStructureMetric,
    _retrieved_usable_content,
)

pytestmark = pytest.mark.unit


_VALID_REPORT = """### **Summary Answer (BLUF)**
Some answer text.

### **Detailed Analysis**
Analysis text.

### **Jurisdiction & Status**
Applies to Scotland.

### **References**
- [Act](http://legislation.gov.uk/example)
"""


def _test_case(*delegate_outputs: str) -> LLMTestCase:
    return LLMTestCase(
        input="q",
        actual_output="final answer",
        tools_called=[
            ToolCall(
                name="delegate_research", input_parameters={}, output=delegate_output
            )
            for delegate_output in delegate_outputs
        ],
    )


def test_prose_use_of_references_does_not_count_as_heading():
    """A missing References *section* must still fail, even when the word
    "references" appears earlier in ordinary prose (real failure mode found
    in captured glm-5.2:cloud output, see structure.py)."""
    output = """### **Summary Answer (BLUF)**
Some answer text.

### **Detailed Analysis**
Section 21(1) provides that references in ss.76, 77, 78 include references
to the 1978 Act.

### **Jurisdiction & Status**
Applies to Scotland.
"""
    metric = MandatoryStructureMetric(research_mode="legislation_only")
    metric.measure(_test_case(output))
    assert not metric.is_successful()
    assert "References" in metric.reason


def test_real_references_heading_after_prose_use_still_passes():
    output = """### **Summary Answer (BLUF)**
Some answer text.

### **Detailed Analysis**
This section references the 1978 Act throughout.

### **Jurisdiction & Status**
Applies to Scotland.

### **References**
- [Act](http://legislation.gov.uk/example)
"""
    metric = MandatoryStructureMetric(research_mode="legislation_only")
    metric.measure(_test_case(output))
    assert metric.is_successful()


def test_jurisdiction_and_status_spelled_out_passes():
    """Model paraphrasing the prompt's literal "&" as "and" is compliant,
    not a missing-heading failure (real failure mode found in captured
    mistral-large-3 output, see structure.py)."""
    output = """### **Summary Answer (BLUF)**
Some answer text.

### **Detailed Analysis**
Analysis text.

#### **3. Jurisdiction and Status**
Applies to Scotland.

### **References**
- [Act](http://legislation.gov.uk/example)
"""
    metric = MandatoryStructureMetric(research_mode="legislation_only")
    metric.measure(_test_case(output))
    assert metric.is_successful(), metric.reason


def test_deep_research_all_steps_present_passes():
    """A deep-research run has one delegate_research entry per plan step;
    every step's report has the mandatory headings here, so it should pass
    exactly like a single-shot run would."""
    metric = MandatoryStructureMetric(research_mode="legislation_only")
    metric.measure(_test_case(_VALID_REPORT, _VALID_REPORT, _VALID_REPORT))
    assert metric.is_successful(), metric.reason


def test_deep_research_bad_second_step_fails():
    """A bad step 2 must not hide behind a good step 1 (the bug this fix
    addresses: the metric used to only look at the first delegate_research
    output, so a deep-research run's later steps were invisible to it)."""
    broken_step = """### **Summary Answer (BLUF)**
Some answer text.

### **Detailed Analysis**
Analysis text.
"""
    metric = MandatoryStructureMetric(research_mode="legislation_only")
    metric.measure(_test_case(_VALID_REPORT, broken_step))
    assert not metric.is_successful()
    assert "step 2" in metric.reason


class TestCitationGroundingParsesSearchResults:
    """``search_legislation`` returns JSON followed by a plain-text "[NEXT STEP:]"
    hint. A plain json.loads of the whole string raises "Extra data", which
    previously left every search result invisible and made correctly cited Acts
    look fabricated."""

    @staticmethod
    def _case(cited_id: str) -> LLMTestCase:
        search_output = (
            '{"results": [{"legislation_id": "asp/2021/3", '
            '"title": "An Act", "url": "http://www.legislation.gov.uk/asp/2021/3"}], '
            '"total": 1}\n\n'
            "[NEXT STEP: Call search_legislation_sections with the relevant "
            'legislation_id(s) below:\n  - legislation_id: "asp/2021/3"]'
        )
        report = f"See [the Act](http://www.legislation.gov.uk/id/{cited_id})."
        return LLMTestCase(
            input="q",
            actual_output="final answer",
            tools_called=[
                ToolCall(
                    name="Worker: search_legislation",
                    input_parameters={},
                    output=search_output,
                ),
                ToolCall(name="delegate_research", input_parameters={}, output=report),
            ],
        )

    def test_act_returned_by_search_is_grounded(self):
        metric = CitationGroundingMetric()
        metric.measure(self._case("asp/2021/3"))
        assert metric.score == 1.0
        assert metric.is_successful()

    def test_act_never_retrieved_is_still_flagged(self):
        metric = CitationGroundingMetric()
        metric.measure(self._case("asp/2000/7"))
        assert metric.score == 0.0
        assert "asp/2000/7" in metric.reason

    def test_non_json_tool_output_is_skipped_not_fatal(self):
        """LEX API errors come back as prose, not JSON."""
        case = LLMTestCase(
            input="q",
            actual_output="final answer",
            tools_called=[
                ToolCall(
                    name="Worker: search_legislation",
                    input_parameters={},
                    output='Error executing tool: {"detail": "Internal server error"}',
                ),
                ToolCall(
                    name="delegate_research", input_parameters={}, output="No links."
                ),
            ],
        )
        metric = CitationGroundingMetric()
        metric.measure(case)
        assert metric.score == 1.0


class TestRetrievedUsableContentRejectsToolErrors:
    """A LEX tool failure comes back as non-empty prose ("Error executing
    tool: ..."), which truthiness alone would count as usable retrieval,
    incorrectly excusing GenuineGapMetric's disclosure requirement."""

    def test_error_string_is_not_usable_content(self):
        tool = ToolCall(
            name="Worker: search_legislation_sections",
            input_parameters={},
            output='Error executing tool: {"detail": "Internal server error"}',
        )
        assert _retrieved_usable_content([tool]) is False

    def test_real_content_is_usable(self):
        tool = ToolCall(
            name="Worker: search_legislation_sections",
            input_parameters={},
            output="Section 6: ...",
        )
        assert _retrieved_usable_content([tool]) is True

    def test_genuine_gap_requires_disclosure_after_a_tool_error(self):
        """A step whose only search_legislation_sections call failed must be
        judged as empty retrieval, not excused because the failure text was
        non-empty."""
        report = _VALID_REPORT  # confidently answers, no gap disclosure
        case = LLMTestCase(
            input="q",
            actual_output="final answer",
            tools_called=[
                ToolCall(name="delegate_research", input_parameters={}, output=report),
                ToolCall(
                    name="Worker: search_legislation_sections",
                    input_parameters={},
                    output='Error executing tool: {"detail": "Internal server error"}',
                ),
            ],
        )
        metric = GenuineGapMetric(research_mode="legislation_only")
        metric.measure(case)
        assert not metric.is_successful(), metric.reason


class TestCitationReadRequiresTextNotJustATitle:
    """Citation Grounding accepts an Act that merely turned up in a
    ``search_legislation`` results list, which returns titles and links but no
    legal text. Citation Read is the check that the Worker actually opened it."""

    _SEARCH_OUTPUT = (
        '{"results": [{"legislation_id": "ssi/2015/99", '
        '"title": "Commencement Order", '
        '"url": "http://www.legislation.gov.uk/ssi/2015/99"}], "total": 1}\n\n'
        "[NEXT STEP: Call search_legislation_sections]"
    )

    @staticmethod
    def _case(report: str, *tools: ToolCall) -> LLMTestCase:
        return LLMTestCase(
            input="q",
            actual_output="final answer",
            tools_called=[
                *tools,
                ToolCall(name="delegate_research", input_parameters={}, output=report),
            ],
        )

    @staticmethod
    def _cite(*legislation_ids: str) -> str:
        return " ".join(
            f"See [it](http://www.legislation.gov.uk/id/{lid})."
            for lid in legislation_ids
        )

    def test_act_whose_sections_were_read_passes(self):
        tool = ToolCall(
            name="Worker: search_legislation_sections",
            input_parameters={"legislation_id": "ssi/2015/99"},
            output='[{"text": "Section 1) This Order comes into force..."}]',
        )
        metric = CitationReadMetric()
        metric.measure(self._case(self._cite("ssi/2015/99"), tool))
        assert metric.score == 1.0
        assert metric.is_successful()

    def test_act_seen_only_in_a_search_result_fails(self):
        tool = ToolCall(
            name="Worker: search_legislation",
            input_parameters={},
            output=self._SEARCH_OUTPUT,
        )
        metric = CitationReadMetric()
        metric.measure(self._case(self._cite("ssi/2015/99"), tool))
        assert metric.score == 0.0
        assert "ssi/2015/99" in metric.reason
        # ...where Citation Grounding, seeing the same run, is satisfied.
        grounding = CitationGroundingMetric()
        grounding.measure(self._case(self._cite("ssi/2015/99"), tool))
        assert grounding.score == 1.0

    def test_failed_text_retrieval_does_not_count_as_read(self):
        tool = ToolCall(
            name="Worker: get_legislation_text",
            input_parameters={"legislation_id": "ssi/2015/99"},
            output='Error executing tool: {"detail": "Internal server error"}',
        )
        metric = CitationReadMetric()
        metric.measure(self._case(self._cite("ssi/2015/99"), tool))
        assert metric.score == 0.0

    def test_score_is_the_fraction_of_cited_acts_read(self):
        tools = [
            ToolCall(
                name="Worker: search_legislation_sections",
                input_parameters={"legislation_id": lid},
                output='[{"text": "Section 1) ..."}]',
            )
            for lid in ("asp/2015/1", "asp/2014/9", "ukpga/1978/29")
        ]
        report = self._cite("asp/2015/1", "asp/2014/9", "ukpga/1978/29", "ssi/2015/99")
        metric = CitationReadMetric()
        metric.measure(self._case(report, *tools))
        assert metric.score == 0.75
        assert not metric.is_successful()

    def test_case_law_citations_are_ignored(self):
        """Case law links have no legislation retrieval to check them against."""
        report = (
            "See [the case]"
            "(https://caselaw.nationalarchives.gov.uk/ewca/civ/2010/123)."
        )
        metric = CitationReadMetric()
        metric.measure(self._case(report))
        assert metric.score == 1.0
        assert "nothing to check" in metric.reason

    def test_no_citations_at_all_scores_one(self):
        metric = CitationReadMetric()
        metric.measure(self._case("No links in this report."))
        assert metric.score == 1.0

    def test_no_delegate_research_call_is_not_a_quality_verdict(self):
        """The reason must carry the prefix streamlit_report.py's
        _NON_SCORED_PREFIXES uses to keep the row out of the mean."""
        case = LLMTestCase(input="q", actual_output="final answer", tools_called=[])
        metric = CitationReadMetric()
        metric.measure(case)
        assert metric.score == 0.0
        assert metric.reason.startswith("No 'delegate_research' tool call found;")


class TestCitationReadSiblingStepDiagnostic:
    """In a deep research run a step can cite an Act a *sibling* step read but
    it did not, i.e. assert ahead of its own evidence. Reported in the reason,
    never scored, since a step's References section lists Acts it did not
    read."""

    @staticmethod
    def _two_step_case() -> LLMTestCase:
        """Step 1 reads only asp/2015/1 but cites ssi/2015/99; step 2 reads
        ssi/2015/99."""

        def _read(lid):
            return ToolCall(
                name="Worker: search_legislation_sections",
                input_parameters={"legislation_id": lid},
                output='[{"text": "Section 1) ..."}]',
            )

        def _cite(*lids):
            return " ".join(
                f"[x](http://www.legislation.gov.uk/id/{lid})" for lid in lids
            )

        return LLMTestCase(
            input="q",
            actual_output="final answer",
            tools_called=[
                ToolCall(
                    name="delegate_research",
                    input_parameters={},
                    output=_cite("asp/2015/1", "ssi/2015/99"),
                ),
                _read("asp/2015/1"),
                ToolCall(
                    name="delegate_research",
                    input_parameters={},
                    output=_cite("ssi/2015/99"),
                ),
                _read("ssi/2015/99"),
            ],
        )

    def test_sibling_read_is_reported(self):
        metric = CitationReadMetric()
        metric.measure(self._two_step_case())
        assert "Diagnostic, not scored" in metric.reason
        assert "step 1" in metric.reason
        assert "ssi/2015/99" in metric.reason

    def test_sibling_read_does_not_affect_the_score(self):
        """The run as a whole read both Acts, so the score stays 1.0 and the
        record still passes. This is the behaviour the diagnostic replaces."""
        metric = CitationReadMetric()
        metric.measure(self._two_step_case())
        assert metric.score == 1.0
        assert metric.is_successful()

    def test_single_step_run_gets_no_diagnostic(self):
        case = LLMTestCase(
            input="q",
            actual_output="final answer",
            tools_called=[
                ToolCall(
                    name="delegate_research",
                    input_parameters={},
                    output="[x](http://www.legislation.gov.uk/id/asp/2015/1)",
                ),
                ToolCall(
                    name="Worker: search_legislation_sections",
                    input_parameters={"legislation_id": "asp/2015/1"},
                    output='[{"text": "Section 1) ..."}]',
                ),
            ],
        )
        metric = CitationReadMetric()
        metric.measure(case)
        assert "Diagnostic" not in metric.reason

    def test_act_no_step_read_still_fails_and_is_not_a_diagnostic(self):
        """An Act nobody read is the scored failure, not the diagnostic."""
        case = LLMTestCase(
            input="q",
            actual_output="final answer",
            tools_called=[
                ToolCall(
                    name="delegate_research",
                    input_parameters={},
                    output="[x](http://www.legislation.gov.uk/id/ssi/2015/99)",
                ),
                ToolCall(
                    name="Worker: search_legislation_sections",
                    input_parameters={"legislation_id": "asp/2015/1"},
                    output='[{"text": "Section 1) ..."}]',
                ),
                ToolCall(name="delegate_research", input_parameters={}, output="none"),
            ],
        )
        metric = CitationReadMetric()
        metric.measure(case)
        assert metric.score == 0.0
        assert "Cited without reading" in metric.reason
        assert "Diagnostic" not in metric.reason


class TestRegnalYearActsAreNotFabricated:
    """Acts before 1963 are identified by regnal year, e.g. ``ukpga/Edw7/4/31``
    is the Shop Hours Act 1904.

    Two things used to make every one of them look fabricated: the Act id was
    taken as the first three path segments, which cuts a four-segment regnal id
    in half, and the API sends ids cased while a citation URL arrives
    lowercased. Both were live, and together they scored two "shop definitions"
    runs 0.0 for citing Acts their own searches had returned.
    """

    @staticmethod
    def _case(retrieved_id: str, cited_path: str) -> LLMTestCase:
        search_output = json.dumps(
            {
                "results": [
                    {
                        "legislation_id": retrieved_id,
                        "title": "An Act",
                        "url": f"http://www.legislation.gov.uk/{retrieved_id}",
                    }
                ],
                "total": 1,
            }
        )
        report = f"See [the Act](http://www.legislation.gov.uk/id/{cited_path})."
        return LLMTestCase(
            input="q",
            actual_output="final answer",
            tools_called=[
                ToolCall(
                    name="Worker: search_legislation",
                    input_parameters={},
                    output=search_output,
                ),
                ToolCall(name="delegate_research", input_parameters={}, output=report),
            ],
        )

    @pytest.mark.parametrize(
        "retrieved_id, cited_path",
        [
            # Four-segment regnal ids: the truncation bug.
            ("ukpga/Edw7/4/31", "ukpga/Edw7/4/31"),
            ("ukpga/Geo6/12-13-14/25", "ukpga/Geo6/12-13-14/25/section/3"),
            ("ukla/Eliz2/1-2/27", "ukla/Eliz2/1-2/27"),
            # Three-segment regnal id: case alone was enough to break it.
            ("ukpga/1-2Geo5/54", "ukpga/1-2Geo5/54/section/14"),
            # Underscored regnal id.
            ("ukpga/26Geo5_1Edw8/28/1936", "ukpga/26Geo5_1Edw8/28/1936"),
            # Modern ids must keep working.
            ("ukpga/1978/29", "ukpga/1978/29/section/10C"),
            ("ssi/2008/216", "ssi/2008/216/regulation/4"),
        ],
    )
    def test_a_retrieved_act_is_grounded(self, retrieved_id, cited_path):
        metric = CitationGroundingMetric()
        metric.measure(self._case(retrieved_id, cited_path))
        assert metric.score == 1.0, metric.reason
        assert "Fabricated" not in metric.reason

    def test_an_act_that_was_never_retrieved_still_fails(self):
        """The fix must not make everything pass."""
        metric = CitationGroundingMetric()
        metric.measure(self._case("ukpga/Edw7/4/31", "ukpga/Geo6/14/28"))
        assert metric.score == 0.0
        assert "ukpga/geo6/14/28" in metric.reason

    @pytest.mark.parametrize(
        "suffix",
        ["section/8", "schedule/1", "regulation/4", "article/2", "made", "enacted"],
    )
    def test_provision_and_version_suffixes_are_stripped(self, suffix):
        metric = CitationGroundingMetric()
        metric.measure(self._case("ssi/2008/216", f"ssi/2008/216/{suffix}"))
        assert metric.score == 1.0, metric.reason

    def test_an_extent_suffix_after_a_section_is_stripped(self):
        metric = CitationGroundingMetric()
        metric.measure(
            self._case("ukpga/1990/43", "ukpga/1990/43/section/79/england+wales")
        )
        assert metric.score == 1.0, metric.reason


class TestMalformedLinksAreNotFabricatedCitations:
    """A model sometimes writes prose into a link body, e.g.
    ``.../id/[UNCLEAR: no document reference provided]``. That is a broken
    link, not a claim about an Act, so calling it a fabricated citation
    accuses the run of the wrong thing. Reported, not scored.
    """

    @staticmethod
    def _case(*cited_paths: str) -> LLMTestCase:
        search_output = json.dumps(
            {"results": [{"legislation_id": "asp/2021/3", "title": "An Act"}]}
        )
        report = " ".join(
            f"See [it](http://www.legislation.gov.uk/id/{p})." for p in cited_paths
        )
        return LLMTestCase(
            input="q",
            actual_output="final answer",
            tools_called=[
                ToolCall(
                    name="Worker: search_legislation",
                    input_parameters={},
                    output=search_output,
                ),
                ToolCall(name="delegate_research", input_parameters={}, output=report),
            ],
        )

    @pytest.mark.parametrize(
        "junk",
        [
            "[UNCLEAR:%20no%20document%20reference%20provided]",
            "10%20&%2011%20Geo.%205.%20c.%2058",
            "s.i.%201950%20No.%201133%20(S.%2080)",
        ],
    )
    def test_a_malformed_link_does_not_fail_the_metric(self, junk):
        metric = CitationGroundingMetric()
        metric.measure(self._case("asp/2021/3", junk))
        assert metric.score == 1.0, metric.reason
        assert "Fabricated" not in metric.reason

    def test_a_malformed_link_is_still_reported(self):
        metric = CitationGroundingMetric()
        metric.measure(self._case("asp/2021/3", "[UNCLEAR:%20none%20provided]"))
        assert "Diagnostic, not scored" in metric.reason
        assert "not a legislation id" in metric.reason

    def test_a_genuine_fabrication_alongside_junk_still_fails(self):
        metric = CitationGroundingMetric()
        metric.measure(self._case("ukpga/1999/99", "[UNCLEAR:%20none]"))
        assert metric.score == 0.0
        assert "ukpga/1999/99" in metric.reason

    def test_a_truncated_act_link_is_not_a_fabrication(self):
        """A bare type/year link has no chapter number, so it names no Act.
        Response 95 wrote one alongside two correct links to
        ukpga/1988/41/section/65, and the Act itself had been retrieved."""
        metric = CitationGroundingMetric()
        metric.measure(self._case("asp/2021/3", "ukpga/1988"))
        assert metric.score == 1.0, metric.reason
        assert "Diagnostic, not scored" in metric.reason


# ---------------------------------------------------------------------------
# A step stopped at LexChat's tool-call limit
# ---------------------------------------------------------------------------

_HALT_REPORT = """[Research Incomplete - step limit reached]
This research step was stopped by a fixed limit of 20 tool-call rounds before
it produced any findings.
"""


class TestHaltedStepsAreSkipped:
    """A halted step has no report of the model's to score.

    LexChat used to send a halt notice through its reformat retry, which
    dressed it up in the required headings and made a step that retrieved
    nothing indistinguishable from a complete one. That reformat is now
    skipped deliberately, so the notice arrives unheaded. Scoring it as a
    missing-heading failure would mark the fix as a regression.
    """

    def test_halted_step_does_not_fail_the_run(self):
        metric = MandatoryStructureMetric(halted_steps={2})
        metric.measure(_test_case(_VALID_REPORT, _HALT_REPORT))
        assert metric.score == 1.0
        assert metric.is_successful()
        assert "1 halted step(s) skipped" in metric.reason

    def test_unhalted_step_still_fails(self):
        """Skipping a halt must not excuse a sibling's missing headings."""
        metric = MandatoryStructureMetric(halted_steps={2})
        metric.measure(_test_case("No headings at all.", _HALT_REPORT))
        assert metric.score == 0.0
        assert not metric.is_successful()
        assert "step 1" in metric.reason

    def test_every_step_halted_is_not_measured(self):
        metric = MandatoryStructureMetric(halted_steps={1, 2})
        metric.measure(_test_case(_HALT_REPORT, _HALT_REPORT))
        assert metric.reason.startswith("Not measured:")
        assert not metric.is_successful()

    def test_no_halts_behaves_as_before(self):
        metric = MandatoryStructureMetric()
        metric.measure(_test_case(_VALID_REPORT))
        assert metric.score == 1.0
        assert "skipped" not in metric.reason


class TestGenuineGapAcceptsTheCurrentWording:
    """The prompt's mandated sentence is not the wording LexChat asks for.

    LexChat measured that sentence appearing in 0 of 179 pre-pilot answers.
    Its tool results now carry a rule telling the Worker to report a miss as
    not found and say what was searched for, so that phrasing is the required
    behaviour and not a paraphrase of it.
    """

    _EMPTY = [
        ToolCall(
            name="Worker: search_legislation_sections",
            input_parameters={"legislation_id": "asp/2021/3"},
            output="",
        )
    ]

    def _measure(self, report, chat_mode="research"):
        case = LLMTestCase(
            input="q",
            actual_output="final",
            tools_called=[
                ToolCall(name="delegate_research", input_parameters={}, output=report),
                *self._EMPTY,
            ],
        )
        metric = GenuineGapMetric(chat_mode=chat_mode)
        metric.measure(case)
        return metric

    def test_not_found_scores_full_in_research_mode(self):
        m = self._measure("The searches for commencement orders returned not found.")
        assert m.score == 1.0
        assert m.is_successful()

    def test_not_held_scores_full_in_research_mode(self):
        m = self._measure("This index has no record under that id: not held.")
        assert m.score == 1.0

    def test_a_paraphrase_still_only_scores_half_in_research_mode(self):
        m = self._measure("There is no relevant material on this.")
        assert m.score == 0.5
        assert not m.is_successful()

    def test_an_undisclosed_gap_still_scores_zero(self):
        m = self._measure("Section 4 plainly requires consultation.")
        assert m.score == 0.0

    def test_the_current_research_sentence_scores_full(self):
        m = self._measure(
            "The material retrieved in this search does not establish the answer."
        )
        assert m.score == 1.0

    def test_made_under_coverage_wording_scores_full(self):
        """The made-under tool tells the Worker an instrument outside the
        record's coverage was not checked."""
        m = self._measure(
            "The record holds no instrument made under section 117. "
            "Instruments made before 1987 were not checked."
        )
        assert m.score == 1.0

    def test_an_empty_made_under_record_said_plainly_passes_in_conversation(self):
        m = self._measure(
            "I found no instrument that names section 117 as its power.",
            chat_mode="conversational",
        )
        assert m.score == 1.0


class TestLexChatsOwnBlocksAreNotReadAsTheModelsWords:
    """LexChat appends instruction blocks to a Worker report, in code.

    On a measured conversational report they were 55% of it. The search-scope
    block tells the model that anything it reports as not found was "not found
    in this index", so a gap-disclosure check matching that phrase would pass
    every report ever written, whether or not the model disclosed anything.
    Scoring the model's writing means removing them first.
    """

    # Shortened from a real capture, markers and wording verbatim.
    _BLOCK = (
        "[SEARCH SCOPE - what this research step actually did]\n"
        "NONE of this can establish that something does not exist. If any part "
        "of the answer you write reports something as not found, it MUST quote "
        "the search terms above. Absence from the index is therefore NOT "
        "evidence of absence in law.\n"
        "[/SEARCH SCOPE]"
    )
    _EMPTY = [
        ToolCall(
            name="Worker: search_legislation_sections",
            input_parameters={"legislation_id": "asp/2021/3"},
            output="",
        )
    ]

    def _gap(self, model_text):
        case = LLMTestCase(
            input="q",
            actual_output="final",
            tools_called=[
                ToolCall(
                    name="delegate_research",
                    input_parameters={},
                    output=f"{model_text}\n{self._BLOCK}",
                ),
                *self._EMPTY,
            ],
        )
        metric = GenuineGapMetric()
        metric.measure(case)
        return metric

    def test_the_block_alone_does_not_count_as_disclosure(self):
        """The whole point: an undisclosed gap must still score 0.0."""
        assert self._gap("Section 4 plainly requires consultation.").score == 0.0

    def test_the_models_own_disclosure_still_counts(self):
        assert self._gap("The instrument was not found in the index.").score == 1.0

    def test_headings_inside_a_block_do_not_satisfy_the_structure_check(self):
        metric = MandatoryStructureMetric()
        metric.measure(_test_case(f"{self._BLOCK}\n### References\n- a"))
        assert metric.score == 0.0

    def test_stripping_leaves_citation_urls_alone(self):
        """Citation checks read the raw report; the blocks carry no URLs."""
        from lex_eval.metrics.structure import _cited_legislation_ids, _model_words

        report = (
            "See [Act](http://www.legislation.gov.uk/id/ssi/2008/216).\n"
            f"{self._BLOCK}"
        )
        assert _cited_legislation_ids(report) == _cited_legislation_ids(
            _model_words(report)
        )


class TestModelWordsBlockRemoval:
    """Three of LexChat's six block markers are paired, three self-contained.

    Verified by grepping LexChat/server_py/src/utils/ for closing forms:
    SEARCH SCOPE, SECTION OUTLINE and PINPOINTS TO KEEP close; ENABLING POWER,
    CHANGE RECORD and CURRENCY do not. Treating them all as paired takes the
    rest of the report away with a self-contained marker.
    """

    def test_paired_block_and_its_contents_go(self):
        from lex_eval.metrics.structure import _model_words

        out = _model_words("A.\n[SEARCH SCOPE - x]\nnot found\n[/SEARCH SCOPE]\nB.")
        assert "not found" not in out
        assert "A." in out and "B." in out

    def test_unclosed_paired_block_is_dropped_to_the_end(self):
        """A truncated report must not leak LexChat's words as the model's."""
        from lex_eval.metrics.structure import _model_words

        out = _model_words("A.\n[SEARCH SCOPE - x]\nnot found blah")
        assert out == "A."

    def test_self_contained_marker_keeps_the_text_after_it(self):
        from lex_eval.metrics.structure import _model_words

        out = _model_words("Findings [ENABLING POWER - record states X] more.")
        assert "more." in out
        assert "ENABLING POWER" not in out

    def test_a_report_with_no_blocks_is_unchanged(self):
        from lex_eval.metrics.structure import _model_words

        assert _model_words("Just the model writing.") == "Just the model writing."


class TestSearchScopeText:
    """The [SEARCH SCOPE] text that `_model_words` removes, kept for the judge."""

    def test_returns_the_block_contents_without_the_report(self):
        from lex_eval.metrics.structure import _search_scope_text

        out = _search_scope_text(
            "Findings.\n[SEARCH SCOPE - x]\nIn-force: not established.\n"
            "[/SEARCH SCOPE]\nMore findings."
        )
        assert out == "In-force: not established."

    def test_joins_one_block_per_step(self):
        from lex_eval.metrics.structure import _search_scope_text

        out = _search_scope_text(
            "[SEARCH SCOPE - a]\none\n[/SEARCH SCOPE] x [SEARCH SCOPE - b]\ntwo"
        )
        assert out == "one\n\ntwo"

    def test_a_report_with_no_block_gives_nothing(self):
        from lex_eval.metrics.structure import _search_scope_text

        assert _search_scope_text("Just the model writing.") == ""


class TestAnswerScopeFooterRemoval:
    """LexChat's disclosure reaches the answer and the report in different forms.

    The report gets a bracketed `[SEARCH SCOPE ...]` block; the answer gets an
    italic `*Search scope: ...*` footer carrying index-coverage percentages and
    a sampling date. Strip the report alone and a groundedness check sees an
    answer asserting statistics with no support behind it, and calls the model
    unfounded for text the model never wrote. Measured: this accounted for 4 of
    5 conversational groundedness failures before the footer was stripped too.
    """

    _FOOTER = (
        "\n\n*Search scope: the legislation index was searched for "
        '"vitamin margarine"; no jurisdiction filter narrowed it. Roughly 85% '
        "of 2025 Scottish SIs are held (sampled Sep 2026).*"
    )

    def test_footer_is_removed_from_an_answer(self):
        from lex_eval.metrics.structure import _model_words

        out = _model_words(f"Regulation 4 sets the vitamin levels.{self._FOOTER}")
        assert out == "Regulation 4 sets the vitamin levels."

    def test_index_statistics_do_not_survive(self):
        from lex_eval.metrics.structure import _model_words

        out = _model_words(f"An answer.{self._FOOTER}").lower()
        assert "85%" not in out and "sampled sep 2026" not in out

    def test_an_answer_with_no_footer_is_unchanged(self):
        from lex_eval.metrics.structure import _model_words

        assert _model_words("Just the answer.") == "Just the answer."

    def test_a_mid_answer_mention_of_search_scope_is_kept(self):
        """Only the trailing footer goes, not the words wherever they appear."""
        from lex_eval.metrics.structure import _model_words

        text = "The search scope: was narrow.\n\nBut the answer continues."
        assert _model_words(text) == text


class TestChangeRecordCountsAsRetrieval:
    """An instrument named in a change record was retrieved, not invented.

    `get_legislation_changes` returns legislation.gov.uk's own record of what
    amends or commences an instrument. LexChat's Worker prompt now requires it
    for any in-force, commencement or amendment question, so it is a main
    retrieval route, not a curiosity. Before it was counted, Citation Grounding
    reported fabrication for correctly sourced citations: measured on two
    deep-research responses, all 14 accused ids came from a change record and
    appeared in no other tool output.
    """

    def _changes_call(self, target, related):
        return ToolCall(
            name="Worker: get_legislation_changes",
            input_parameters={"legislation_id": target, "direction": "to"},
            output=json.dumps(
                {
                    "legislation_id": target,
                    "direction": "to",
                    "related": [
                        {"legislation_id": r, "type_of_effect": "inserted"}
                        for r in related
                    ],
                }
            ),
        )

    def test_related_instruments_count_as_retrieved(self):
        from lex_eval.metrics.structure import _retrieved_legislation_ids

        got = _retrieved_legislation_ids(
            [self._changes_call("asp/2000/1", ["asp/2010/8", "uksi/2014/631"])]
        )
        assert {"asp/2000/1", "asp/2010/8", "uksi/2014/631"} <= got

    def test_a_citation_from_a_change_record_is_not_fabrication(self):
        case = LLMTestCase(
            input="q",
            actual_output="final",
            tools_called=[
                ToolCall(
                    name="delegate_research",
                    input_parameters={},
                    output=(
                        "Amended by [asp 2010/8]"
                        "(http://www.legislation.gov.uk/id/asp/2010/8)."
                    ),
                ),
                self._changes_call("asp/2000/1", ["asp/2010/8"]),
            ],
        )
        metric = CitationGroundingMetric()
        metric.measure(case)
        assert metric.score == 1.0, metric.reason

    def test_a_genuinely_unretrieved_citation_still_fails(self):
        """The fabrication check must still work."""
        case = LLMTestCase(
            input="q",
            actual_output="final",
            tools_called=[
                ToolCall(
                    name="delegate_research",
                    input_parameters={},
                    output=(
                        "See [an Act]" "(http://www.legislation.gov.uk/id/asp/1999/99)."
                    ),
                ),
                self._changes_call("asp/2000/1", ["asp/2010/8"]),
            ],
        )
        metric = CitationGroundingMetric()
        metric.measure(case)
        assert metric.score == 0.0
        assert "asp/1999/99" in metric.reason

    def test_malformed_change_record_output_is_ignored(self):
        from lex_eval.metrics.structure import _retrieved_legislation_ids

        bad = ToolCall(
            name="Worker: get_legislation_changes",
            input_parameters={"legislation_id": "asp/2000/1"},
            output="not json at all",
        )
        assert _retrieved_legislation_ids([bad]) == {"asp/2000/1"}


class TestCitationReadNamesTheRoute:
    """The verdict stays; the explanation stops misdescribing what happened.

    An Act known only from a change record was retrieved, so it is not
    fabrication, but its text was not pulled, so it is not read. Saying it was
    "cited on the strength of a search result title" is simply untrue, and it
    was untrue of 41 of 44 unread citations across twelve measured responses.
    """

    def _case(self, cited_id, *tools):
        return LLMTestCase(
            input="q",
            actual_output="final",
            tools_called=[
                ToolCall(
                    name="delegate_research",
                    input_parameters={},
                    output=(
                        f"See [an Act]"
                        f"(http://www.legislation.gov.uk/id/{cited_id})."
                    ),
                ),
                *tools,
            ],
        )

    def test_a_change_record_citation_is_named_as_such(self):
        changes = ToolCall(
            name="Worker: get_legislation_changes",
            input_parameters={"legislation_id": "asp/2000/1"},
            output=json.dumps({"related": [{"legislation_id": "asp/2010/8"}]}),
        )
        metric = CitationReadMetric()
        metric.measure(self._case("asp/2010/8", changes))
        assert "known from a change record" in metric.reason
        assert "search result title" not in metric.reason
        assert metric.score == 0.0  # the verdict is unchanged

    def test_a_search_title_citation_is_still_named_as_such(self):
        search = ToolCall(
            name="Worker: search_legislation",
            input_parameters={},
            output=json.dumps(
                {"results": [{"legislation_id": "asp/2010/8", "title": "An Act"}]}
            ),
        )
        metric = CitationReadMetric()
        metric.measure(self._case("asp/2010/8", search))
        assert "search result title" in metric.reason
        assert "known from a change record" not in metric.reason

    def test_a_read_citation_still_passes(self):
        read = ToolCall(
            name="Worker: get_legislation_text",
            input_parameters={"legislation_id": "asp/2010/8"},
            output="The full text of the Act.",
        )
        metric = CitationReadMetric()
        metric.measure(self._case("asp/2010/8", read))
        assert metric.score == 1.0


class TestRecordToolsCountAsRetrieval:
    """An instrument listed by find_definitions or find_instruments_made_under
    was retrieved, with the words the answer relies on, so citing it is
    neither fabrication nor citing unread. Measured on the v2026.11.1 gather,
    where every id Citation Grounding called fabricated on the definitions and
    made-under questions was in one of these two results."""

    def _definitions(self, *ids):
        return ToolCall(
            name="Worker: find_definitions",
            input_parameters={"term": "shop"},
            output=json.dumps(
                {
                    "tool": "find_definitions",
                    "count": len(ids),
                    "definitions": [
                        {"legislation_id": i, "definition": '"shop" means x'}
                        for i in ids
                    ],
                    "longer_terms": [],
                }
            )
            + "\n\n[SEARCH SCOPE — appended by code][/SEARCH SCOPE]",
        )

    def _made_under(self, status="found", *ids):
        return ToolCall(
            name="Worker: find_instruments_made_under",
            input_parameters={"act": "asp/2018/9", "section": "95"},
            output=json.dumps(
                {
                    "tool": "find_instruments_made_under",
                    "status": status,
                    "act_id": "asp/2018/9",
                    "count": len(ids),
                    "instruments": [{"legislation_id": i} for i in ids],
                }
            )
            + "\n\n[ENABLING POWER — the made-under record: ...]",
        )

    def _case(self, report, *tools):
        return LLMTestCase(
            input="q",
            actual_output="final",
            tools_called=[
                ToolCall(name="delegate_research", input_parameters={}, output=report),
                *tools,
            ],
        )

    _REPORT = (
        "[One](http://www.legislation.gov.uk/id/uksi/1967/907) and "
        "[Two](https://www.legislation.gov.uk/ssi/2020/351)."
    )

    def test_listed_instruments_are_grounded_and_read(self):
        case = self._case(
            self._REPORT,
            self._definitions("uksi/1967/907"),
            self._made_under("found", "SSI/2020/351"),
        )
        for metric in (CitationGroundingMetric(), CitationReadMetric()):
            metric.measure(case)
            assert metric.score == 1.0, metric.reason

    def test_the_made_under_act_is_retrieved_but_not_read(self):
        from lex_eval.metrics.structure import (
            _read_legislation_ids,
            _retrieved_legislation_ids,
        )

        tools = [self._made_under("found", "ssi/2020/351")]
        assert "asp/2018/9" in _retrieved_legislation_ids(tools)
        assert "asp/2018/9" not in _read_legislation_ids(tools)

    def test_made_under_without_a_found_list_contributes_nothing(self):
        from lex_eval.metrics.structure import _retrieved_legislation_ids

        tools = [self._made_under("act_not_in_record")]
        assert _retrieved_legislation_ids(tools) == set()
        assert not _retrieved_usable_content(tools)

    def test_a_list_is_usable_retrieval_for_genuine_gap(self):
        assert _retrieved_usable_content([self._definitions("ukpga/1996/18")])
        assert _retrieved_usable_content([self._made_under("found", "ssi/2020/351")])
        assert not _retrieved_usable_content([self._definitions()])

        metric = GenuineGapMetric()
        metric.measure(
            self._case("A confident answer.", self._definitions("ukpga/1996/18"))
        )
        assert metric.score == 1.0, metric.reason

    def test_an_unlisted_citation_still_fails(self):
        case = self._case(
            "[X](http://www.legislation.gov.uk/id/asp/1999/99).",
            self._definitions("uksi/1967/907"),
        )
        metric = CitationGroundingMetric()
        metric.measure(case)
        assert metric.score == 0.0
        assert "asp/1999/99" in metric.reason


class TestCodeSuppliedInstrumentsCountAsRetrieval:
    """LexChat's code hands the Worker two kinds of evidence inside another
    tool's result: a section search's list from the made-under record, and the
    text of an instrument the index lacks, read from legislation.gov.uk. Either
    instrument was retrieved, so citing it is neither fabrication nor citing
    unread."""

    _SECTIONS = ToolCall(
        name="Worker: search_legislation_sections",
        input_parameters={"legislation_id": "ukpga/1987/26"},
        output='{"results": []}\n\n[ENABLING POWER — the made-under record, for '
        "the sections this search returned of the Housing (Scotland) Act 1987 "
        "(ukpga/1987/26) and the lettered sections beside them: instruments whose "
        "own preamble names the section given as a power they were made under. "
        "Made under s.33A: The Homeless Persons (Suspension of Referrals between "
        "Local Authorities) (Scotland) Order 2022 (ssi/2022/356) (1 more recorded "
        "on legislation.gov.uk as revoked in whole, not listed). You MAY state ...]",
    )

    def _lookup(self, read: bool):
        lead = "[PROVISION FETCHED BY CODE — this index lacks 1962 c. 47"
        if read:
            text = (
                f"{lead}, so code read its text from legislation.gov.uk, the official "
                "publisher, through the LEX API: the version as enacted. This text "
                "comes from legislation.gov.uk, a different source from the index: "
                "cite it as https://www.legislation.gov.uk/ukpga/Eliz2/10-11/47/enacted"
                ", and say in the report that it was read from legislation.gov.uk.]\n"
                "117. Regulations ...\n[/PROVISION FETCHED BY CODE]"
            )
        else:
            text = (
                f"{lead}, and code's read of its text from legislation.gov.uk did not "
                "complete (no reply), so its text could not be checked here.]"
            )
        return ToolCall(
            name="Worker: lookup_legislation",
            input_parameters={"legislation_type": "ukpga", "year": 1962, "number": 47},
            output='{"status": "not_held"}  ' + text,
        )

    def _case(self, report, *tools):
        return LLMTestCase(
            input="q",
            actual_output="final",
            tools_called=[
                ToolCall(name="delegate_research", input_parameters={}, output=report),
                *tools,
            ],
        )

    def test_a_section_search_made_under_list_is_grounded_and_read(self):
        case = self._case(
            "[Order](https://www.legislation.gov.uk/id/ssi/2022/356).", self._SECTIONS
        )
        for metric in (CitationGroundingMetric(), CitationReadMetric()):
            metric.measure(case)
            assert metric.score == 1.0, metric.reason

    def test_text_code_read_from_legislation_gov_uk_is_grounded_read_and_usable(self):
        report = "[1962 Act](https://www.legislation.gov.uk/ukpga/Eliz2/10-11/47)."
        case = self._case(report, self._lookup(read=True))
        for metric in (CitationGroundingMetric(), CitationReadMetric()):
            metric.measure(case)
            assert metric.score == 1.0, metric.reason
        assert _retrieved_usable_content([self._lookup(read=True)])

    def test_a_failed_read_adds_nothing(self):
        assert not _retrieved_usable_content([self._lookup(read=False)])
        metric = CitationGroundingMetric()
        metric.measure(
            self._case(
                "[1962 Act](https://www.legislation.gov.uk/ukpga/Eliz2/10-11/47).",
                self._lookup(read=False),
            )
        )
        assert metric.score == 0.0


def test_a_search_result_id_with_a_series_number_matches_its_citation():
    """LEX lists some old instruments as "uksi/1951/548 (S. 25)"; a citation
    URL carries only "uksi/1951/548"."""
    case = LLMTestCase(
        input="q",
        actual_output="final",
        tools_called=[
            ToolCall(
                name="delegate_research",
                input_parameters={},
                output="[Regs](http://www.legislation.gov.uk/id/uksi/1951/548).",
            ),
            ToolCall(
                name="Worker: search_legislation",
                input_parameters={"query": "q"},
                output=json.dumps(
                    {"results": [{"legislation_id": "uksi/1951/548 (S. 25)"}]}
                ),
            ),
        ],
    )
    metric = CitationGroundingMetric()
    metric.measure(case)
    assert metric.score == 1.0, metric.reason
